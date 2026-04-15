"""Default memory policy — FIFO eviction with no recall.

This is the simplest possible ``MemoryPolicyProtocol`` implementation.
Applications that need smarter eviction (e.g. Tiphys with semantic +
episodic awareness) should provide their own implementation.
"""

from __future__ import annotations

from typing import Any

from castor.mmu.token_counter import CharCountEstimator, TokenCounter


class DefaultMemoryPolicy:
    """FIFO eviction: oldest non-pinned messages first.

    No summarization, no recall, no session-end hook. This is the
    baseline that castor-server uses when no richer policy is configured.
    """

    def __init__(
        self,
        *,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self._counter = token_counter or CharCountEstimator()

    async def should_evict(
        self,
        context_history: list[Any],
        token_budget: int,
    ) -> list[int] | None:
        total = 0
        for msg in context_history:
            if hasattr(msg, "token_count") and msg.token_count > 0:
                total += msg.token_count
            elif hasattr(msg, "content"):
                total += self._counter.count(
                    msg.content if isinstance(msg.content, str) else str(msg.content)
                )
            elif isinstance(msg, dict):
                total += self._counter.count(str(msg.get("content", "")))

        if total <= token_budget:
            return None

        # Evict oldest non-pinned messages until under budget
        to_evict: list[int] = []
        for i, msg in enumerate(context_history):
            pinned = getattr(msg, "pinned", False)
            if isinstance(msg, dict):
                pinned = msg.get("pinned", False)
            if pinned:
                continue

            if hasattr(msg, "token_count") and msg.token_count > 0:
                cost = msg.token_count
            elif hasattr(msg, "content"):
                cost = self._counter.count(
                    msg.content if isinstance(msg.content, str) else str(msg.content)
                )
            elif isinstance(msg, dict):
                cost = self._counter.count(str(msg.get("content", "")))
            else:
                cost = 0

            to_evict.append(i)
            total -= cost
            if total <= token_budget:
                break

        return to_evict if to_evict else None

    async def generate_summary(self, evicted_messages: list[Any]) -> str | None:
        return None  # No summarization in default policy

    async def should_recall(
        self, context_history: list[Any], current_query: str
    ) -> str | None:
        return None  # No recall in default policy

    async def on_session_end(
        self, context_history: list[Any], syscall_log: list[Any]
    ) -> None:
        pass  # No consolidation in default policy
