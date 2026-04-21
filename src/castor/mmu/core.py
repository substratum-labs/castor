"""MMU: context window memory management controller.

AISA §2.2 shape — routes memory operations through 7 syscalls with
``memory_id``-based addressing:

    mem_write    — create a message in context (returns memory_id)
    mem_read     — read by ID (context or cold)
    mem_search   — search cold storage (returns ranked list with IDs)
    mem_delete   — permanently remove from context + cold
    mem_evict    — move from context → cold (single item)
    mem_promote  — move from cold → context
    mem_protect  — set/clear pinned flag

Auto-evict (Decision C): hard watermark safety net + soft watermark
policy-driven + pause/resume for application control.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from castor.gate.registry import ToolMetadata, ToolRegistry
from castor.mmu.cold_storage import InMemoryColdStorage
from castor.mmu.policy import DefaultMemoryPolicy
from castor.mmu.token_counter import CharCountEstimator, TokenCounter
from castor.models.checkpoint import (
    AgentCheckpoint,
    CastorMessage,
    compute_memory_id,
)
from castor.protocols import ColdStorageProtocol, MemoryPolicyProtocol

if TYPE_CHECKING:
    from castor.scheduler.proxy import SyscallProxy

logger = logging.getLogger("castor.mmu")

# Syscall names — AISA §2.2 canonical set.
MEM_WRITE = "mem_write"
MEM_READ = "mem_read"
MEM_SEARCH = "mem_search"
MEM_DELETE = "mem_delete"
MEM_EVICT = "mem_evict"
MEM_PROMOTE = "mem_promote"
MEM_PROTECT = "mem_protect"

_KERNEL_TOOL_NAMES = frozenset(
    {MEM_WRITE, MEM_READ, MEM_SEARCH, MEM_DELETE, MEM_EVICT, MEM_PROMOTE, MEM_PROTECT}
)


class MMU:
    """Context window memory manager (AISA §2.2 shape)."""

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
        self._msg_seq = 0

        self._register_tools(registry)

    # ── Public API ──

    @property
    def kernel_tool_names(self) -> set[str]:
        return set(_KERNEL_TOOL_NAMES)

    def pause_auto_evict(self) -> None:
        self._auto_evict_paused = True

    def resume_auto_evict(self) -> None:
        self._auto_evict_paused = False

    def total_tokens(self, checkpoint: AgentCheckpoint) -> int:
        total = 0
        for entry in checkpoint.context_history:
            if isinstance(entry, CastorMessage):
                c = entry.content
                total += (
                    entry.token_count
                    if entry.token_count > 0
                    else self._counter.count(c if isinstance(c, str) else str(c))
                )
        return total

    def next_memory_id(self, pid: str, role: str, content: str) -> str:
        mid = compute_memory_id(pid, self._msg_seq, role, content)
        self._msg_seq += 1
        return mid

    async def check_and_evict(
        self, proxy: SyscallProxy, checkpoint: AgentCheckpoint
    ) -> None:
        if self._auto_evict_paused:
            return
        total = self.total_tokens(checkpoint)
        if total > self._soft_watermark:
            ids = await self._policy.should_evict(
                checkpoint.context_history, self._soft_watermark
            )
            if ids:
                for mid in ids:
                    await proxy.syscall(MEM_EVICT, {"memory_id": mid})
                total = self.total_tokens(checkpoint)
        if total > self._hard_watermark:
            for mid in self._fifo_select_ids(checkpoint, self._hard_watermark):
                await proxy.syscall(MEM_EVICT, {"memory_id": mid})

    async def on_session_end(
        self, context_history: list[Any], syscall_log: list[Any]
    ) -> None:
        try:
            await self._policy.on_session_end(context_history, syscall_log)
        except Exception:
            logger.exception("on_session_end failed agent=%s", self._agent_id)

    # ── FIFO safety net ──

    def _fifo_select_ids(self, checkpoint: AgentCheckpoint, target: int) -> list[str]:
        total = self.total_tokens(checkpoint)
        ids: list[str] = []
        for entry in checkpoint.context_history:
            if total <= target:
                break
            if isinstance(entry, CastorMessage) and not entry.pinned and entry.id:
                c = entry.content
                tokens = (
                    entry.token_count
                    if entry.token_count > 0
                    else self._counter.count(c if isinstance(c, str) else str(c))
                )
                ids.append(entry.id)
                total -= tokens
        return ids

    # ── Post-syscall effects ──

    def find_by_id(
        self, checkpoint: AgentCheckpoint, memory_id: str
    ) -> CastorMessage | None:
        for entry in checkpoint.context_history:
            if isinstance(entry, CastorMessage) and entry.id == memory_id:
                return entry
        return None

    def apply_eviction(
        self, checkpoint: AgentCheckpoint, memory_id: str
    ) -> CastorMessage | None:
        for i, entry in enumerate(checkpoint.context_history):
            if isinstance(entry, CastorMessage) and entry.id == memory_id:
                return checkpoint.context_history.pop(i)
        return None

    def apply_protect(
        self, checkpoint: AgentCheckpoint, memory_id: str, protect: bool
    ) -> None:
        msg = self.find_by_id(checkpoint, memory_id)
        if msg:
            msg.pinned = protect

    def apply_delete(
        self, checkpoint: AgentCheckpoint, memory_id: str
    ) -> CastorMessage | None:
        return self.apply_eviction(checkpoint, memory_id)

    def apply_promote(self, checkpoint: AgentCheckpoint, msg: CastorMessage) -> None:
        pos = max(0, len(checkpoint.context_history) - 1)
        checkpoint.context_history.insert(pos, msg)

    def apply_write(self, checkpoint: AgentCheckpoint, msg: CastorMessage) -> None:
        checkpoint.context_history.append(msg)

    async def persist_evicted(
        self, msg: CastorMessage, summary: str | None = None
    ) -> None:
        if msg:
            await self._cold.store(
                self._agent_id, [msg], summary=summary, source="eviction"
            )

    # ── Tool registration ──

    def _register_tools(self, registry: ToolRegistry) -> None:
        async def _mem_write(
            content: str = "", metadata: dict | None = None, pin: bool = False
        ) -> dict:
            return {"content": content, "metadata": metadata, "pin": pin}

        registry.register(
            ToolMetadata(
                tool_name=MEM_WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "metadata": {"type": "object"},
                        "pin": {"type": "boolean"},
                    },
                    "required": ["content"],
                },
                func=_mem_write,
                is_async=True,
            )
        )

        async def _mem_read(memory_id: str = "") -> dict:
            entry = await self._cold.read(self._agent_id, memory_id)
            if entry:
                return {
                    "content": entry.get("content", ""),
                    "metadata": entry.get("metadata", {}),
                    "location": "COLD_STORAGE",
                }
            return {"content": "", "metadata": {}, "location": "NOT_FOUND"}

        registry.register(
            ToolMetadata(
                tool_name=MEM_READ,
                input_schema={
                    "type": "object",
                    "properties": {"memory_id": {"type": "string"}},
                    "required": ["memory_id"],
                },
                func=_mem_read,
                is_async=True,
            )
        )

        async def _mem_search(
            query: str = "", limit: int = 5, filter: dict | None = None
        ) -> dict:
            results = await self._cold.search(self._agent_id, query, limit, filter)
            return {"results": results}

        registry.register(
            ToolMetadata(
                tool_name=MEM_SEARCH,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                        "filter": {"type": "object"},
                    },
                    "required": ["query"],
                },
                func=_mem_search,
                is_async=True,
            )
        )

        async def _mem_delete(memory_id: str = "") -> dict:
            deleted = await self._cold.delete(self._agent_id, memory_id)
            return {"deleted": deleted}

        registry.register(
            ToolMetadata(
                tool_name=MEM_DELETE,
                input_schema={
                    "type": "object",
                    "properties": {"memory_id": {"type": "string"}},
                    "required": ["memory_id"],
                },
                func=_mem_delete,
                is_async=True,
            )
        )

        async def _mem_evict(memory_id: str = "") -> dict:
            return {"memory_id": memory_id, "evicted": True}

        registry.register(
            ToolMetadata(
                tool_name=MEM_EVICT,
                input_schema={
                    "type": "object",
                    "properties": {"memory_id": {"type": "string"}},
                    "required": ["memory_id"],
                },
                func=_mem_evict,
                is_async=True,
            )
        )

        async def _mem_promote(memory_id: str = "") -> dict:
            entry = await self._cold.read(self._agent_id, memory_id)
            if entry is None:
                return {"promoted": False}
            return {
                "promoted": True,
                "memory_id": memory_id,
                "content": entry.get("content", ""),
                "role": entry.get("role", "system"),
            }

        registry.register(
            ToolMetadata(
                tool_name=MEM_PROMOTE,
                input_schema={
                    "type": "object",
                    "properties": {"memory_id": {"type": "string"}},
                    "required": ["memory_id"],
                },
                func=_mem_promote,
                is_async=True,
            )
        )

        async def _mem_protect(memory_id: str = "", protect: bool = True) -> dict:
            return {"memory_id": memory_id, "protected": protect}

        registry.register(
            ToolMetadata(
                tool_name=MEM_PROTECT,
                input_schema={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "protect": {"type": "boolean"},
                    },
                    "required": ["memory_id"],
                },
                func=_mem_protect,
                is_async=True,
            )
        )
