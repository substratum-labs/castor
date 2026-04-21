"""Default memory policy — FIFO eviction with no recall.

This is the simplest possible ``MemoryPolicyProtocol`` implementation.
Applications that need smarter eviction (e.g. Tiphys with semantic +
episodic awareness) should provide their own implementation.

AISA shape: ``should_evict`` returns ``list[str]`` (memory_ids), not
indices. The kernel issues individual ``mem_evict(memory_id)`` syscalls.
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

    def _msg_tokens(self, msg: Any) -> int:
        if hasattr(msg, "token_count") and msg.token_count > 0:
            return msg.token_count
        if hasattr(msg, "content"):
            c = msg.content
            return self._counter.count(c if isinstance(c, str) else str(c))
        if isinstance(msg, dict):
            return self._counter.count(str(msg.get("content", "")))
        return 0

    def _msg_pinned(self, msg: Any) -> bool:
        if hasattr(msg, "pinned"):
            return msg.pinned
        if isinstance(msg, dict):
            return msg.get("pinned", False)
        return False

    def _msg_id(self, msg: Any) -> str:
        if hasattr(msg, "id"):
            return msg.id
        if isinstance(msg, dict):
            return msg.get("id", "")
        return ""

    async def should_evict(
        self,
        context_history: list[Any],
        token_budget: int,
    ) -> list[str] | None:
        total = sum(self._msg_tokens(m) for m in context_history)
        if total <= token_budget:
            return None

        to_evict: list[str] = []
        for msg in context_history:
            if self._msg_pinned(msg):
                continue
            mid = self._msg_id(msg)
            if not mid:
                continue  # skip messages without IDs
            to_evict.append(mid)
            total -= self._msg_tokens(msg)
            if total <= token_budget:
                break

        return to_evict if to_evict else None

    async def generate_summary(self, evicted_messages: list[Any]) -> str | None:
        return None

    async def should_recall(
        self, context_history: list[Any], current_query: str
    ) -> str | None:
        return None

    async def on_session_end(
        self, context_history: list[Any], syscall_log: list[Any]
    ) -> None:
        pass
