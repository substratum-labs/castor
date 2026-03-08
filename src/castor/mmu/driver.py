"""SemanticMemoryDriver: HAL for Lodge cold storage backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SemanticMemoryDriver(ABC):
    """Abstract interface for Lodge's cold storage backend.

    Implementations may use vector databases (Pinecone, Qdrant),
    local embeddings (Mem0), or simple text stores.  Lodge core
    never imports a concrete driver — it only depends on this ABC.
    """

    @abstractmethod
    async def ingest(self, messages: list[dict[str, Any]], pid: str) -> str:
        """Store evicted messages in cold storage.

        Returns a confirmation string (logged in syscall_log).
        """

    @abstractmethod
    async def search(self, query: str, pid: str) -> str:
        """Search cold storage and return relevant content as text."""
