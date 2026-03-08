"""Token counting protocol and default estimator for Lodge eviction."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    """Protocol for counting tokens in a text string.

    Implement this to plug in tiktoken or another tokenizer.
    """

    def count(self, text: str) -> int: ...


class CharCountEstimator:
    """Rough token estimate: ``len(text) // 4``.

    Good enough for eviction decisions without adding a tiktoken dependency.
    """

    def count(self, text: str) -> int:
        return max(1, len(text) // 4)
