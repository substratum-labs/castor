"""Experience Replay — extract lessons from past executions.

Analyzes Journal entries to identify patterns, failures, and strategies.
Lessons are stored and retrieved for future tasks, making the agent
improve over time.

Usage:
    from castor.kernel.experience import extract_lessons, InMemoryExperienceStore

    # After a task completes
    lessons = extract_lessons(checkpoint, gate)

    # Store
    store = InMemoryExperienceStore()
    for lesson in lessons:
        store.add(lesson)

    # Before next task — retrieve relevant experience
    relevant = store.search("security scanning", limit=5)
    # → inject into agent's system prompt
"""

from castor.kernel.experience.extractor import extract_lessons
from castor.kernel.experience.store import (
    ExperienceStoreProtocol,
    InMemoryExperienceStore,
)
from castor.kernel.experience.types import (
    Lesson,
    LessonType,
    ToolPerformance,
)

__all__ = [
    "ExperienceStoreProtocol",
    "InMemoryExperienceStore",
    "Lesson",
    "LessonType",
    "ToolPerformance",
    "extract_lessons",
]
