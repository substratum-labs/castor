"""MMU: context window memory management controller.

Routes memory operations through four canonical syscalls so they appear
in the journal and are replay-safe:

    mem_evict   — remove messages from context, persist to cold storage
    mem_recall  — retrieve from cold storage, insert into context
    mem_pin     — mark a message as non-evictable
    mem_store   — agent explicitly stores something to cold storage

The MMU does NOT decide WHAT to evict — that's the ``MemoryPolicyProtocol``
(application layer). The MMU decides HOW to execute the policy's decisions
through kernel syscalls.

Auto-evict behaviour (Decision C):
    - Hard watermark: if tokens exceed ``hard_watermark``, MMU auto-evicts
      using FIFO as a safety net, even if the policy didn't request it.
    - Soft watermark (= ``hard_watermark * 0.8`` by default): the MMU
      consults the policy's ``should_evict()`` and executes its choices.
    - ``pause_auto_evict()`` / ``resume_auto_evict()`` suppress the auto
      check (Tiphys planning phase), but manual ``mem_evict`` still works.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from castor.gate.registry import ToolMetadata, ToolRegistry
from castor.mmu.cold_storage import InMemoryColdStorage
from castor.mmu.policy import DefaultMemoryPolicy
from castor.mmu.token_counter import CharCountEstimator, TokenCounter
from castor.models.checkpoint import AgentCheckpoint, CastorMessage
from castor.protocols import ColdStorageProtocol, MemoryPolicyProtocol

if TYPE_CHECKING:
    from castor.scheduler.proxy import SyscallProxy

logger = logging.getLogger("castor.mmu")

# Syscall names — must match what proxy dispatches / journal records.
MEM_EVICT = "mem_evict"
MEM_RECALL = "mem_recall"
MEM_PIN = "mem_pin"
MEM_STORE = "mem_store"

_KERNEL_TOOL_NAMES = frozenset({MEM_EVICT, MEM_RECALL, MEM_PIN, MEM_STORE})


class MMU:
    """Context window memory manager.

    Monitors ``context_history`` token usage, consults the memory policy,
    and dispatches eviction / recall through kernel syscalls.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        cold_storage: ColdStorageProtocol | None = None,
        policy: MemoryPolicyProtocol | None = None,
        token_counter: TokenCounter | None = None,
        hard_watermark: int = 8000,
        soft_watermark_ratio: float = 0.8,
        agent_id: str = "default",
    ) -> None:
        self._cold: ColdStorageProtocol = cold_storage or InMemoryColdStorage()
        self._policy: MemoryPolicyProtocol = policy or DefaultMemoryPolicy()
        self._counter: TokenCounter = token_counter or CharCountEstimator()
        self._hard_watermark = hard_watermark
        self._soft_watermark = int(hard_watermark * soft_watermark_ratio)
        self._agent_id = agent_id
        self._auto_evict_paused = False

        # Register the four memory syscall handlers as kernel-internal tools.
        self._register_tools(registry)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def kernel_tool_names(self) -> set[str]:
        """Kernel-internal tools auto-skipped during replay."""
        return set(_KERNEL_TOOL_NAMES)

    def pause_auto_evict(self) -> None:
        """Suppress automatic eviction checks.

        Manual ``mem_evict`` syscalls still work. Call
        ``resume_auto_evict()`` to restore.
        """
        self._auto_evict_paused = True
        logger.info("auto_evict_paused agent=%s", self._agent_id)

    def resume_auto_evict(self) -> None:
        """Restore automatic eviction checks (idempotent)."""
        self._auto_evict_paused = False
        logger.info("auto_evict_resumed agent=%s", self._agent_id)

    def total_tokens(self, checkpoint: AgentCheckpoint) -> int:
        """Sum token counts of all CastorMessage entries."""
        total = 0
        for entry in checkpoint.context_history:
            if isinstance(entry, CastorMessage):
                total += (
                    entry.token_count
                    if entry.token_count > 0
                    else self._counter.count(entry.content)
                )
        return total

    async def check_and_evict(
        self, proxy: SyscallProxy, checkpoint: AgentCheckpoint
    ) -> None:
        """Check token usage and evict if needed.

        Called by the runner before each LLM turn. When auto-evict is
        paused, this is a no-op. Otherwise:

        1. If tokens > soft watermark → consult policy.should_evict()
           and execute the policy's choices via mem_evict syscall.
        2. If tokens STILL > hard watermark after policy eviction →
           FIFO safety-net eviction (oldest non-pinned first).
        """
        if self._auto_evict_paused:
            return

        total = self.total_tokens(checkpoint)

        # Phase 1: policy-driven eviction at soft watermark
        if total > self._soft_watermark:
            indices = await self._policy.should_evict(
                checkpoint.context_history, self._soft_watermark
            )
            if indices:
                await proxy.syscall(
                    MEM_EVICT,
                    {"indices": indices, "summary": None},
                )
                total = self.total_tokens(checkpoint)

        # Phase 2: hard-watermark safety net (FIFO)
        if total > self._hard_watermark:
            fifo_indices = self._fifo_select(checkpoint, self._hard_watermark)
            if fifo_indices:
                await proxy.syscall(
                    MEM_EVICT,
                    {"indices": fifo_indices, "summary": None},
                )

    async def on_session_end(
        self,
        context_history: list[Any],
        syscall_log: list[Any],
    ) -> None:
        """Invoke the policy's session-end consolidation hook.

        Called by the runner/facade AFTER the final checkpoint is saved.
        Errors are logged but not raised — consolidation must not block
        teardown.
        """
        try:
            await self._policy.on_session_end(context_history, syscall_log)
        except Exception:
            logger.exception("on_session_end failed agent=%s", self._agent_id)

    # ------------------------------------------------------------------
    # Internal: FIFO safety net
    # ------------------------------------------------------------------

    def _fifo_select(self, checkpoint: AgentCheckpoint, target: int) -> list[int]:
        """Select oldest non-pinned message indices to get under target."""
        total = self.total_tokens(checkpoint)
        indices: list[int] = []
        for i, entry in enumerate(checkpoint.context_history):
            if total <= target:
                break
            if isinstance(entry, CastorMessage) and not entry.pinned:
                tokens = (
                    entry.token_count
                    if entry.token_count > 0
                    else self._counter.count(entry.content)
                )
                indices.append(i)
                total -= tokens
        return indices

    # ------------------------------------------------------------------
    # Internal: tool registration
    # ------------------------------------------------------------------

    def _register_tools(self, registry: ToolRegistry) -> None:
        """Register the four memory syscall handlers."""

        # ── mem_evict ──
        async def _mem_evict(
            indices: list[int] | None = None,
            summary: str | None = None,
        ) -> dict[str, Any]:
            # Called from within proxy.syscall context — self has access
            # to the checkpoint via the proxy. But since we're a tool
            # function, we receive only our declared args. The actual
            # context_history manipulation happens in _do_evict which
            # the proxy calls after the tool returns.
            return {"indices": indices or [], "summary": summary}

        registry.register(
            ToolMetadata(
                tool_name=MEM_EVICT,
                input_schema={
                    "type": "object",
                    "properties": {
                        "indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "summary": {"type": "string"},
                    },
                    "required": [],
                },
                func=_mem_evict,
                is_async=True,
            )
        )

        # ── mem_recall ──
        async def _mem_recall(
            query: str = "",
            max_results: int = 5,
            source_filter: str | None = None,
        ) -> dict[str, Any]:
            results = await self._cold.search(
                self._agent_id, query, max_results, source_filter
            )
            return {"messages": results}

        registry.register(
            ToolMetadata(
                tool_name=MEM_RECALL,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer"},
                        "source_filter": {"type": "string"},
                    },
                    "required": ["query"],
                },
                func=_mem_recall,
                is_async=True,
            )
        )

        # ── mem_pin ──
        async def _mem_pin(index: int = 0) -> dict[str, Any]:
            return {"index": index, "pinned": True}

        registry.register(
            ToolMetadata(
                tool_name=MEM_PIN,
                input_schema={
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                    },
                    "required": ["index"],
                },
                func=_mem_pin,
                is_async=True,
            )
        )

        # ── mem_store ──
        async def _mem_store(
            content: str = "",
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            await self._cold.store_explicit(self._agent_id, content, metadata)
            return {"stored": True}

        registry.register(
            ToolMetadata(
                tool_name=MEM_STORE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["content"],
                },
                func=_mem_store,
                is_async=True,
            )
        )

    # ------------------------------------------------------------------
    # Post-syscall hooks (called by proxy after journal record)
    # ------------------------------------------------------------------

    def apply_eviction(
        self,
        checkpoint: AgentCheckpoint,
        indices: list[int],
    ) -> list[CastorMessage]:
        """Remove messages at the given indices from context_history.

        Returns the removed messages (for cold storage persistence).
        Indices are processed in reverse order so removal doesn't shift
        positions of later indices.
        """
        removed: list[CastorMessage] = []
        for idx in sorted(set(indices), reverse=True):
            if 0 <= idx < len(checkpoint.context_history):
                entry = checkpoint.context_history.pop(idx)
                if isinstance(entry, CastorMessage):
                    removed.append(entry)
        removed.reverse()  # restore original order
        return removed

    def apply_pin(self, checkpoint: AgentCheckpoint, index: int) -> None:
        """Mark a message as pinned (non-evictable)."""
        if 0 <= index < len(checkpoint.context_history):
            entry = checkpoint.context_history[index]
            if isinstance(entry, CastorMessage):
                entry.pinned = True

    def apply_recall(
        self,
        checkpoint: AgentCheckpoint,
        messages: list[Any],
    ) -> None:
        """Insert recalled messages into context_history.

        Inserts before the last message (which is typically the current
        user query) so the LLM sees the recalled context.
        """
        if not messages:
            return
        # Convert dicts to CastorMessage if possible
        to_insert: list[CastorMessage | dict] = []
        for msg in messages:
            if isinstance(msg, CastorMessage):
                to_insert.append(msg)
            elif isinstance(msg, dict) and "content" in msg:
                to_insert.append(
                    CastorMessage(
                        role=msg.get("role", "system"),
                        content=str(msg["content"]),
                        pinned=False,
                    )
                )
        if to_insert:
            # Insert before the last entry
            pos = max(0, len(checkpoint.context_history) - 1)
            for i, m in enumerate(to_insert):
                checkpoint.context_history.insert(pos + i, m)

    async def persist_evicted(
        self,
        messages: list[CastorMessage],
        summary: str | None = None,
    ) -> None:
        """Send evicted messages to cold storage."""
        if messages:
            await self._cold.store(
                self._agent_id,
                messages,
                summary=summary,
                source="eviction",
            )
