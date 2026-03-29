"""Experience types — structured lessons extracted from Journal entries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class LessonType(StrEnum):
    """Categories of lessons that can be extracted."""

    TOOL_EFFECTIVENESS = "tool_effectiveness"
    # "scanner_v2 (AST) found more issues than scanner_v1 (regex)"

    FAILURE_PATTERN = "failure_pattern"
    # "regex matching for exec() gives false positives from subprocess.exec"

    STRATEGY = "strategy"
    # "for code analysis, create a specialized tool first"

    COST_INSIGHT = "cost_insight"
    # "this type of task typically costs 30-40 credits"

    TOOL_CREATION = "tool_creation"
    # "created scanner_v2 for security analysis — reusable"


@dataclass
class ToolPerformance:
    """Performance record for a specific tool in a specific context."""

    tool_name: str
    success: bool
    cost: float = 0.0
    elapsed: float = 0.0
    error: str | None = None


@dataclass
class Lesson:
    """A single lesson extracted from an execution.

    Lessons are the atomic unit of experience. They capture
    what happened, why it matters, and when it's relevant.
    """

    # What was learned
    lesson_type: LessonType
    summary: str  # Human-readable summary
    detail: str = ""  # Detailed explanation

    # When is this relevant
    task_context: str = ""  # "security scanning", "code generation", etc.
    tags: list[str] = field(default_factory=list)  # searchable tags

    # Evidence
    source_pid: str = ""  # Checkpoint PID this came from
    step_indices: list[int] = field(default_factory=list)  # Which steps
    evidence: dict[str, Any] = field(default_factory=dict)

    # Quality
    confidence: float = 0.5  # 0-1, higher = more reliable
    # High confidence: backed by A/B comparison (fork)
    # Medium: single observation with clear outcome
    # Low: inference without direct evidence

    # Metadata
    created_at: str = ""  # ISO timestamp
    times_applied: int = 0  # How many times this lesson was used
    times_helpful: int = 0  # How many times it led to better outcomes

    @property
    def helpfulness_rate(self) -> float:
        """Fraction of times this lesson led to better outcomes."""
        if self.times_applied == 0:
            return 0.0
        return self.times_helpful / self.times_applied

    def to_prompt_text(self) -> str:
        """Format this lesson for injection into an LLM prompt."""
        confidence_label = (
            "high confidence"
            if self.confidence >= 0.8
            else "medium confidence"
            if self.confidence >= 0.5
            else "low confidence"
        )
        text = f"- [{self.lesson_type.value}] {self.summary} ({confidence_label})"
        if self.detail:
            text += f"\n  Detail: {self.detail}"
        return text
