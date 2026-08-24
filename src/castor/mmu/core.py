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
from castor.models.causal import (
    CascadeMode,
    ExternalSource,
    MemoryRef,
    ProvenanceGraph,
    ProvenanceNode,
    ProvenanceRef,
)
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
MEM_PROVENANCE = "mem_provenance"
MEM_EXPLAIN = "mem_explain"

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
        self._msg_seq = 0  # rebuilt from checkpoint in sync_seq()
        self._entries: dict[str, CastorMessage] = {}
        self._active_checkpoint: AgentCheckpoint

        self._register_tools(registry)

    def sync_seq(self, checkpoint: AgentCheckpoint) -> None:
        """Rebuild ``_msg_seq`` from checkpoint state.

        Called by the runner before each agent execution to ensure the
        seq counter matches what was persisted. Without this, a server
        restart would reset seq to 0, producing non-deterministic IDs
        if two messages happen to share the same role + content.

        Counts mem_write entries in the journal (each one incremented
        seq once in the original run).
        """
        count = sum(
            1 for r in checkpoint.syscall_log if r.request.get("tool_name") == MEM_WRITE
        )
        self._msg_seq = count

    # ── Public API ──

    @property
    def kernel_tool_names(self) -> set[str]:
        # Memory syscalls are agent-observable and replay from journal
        # cache (ReplayHit). They are NOT kernel-internal — returning
        # them here would cause decide_syscall to SKIP them during
        # replay, leading to re-execution and duplicate side effects.
        return set()

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
        # Ensure seq counter is in sync with checkpoint (survives restart)
        self.sync_seq(checkpoint)

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

    def register_message(self, msg: CastorMessage) -> None:
        if msg.id:
            self._entries[msg.id] = msg

    def _entry(
        self, checkpoint: AgentCheckpoint, memory_id: str
    ) -> CastorMessage | None:
        return self.find_by_id(checkpoint, memory_id) or self._entries.get(memory_id)

    def _direct_derivers(
        self, checkpoint: AgentCheckpoint, memory_id: str
    ) -> list[str]:
        candidates = list(self._entries.values()) + [
            entry
            for entry in checkpoint.context_history
            if isinstance(entry, CastorMessage) and entry.id not in self._entries
        ]
        return sorted(
            {
                entry.id
                for entry in candidates
                if entry.id
                and any(
                    isinstance(ref, MemoryRef) and ref.memory_id == memory_id
                    for ref in entry.depends_on
                )
            }
        )

    def eviction_result(
        self, checkpoint: AgentCheckpoint, memory_id: str, cascade: CascadeMode | str
    ) -> dict[str, Any]:
        mode = CascadeMode(cascade)
        derivers = self._direct_derivers(checkpoint, memory_id)
        if mode is CascadeMode.FORBID and derivers:
            return {"evicted": [], "orphaned": derivers, "refused": True}
        if mode is CascadeMode.WARN:
            return {"evicted": [memory_id], "orphaned": derivers, "refused": False}

        ordered: list[str] = []
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visited:
                return
            visited.add(node)
            for child in self._direct_derivers(checkpoint, node):
                visit(child)
            ordered.append(node)

        visit(memory_id)
        return {"evicted": ordered, "orphaned": [], "refused": False}

    def provenance_graph(
        self,
        checkpoint: AgentCheckpoint,
        memory_id: str,
        direction: str = "both",
        max_depth: int = 5,
    ) -> ProvenanceGraph:
        if direction not in {"sources", "derivers", "both"}:
            raise ValueError("direction must be sources, derivers, or both")
        if max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        nodes: dict[str, ProvenanceNode] = {}
        edges: list[tuple[str, str]] = []
        queue: list[tuple[str, int]] = [(memory_id, 0)]
        visited: set[str] = set()
        truncated = False

        def add_memory(mid: str) -> CastorMessage | None:
            entry = self._entry(checkpoint, mid)
            if entry is not None:
                nodes[mid] = ProvenanceNode(
                    ref=MemoryRef(memory_id=mid),
                    trust=entry.source_trust,
                    reason=entry.reason,
                    truncated_content=str(entry.content)[:256],
                )
            return entry

        while queue:
            current, depth = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            entry = add_memory(current)
            if entry is None:
                continue
            source_refs = entry.depends_on if direction in {"sources", "both"} else []
            deriver_ids = (
                self._direct_derivers(checkpoint, current)
                if direction in {"derivers", "both"}
                else []
            )
            neighbors: list[tuple[str, ProvenanceRef, bool]] = []
            for ref in source_refs:
                target = ref.memory_id if isinstance(ref, MemoryRef) else ref.uri
                neighbors.append((target, ref, True))
            for target in deriver_ids:
                neighbors.append((target, MemoryRef(memory_id=target), False))
            for target, ref, is_source in neighbors:
                if depth >= max_depth:
                    truncated = True
                    continue
                edges.append((current, target) if is_source else (target, current))
                if isinstance(ref, ExternalSource):
                    nodes[target] = ProvenanceNode(ref=ref, trust=entry.source_trust)
                else:
                    queue.append((target, depth + 1))

        return ProvenanceGraph(
            root=memory_id,
            direction=direction,
            nodes=nodes,
            edges=edges,
            truncated_at_max_depth=truncated,
        )

    def explain(
        self, checkpoint: AgentCheckpoint, memory_id: str, style: str, max_depth: int
    ) -> str:
        graph = self.provenance_graph(checkpoint, memory_id, "sources", max_depth)
        root = graph.nodes.get(memory_id)
        if root is None:
            return ""
        if style == "summary":
            return root.reason or root.truncated_content
        lines = [root.truncated_content]
        children: dict[str, list[str]] = {}
        for source, target in graph.edges:
            children.setdefault(source, []).append(target)

        def render(node: str, indent: int, seen: set[str]) -> None:
            for child in children.get(node, []):
                if child in seen:
                    continue
                label = graph.nodes[child].truncated_content or child
                lines.append(f"{'  ' * indent}because {label}")
                render(child, indent + 1, seen | {child})

        render(memory_id, 1, {memory_id})
        return "\n".join(lines)

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
        """Insert a promoted message into context.

        For 0 or 1 items: append (so promoted goes AFTER system prompt).
        For 2+ items: insert before the last entry (the latest user
        query), so the LLM sees the recalled context in the right spot.
        """
        n = len(checkpoint.context_history)
        if n <= 1:
            checkpoint.context_history.append(msg)
        else:
            checkpoint.context_history.insert(n - 1, msg)

    def apply_write(self, checkpoint: AgentCheckpoint, msg: CastorMessage) -> None:
        """Append a new message to the end of context_history."""
        checkpoint.context_history.append(msg)
        self.register_message(msg)

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
            content: str = "",
            metadata: dict | None = None,
            pin: bool = False,
            role: str = "memory",
            depends_on: list[dict[str, Any]] | None = None,
            superseding: str | None = None,
            source_trust: float = 1.0,
            reason: str = "",
        ) -> dict:
            return {
                "content": content,
                "metadata": metadata,
                "pin": pin,
                "role": role,
                "depends_on": depends_on,
                "superseding": superseding,
                "source_trust": max(0.0, min(1.0, source_trust)),
                "reason": reason,
            }

        registry.register(
            ToolMetadata(
                tool_name=MEM_WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "metadata": {"type": "object"},
                        "pin": {"type": "boolean"},
                        "role": {"type": "string"},
                        "depends_on": {"type": "array"},
                        "superseding": {"type": "string"},
                        "source_trust": {"type": "number"},
                        "reason": {"type": "string"},
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

        async def _mem_evict(memory_id: str = "", cascade: str = "warn") -> dict:
            return self.eviction_result(self._active_checkpoint, memory_id, cascade)

        registry.register(
            ToolMetadata(
                tool_name=MEM_EVICT,
                input_schema={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "cascade": {
                            "type": "string",
                            "enum": ["forbid", "warn", "cascade"],
                        },
                    },
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

        async def _mem_provenance(
            memory_id: str = "", direction: str = "both", max_depth: int = 5
        ) -> dict:
            return self.provenance_graph(
                self._active_checkpoint, memory_id, direction, max_depth
            ).model_dump(mode="json")

        registry.register(
            ToolMetadata(
                tool_name=MEM_PROVENANCE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "direction": {
                            "type": "string",
                            "enum": ["sources", "derivers", "both"],
                        },
                        "max_depth": {"type": "integer"},
                    },
                    "required": ["memory_id"],
                },
                func=_mem_provenance,
                is_async=True,
                cost_per_use=0.0002,
            )
        )

        async def _mem_explain(
            memory_id: str = "", style: str = "summary", max_depth: int = 5
        ) -> str:
            if style not in {"chain", "tree", "summary"}:
                raise ValueError("style must be chain, tree, or summary")
            return self.explain(self._active_checkpoint, memory_id, style, max_depth)

        registry.register(
            ToolMetadata(
                tool_name=MEM_EXPLAIN,
                input_schema={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "style": {
                            "type": "string",
                            "enum": ["chain", "tree", "summary"],
                        },
                        "max_depth": {"type": "integer"},
                    },
                    "required": ["memory_id"],
                },
                func=_mem_explain,
                is_async=True,
                cost_per_use=0.0002,
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
