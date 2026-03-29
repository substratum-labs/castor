"""Experience extractor — analyzes Journal to produce Lessons.

Pure functions. Reads a checkpoint's syscall_log and extracts
structured lessons about what worked, what failed, and why.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from castor.kernel.experience.types import Lesson, LessonType, ToolPerformance

if TYPE_CHECKING:
    from castor.models.checkpoint import AgentCheckpoint
    from castor.protocols import GateProtocol


def extract_lessons(
    checkpoint: AgentCheckpoint,
    gate: GateProtocol | None = None,
) -> list[Lesson]:
    """Extract lessons from a completed checkpoint's Journal.

    Analyzes the syscall_log for patterns:
    - Tool effectiveness (which tools succeeded/failed)
    - Failure patterns (what went wrong and why)
    - Strategy insights (what approach worked)
    - Cost insights (how much did this type of task cost)
    - Tool creation (self-surgery — reusable tools created)

    Args:
        checkpoint: Completed checkpoint with syscall_log.
        gate: Optional gate for tool metadata (destructive, cost info).

    Returns:
        List of Lesson objects.
    """
    lessons: list[Lesson] = []
    log = checkpoint.syscall_log
    now = datetime.now(UTC).isoformat()

    if not log:
        return lessons

    # Analyze tool usage patterns
    lessons.extend(_extract_tool_patterns(log, checkpoint.pid, now))

    # Analyze failures
    lessons.extend(_extract_failure_patterns(log, checkpoint.pid, now))

    # Analyze self-surgery (tool creation)
    lessons.extend(_extract_tool_creation(log, checkpoint.pid, now))

    # Analyze cost
    lessons.extend(_extract_cost_insights(log, checkpoint, now))

    # Analyze overall strategy
    lessons.extend(_extract_strategy_insights(log, checkpoint, now))

    return lessons


def _extract_tool_patterns(
    log: list,
    pid: str,
    now: str,
) -> list[Lesson]:
    """Identify which tools were used and their effectiveness."""
    lessons = []
    tool_stats: dict[str, ToolPerformance] = {}

    for i, record in enumerate(log):
        tool = record.request.get("tool_name", "")
        response = str(record.response)

        is_error = (
            "error" in response.lower()[:50]
            or "failed" in response.lower()[:50]
            or (
                isinstance(record.response, dict)
                and record.response.get("status")
                in ("VALIDATION_ERROR", "INSUFFICIENT_CAPABILITY")
            )
        )

        if tool not in tool_stats:
            tool_stats[tool] = ToolPerformance(tool_name=tool, success=not is_error)
        elif is_error:
            tool_stats[tool].success = False

    # Find tools that failed then a different tool succeeded on similar input
    tool_sequence = [r.request.get("tool_name", "") for r in log]
    for i in range(len(tool_sequence) - 1):
        if tool_sequence[i] == tool_sequence[i + 1]:
            continue
        resp_i = str(log[i].response)
        if "error" in resp_i.lower()[:50] or "failed" in resp_i.lower()[:50]:
            resp_next = str(log[i + 1].response)
            if "error" not in resp_next.lower()[:50]:
                tf, ts = tool_sequence[i], tool_sequence[i + 1]
                lessons.append(
                    Lesson(
                        lesson_type=LessonType.TOOL_EFFECTIVENESS,
                        summary=f"'{tf}' failed, '{ts}' succeeded",
                        detail=(
                            f"Step {i} ({tf}) failed. Step {i + 1} ({ts}) succeeded."
                        ),
                        source_pid=pid,
                        step_indices=[i, i + 1],
                        confidence=0.7,
                        created_at=now,
                        tags=[tf, ts],
                    )
                )

    return lessons


def _extract_failure_patterns(
    log: list,
    pid: str,
    now: str,
) -> list[Lesson]:
    """Identify repeated failures and their patterns."""
    lessons = []
    failure_counts: dict[str, list[int]] = {}

    for i, record in enumerate(log):
        tool = record.request.get("tool_name", "")
        response = str(record.response)

        is_error = (
            "error" in response.lower()[:100] or "failed" in response.lower()[:100]
        )

        if is_error:
            failure_counts.setdefault(tool, []).append(i)

    for tool, indices in failure_counts.items():
        if len(indices) >= 2:
            lessons.append(
                Lesson(
                    lesson_type=LessonType.FAILURE_PATTERN,
                    summary=f"'{tool}' failed {len(indices)} times",
                    detail=f"Tool '{tool}' failed at steps {indices}. Consider using a different approach or creating a specialized tool.",
                    source_pid=pid,
                    step_indices=indices,
                    confidence=0.8,
                    created_at=now,
                    tags=[tool, "repeated_failure"],
                )
            )

    return lessons


def _extract_tool_creation(
    log: list,
    pid: str,
    now: str,
) -> list[Lesson]:
    """Identify self-surgery events (tools created during execution)."""
    lessons = []

    for i, record in enumerate(log):
        tool = record.request.get("tool_name", "")
        if tool != "create_tool":
            continue

        args = record.request.get("arguments", {})
        created_name = args.get("name", "unknown")
        description = args.get("description", "")
        response = str(record.response)

        if "created and registered" in response:
            lessons.append(
                Lesson(
                    lesson_type=LessonType.TOOL_CREATION,
                    summary=f"Created tool '{created_name}': {description[:80]}",
                    detail=f"Self-surgery at step {i}: agent created '{created_name}' to solve a problem the existing tools couldn't handle.",
                    source_pid=pid,
                    step_indices=[i],
                    confidence=0.9,
                    created_at=now,
                    tags=[created_name, "self_surgery", "tool_creation"],
                    evidence={"tool_name": created_name, "description": description},
                )
            )

    return lessons


def _extract_cost_insights(
    log: list,
    checkpoint: Any,
    now: str,
) -> list[Lesson]:
    """Extract cost/budget insights."""
    lessons = []

    total_steps = len(log)
    if total_steps == 0:
        return lessons

    # Count tool types
    tool_counts: dict[str, int] = {}
    for record in log:
        tool = record.request.get("tool_name", "")
        tool_counts[tool] = tool_counts.get(tool, 0) + 1

    total_budget = sum(c.current_usage for c in checkpoint.capabilities.values())

    if total_budget > 0:
        lessons.append(
            Lesson(
                lesson_type=LessonType.COST_INSIGHT,
                summary=f"Task used {total_budget:.1f} credits across {total_steps} steps",
                detail=f"Tool breakdown: {tool_counts}",
                source_pid=checkpoint.pid,
                confidence=1.0,
                created_at=now,
                tags=["cost", "budget"],
                evidence={
                    "total_credits": total_budget,
                    "steps": total_steps,
                    "tool_counts": tool_counts,
                },
            )
        )

    return lessons


def _extract_strategy_insights(
    log: list,
    checkpoint: Any,
    now: str,
) -> list[Lesson]:
    """Extract high-level strategy insights."""
    lessons = []

    if len(log) < 3:
        return lessons

    # Detect "research then act" pattern
    tool_sequence = [r.request.get("tool_name", "") for r in log]
    write_tools = {"write_file", "create_tool", "write_report"}

    first_write_idx = None
    for i, tool in enumerate(tool_sequence):
        if tool in write_tools:
            first_write_idx = i
            break

    if first_write_idx and first_write_idx >= 2:
        research_steps = first_write_idx
        action_steps = len(log) - first_write_idx
        lessons.append(
            Lesson(
                lesson_type=LessonType.STRATEGY,
                summary=f"Research-then-act pattern: {research_steps} research steps before first action",
                detail=f"Agent spent {research_steps} steps gathering information before taking action at step {first_write_idx}.",
                source_pid=checkpoint.pid,
                confidence=0.6,
                created_at=now,
                tags=["strategy", "research_first"],
                evidence={
                    "research_steps": research_steps,
                    "action_steps": action_steps,
                },
            )
        )

    return lessons
