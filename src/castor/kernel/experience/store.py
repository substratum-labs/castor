"""Experience store — persist and retrieve lessons.

Protocol + in-memory implementation. Swappable backend:
  Level 0: InMemoryExperienceStore (list, lost on restart)
  Level 1: JSON file store (persists to disk)
  Level 2: Vector DB store (semantic search)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from castor.kernel.experience.types import Lesson


@runtime_checkable
class ExperienceStoreProtocol(Protocol):
    """Interface for experience storage backends."""

    def add(self, lesson: Lesson) -> None:
        """Store a lesson."""
        ...

    def search(self, query: str, *, limit: int = 5) -> list[Lesson]:
        """Find lessons relevant to a query."""
        ...

    def get_all(self) -> list[Lesson]:
        """Get all stored lessons."""
        ...

    def __len__(self) -> int:
        """Number of stored lessons."""
        ...


class InMemoryExperienceStore:
    """In-memory experience store with keyword-based search.

    Simple but effective for Level 0. Lessons are lost on restart.
    For persistent storage, swap with a file-backed or vector DB store.
    """

    def __init__(self) -> None:
        self._lessons: list[Lesson] = []

    def add(self, lesson: Lesson) -> None:
        """Store a lesson."""
        self._lessons.append(lesson)

    def search(self, query: str, *, limit: int = 5) -> list[Lesson]:
        """Find lessons relevant to a query using keyword matching.

        Scores each lesson by how many query words appear in its
        summary, detail, tags, and task_context. Returns top N.
        """
        if not query or not self._lessons:
            return []

        query_words = set(query.lower().split())
        scored: list[tuple[float, Lesson]] = []

        for lesson in self._lessons:
            # Build searchable text
            text = " ".join(
                [
                    lesson.summary.lower(),
                    lesson.detail.lower(),
                    lesson.task_context.lower(),
                    " ".join(lesson.tags).lower(),
                ]
            )

            # Score: fraction of query words found + confidence boost
            words_found = sum(1 for w in query_words if w in text)
            if words_found == 0:
                continue

            score = (words_found / len(query_words)) * 0.7 + lesson.confidence * 0.3
            scored.append((score, lesson))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)
        return [lesson for _, lesson in scored[:limit]]

    def get_all(self) -> list[Lesson]:
        """Get all lessons."""
        return list(self._lessons)

    def get_by_type(self, lesson_type: str) -> list[Lesson]:
        """Get lessons of a specific type."""
        return [x for x in self._lessons if x.lesson_type.value == lesson_type]

    def get_by_tag(self, tag: str) -> list[Lesson]:
        """Get lessons with a specific tag."""
        return [x for x in self._lessons if tag in x.tags]

    def __len__(self) -> int:
        return len(self._lessons)

    def to_prompt_block(self, query: str, *, limit: int = 5) -> str:
        """Search and format lessons as a prompt block for LLM injection.

        Returns a formatted string ready to insert into a system prompt.
        """
        relevant = self.search(query, limit=limit)
        if not relevant:
            return ""

        lines = ["## Relevant Past Experience", ""]
        for lesson in relevant:
            lines.append(lesson.to_prompt_text())
        lines.append("")
        return "\n".join(lines)
