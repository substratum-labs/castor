"""Per-agent Mnemos context lifecycle management."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mnemos.client import MnemosClient
    from mnemos.models.context import ContextHandle


class ContextLifecycleManager:
    """Manages Mnemos ContextHandle per Castor agent (keyed by pid).

    For M2 simplicity, lifecycle is process-level (not persisted to
    checkpoint). On Castor restart, contexts are re-created.

    The manager auto-creates a context on first access for a given pid,
    reuses it for subsequent calls, and provides explicit drop on agent
    completion.
    """

    def __init__(self, client: MnemosClient) -> None:
        self._client = client
        self._handles: dict[str, ContextHandle] = {}

    async def get_or_create(
        self,
        pid: str,
        model_id: str,
        max_tokens: int,
    ) -> ContextHandle:
        """Return existing handle for pid, or create a new one."""
        if pid in self._handles:
            return self._handles[pid]

        from mnemos.models.context import ContextConfig

        handle = await self._client.create_context(
            ContextConfig(model_id=model_id, max_tokens=max_tokens)
        )
        self._handles[pid] = handle
        return handle

    async def drop(self, pid: str) -> None:
        """Drop the context for this pid (idempotent)."""
        handle = self._handles.pop(pid, None)
        if handle is not None:
            await self._client.drop_context(handle)

    async def drop_all(self) -> None:
        """Drop all managed contexts."""
        for pid in list(self._handles.keys()):
            await self.drop(pid)

    def has(self, pid: str) -> bool:
        return pid in self._handles

    def get(self, pid: str) -> ContextHandle | None:
        return self._handles.get(pid)
