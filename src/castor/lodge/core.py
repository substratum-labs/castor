"""CastorLodge: the MMU controller for LLM context windows."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from castor.dam.registry import ToolMetadata, ToolRegistry
from castor.lodge.driver import SemanticMemoryDriver
from castor.lodge.token_counter import CharCountEstimator, TokenCounter
from castor.models.checkpoint import AgentCheckpoint, CastorMessage

if TYPE_CHECKING:
    from castor.stream.proxy import SyscallProxy

PAGE_OUT_TOOL = "sys_kernel_page_out"
SEARCH_MEMORY_TOOL = "search_memory"


class CastorLodge:
    """Context window memory manager.

    Monitors ``context_history`` token usage and evicts unpinned messages
    to cold storage when the watermark is exceeded.  Eviction and search
    are routed through ``proxy.syscall()`` for replay safety.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        driver: SemanticMemoryDriver,
        token_counter: TokenCounter | None = None,
        watermark: int = 8000,
        consumes: str = "system",
        cost_per_use: float = 0.0,
    ) -> None:
        self._driver = driver
        self._counter: TokenCounter = token_counter or CharCountEstimator()
        self._watermark = watermark

        # Register page-out tool (closure captures driver)
        async def page_out_fn(messages_json: str) -> str:
            msgs: list[dict[str, Any]] = json.loads(messages_json)
            # pid is embedded in the messages payload by check_and_evict
            pid = msgs[0].get("_pid", "unknown") if msgs else "unknown"
            # Strip internal _pid field before passing to driver
            clean = [{k: v for k, v in m.items() if k != "_pid"} for m in msgs]
            return await driver.ingest(clean, pid)

        registry.register(
            ToolMetadata(
                tool_name=PAGE_OUT_TOOL,
                consumes=consumes,
                cost_per_use=cost_per_use,
                requires_hitl=False,
                destructive=False,
                input_schema={
                    "properties": {
                        "messages_json": {"type": "string"},
                    },
                    "required": ["messages_json"],
                    "type": "object",
                },
                func=page_out_fn,
                is_async=True,
            )
        )

        # Register search-memory tool (closure captures driver)
        async def search_fn(query: str, pid: str) -> str:
            return await driver.search(query, pid)

        registry.register(
            ToolMetadata(
                tool_name=SEARCH_MEMORY_TOOL,
                consumes=consumes,
                cost_per_use=cost_per_use,
                requires_hitl=False,
                destructive=False,
                input_schema={
                    "properties": {
                        "query": {"type": "string"},
                        "pid": {"type": "string"},
                    },
                    "required": ["query", "pid"],
                    "type": "object",
                },
                func=search_fn,
                is_async=True,
            )
        )

    @property
    def kernel_tool_names(self) -> set[str]:
        """Kernel-internal tools auto-skipped during replay."""
        return {PAGE_OUT_TOOL}

    def total_tokens(self, checkpoint: AgentCheckpoint) -> int:
        """Sum token counts of all CastorMessage entries in context_history."""
        total = 0
        for entry in checkpoint.context_history:
            if isinstance(entry, CastorMessage):
                if entry.token_count > 0:
                    total += entry.token_count
                else:
                    total += self._counter.count(entry.content)
            # Plain dicts are ignored — they don't participate in eviction
        return total

    def _select_victims(self, checkpoint: AgentCheckpoint) -> list[CastorMessage]:
        """Select unpinned messages for eviction using FIFO order.

        Removes oldest unpinned messages until total tokens <= watermark.
        """
        victims: list[CastorMessage] = []
        running_total = self.total_tokens(checkpoint)

        for entry in checkpoint.context_history:
            if running_total <= self._watermark:
                break
            if isinstance(entry, CastorMessage) and not entry.pinned:
                tokens = (
                    entry.token_count
                    if entry.token_count > 0
                    else self._counter.count(entry.content)
                )
                victims.append(entry)
                running_total -= tokens

        return victims

    async def check_and_evict(
        self, proxy: SyscallProxy, checkpoint: AgentCheckpoint
    ) -> None:
        """Check token usage and evict if over watermark.

        Eviction is performed as a syscall through the proxy, so it is
        automatically logged and replayed correctly.
        """
        if self.total_tokens(checkpoint) <= self._watermark:
            return

        victims = self._select_victims(checkpoint)
        if not victims:
            return

        # Serialize victims with pid tag for the driver
        payload = [{**v.model_dump(), "_pid": checkpoint.pid} for v in victims]
        await proxy.syscall(PAGE_OUT_TOOL, {"messages_json": json.dumps(payload)})

        # Remove evicted messages from context_history
        victim_set = set(id(v) for v in victims)
        checkpoint.context_history = [
            entry for entry in checkpoint.context_history if id(entry) not in victim_set
        ]
