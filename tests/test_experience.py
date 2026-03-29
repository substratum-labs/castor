"""Tests for Experience Replay — lesson extraction and storage."""

from __future__ import annotations

from castor.kernel.experience import (
    InMemoryExperienceStore,
    Lesson,
    LessonType,
    extract_lessons,
)
from castor.models.checkpoint import AgentCheckpoint, SyscallRecord


def _cp(records: list[SyscallRecord], pid: str = "test") -> AgentCheckpoint:
    """Helper to create a checkpoint with given records."""
    return AgentCheckpoint(
        pid=pid,
        status="COMPLETED",
        agent_function_name="test",
        capabilities={},
        syscall_log=records,
    )


def _record(tool: str, response: str = "ok", args: dict | None = None) -> SyscallRecord:
    return SyscallRecord(
        request={"tool_name": tool, "arguments": args or {}},
        response=response,
    )


class TestExtractLessons:
    def test_empty_log(self):
        cp = _cp([])
        lessons = extract_lessons(cp)
        assert lessons == []

    def test_detects_tool_creation(self):
        cp = _cp(
            [
                _record("think", "plan..."),
                _record(
                    "create_tool",
                    "Tool 'scanner' created and registered successfully.",
                    args={"name": "scanner", "description": "Security scanner"},
                ),
                _record("scanner", "found 3 issues"),
            ]
        )
        lessons = extract_lessons(cp)

        creation_lessons = [
            l for l in lessons if l.lesson_type == LessonType.TOOL_CREATION
        ]
        assert len(creation_lessons) == 1
        assert "scanner" in creation_lessons[0].summary
        assert creation_lessons[0].confidence >= 0.9

    def test_detects_failure_then_success(self):
        cp = _cp(
            [
                _record("tool_a", "Error: failed to parse"),
                _record("tool_b", "success: parsed 100 records"),
            ]
        )
        lessons = extract_lessons(cp)

        effectiveness = [
            l for l in lessons if l.lesson_type == LessonType.TOOL_EFFECTIVENESS
        ]
        assert len(effectiveness) >= 1
        assert "tool_a" in effectiveness[0].summary
        assert "tool_b" in effectiveness[0].summary

    def test_detects_repeated_failure(self):
        cp = _cp(
            [
                _record("flaky_tool", "Error: timeout"),
                _record("flaky_tool", "Error: connection refused"),
            ]
        )
        lessons = extract_lessons(cp)

        failures = [l for l in lessons if l.lesson_type == LessonType.FAILURE_PATTERN]
        assert len(failures) >= 1
        assert "2 times" in failures[0].summary

    def test_detects_strategy_pattern(self):
        cp = _cp(
            [
                _record("think", "planning..."),
                _record("read_file", "contents..."),
                _record("think", "analyzing..."),
                _record("write_file", "wrote output"),
            ]
        )
        lessons = extract_lessons(cp)

        strategies = [l for l in lessons if l.lesson_type == LessonType.STRATEGY]
        assert len(strategies) >= 1
        assert "research" in strategies[0].summary.lower()

    def test_cost_insight(self):
        from castor.models.capability import Capability

        cp = AgentCheckpoint(
            pid="cost-test",
            status="COMPLETED",
            agent_function_name="test",
            capabilities={
                "api": Capability(resource_type="api", max_budget=50, current_usage=25)
            },
            syscall_log=[_record("think", "ok"), _record("write_file", "ok")],
        )
        lessons = extract_lessons(cp)

        costs = [l for l in lessons if l.lesson_type == LessonType.COST_INSIGHT]
        assert len(costs) == 1
        assert "25.0" in costs[0].summary


class TestExperienceStore:
    def test_add_and_retrieve(self):
        store = InMemoryExperienceStore()
        lesson = Lesson(
            lesson_type=LessonType.STRATEGY,
            summary="Use AST for code analysis",
            tags=["ast", "code", "analysis"],
        )
        store.add(lesson)
        assert len(store) == 1
        assert store.get_all()[0].summary == "Use AST for code analysis"

    def test_search_by_keyword(self):
        store = InMemoryExperienceStore()
        store.add(
            Lesson(
                lesson_type=LessonType.STRATEGY,
                summary="Use AST for security scanning",
                tags=["ast", "security"],
                confidence=0.9,
            )
        )
        store.add(
            Lesson(
                lesson_type=LessonType.STRATEGY,
                summary="Use JSON for config storage",
                tags=["json", "config"],
                confidence=0.7,
            )
        )

        results = store.search("security scanning")
        assert len(results) >= 1
        assert "AST" in results[0].summary

    def test_search_no_match(self):
        store = InMemoryExperienceStore()
        store.add(
            Lesson(
                lesson_type=LessonType.STRATEGY,
                summary="Use AST for scanning",
            )
        )
        results = store.search("quantum physics")
        assert results == []

    def test_get_by_type(self):
        store = InMemoryExperienceStore()
        store.add(Lesson(lesson_type=LessonType.STRATEGY, summary="a"))
        store.add(Lesson(lesson_type=LessonType.FAILURE_PATTERN, summary="b"))
        store.add(Lesson(lesson_type=LessonType.STRATEGY, summary="c"))

        strategies = store.get_by_type("strategy")
        assert len(strategies) == 2

    def test_get_by_tag(self):
        store = InMemoryExperienceStore()
        store.add(
            Lesson(lesson_type=LessonType.STRATEGY, summary="a", tags=["security"])
        )
        store.add(Lesson(lesson_type=LessonType.STRATEGY, summary="b", tags=["config"]))

        results = store.get_by_tag("security")
        assert len(results) == 1

    def test_to_prompt_block(self):
        store = InMemoryExperienceStore()
        store.add(
            Lesson(
                lesson_type=LessonType.TOOL_EFFECTIVENESS,
                summary="AST scanner finds more issues than regex",
                confidence=0.9,
                tags=["security", "scanner"],
            )
        )

        block = store.to_prompt_block("security scanning")
        assert "Past Experience" in block
        assert "AST scanner" in block
        assert "high confidence" in block

    def test_to_prompt_block_no_match(self):
        store = InMemoryExperienceStore()
        block = store.to_prompt_block("unrelated topic")
        assert block == ""

    def test_lesson_to_prompt_text(self):
        lesson = Lesson(
            lesson_type=LessonType.FAILURE_PATTERN,
            summary="Regex fails on nested quotes",
            detail="Use a proper parser instead",
            confidence=0.3,
        )
        text = lesson.to_prompt_text()
        assert "low confidence" in text
        assert "Regex fails" in text


class TestCastorLearn:
    def test_kernel_learn_method(self):
        from castor import Castor
        from castor.gate.registry import ToolMetadata, ToolRegistry
        from castor.gate.validator import SyscallGate

        reg = ToolRegistry()

        async def my_tool(x: str) -> str:
            return x

        reg.register(
            ToolMetadata.from_function(my_tool, consumes="api", cost_per_use=1.0)
        )

        kernel = Castor(gate=SyscallGate(reg))

        cp = _cp(
            [
                _record("my_tool", "ok"),
                _record("my_tool", "Error: something broke"),
                _record("my_tool", "ok again"),
            ]
        )
        cp.pid = "learn-test"

        lessons = kernel.learn(cp)
        assert isinstance(lessons, list)
        assert all(isinstance(l, Lesson) for l in lessons)
