"""Cold storage backends for evicted / explicit memory.

Level 0: ``InMemoryColdStorage`` — dict-backed, for testing.
Level 1: SQLite-backed or vector DB backends (future).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger("castor.mmu.cold_storage")


def _extract_content(msg: Any) -> str:
    if hasattr(msg, "content"):
        c = msg.content
        return c if isinstance(c, str) else str(c)
    if isinstance(msg, dict):
        return str(msg.get("content", ""))
    return ""


def _extract_role(msg: Any) -> str:
    if hasattr(msg, "role"):
        return msg.role
    if isinstance(msg, dict):
        return msg.get("role", "unknown")
    return "unknown"


class InMemoryColdStorage:
    """Dict-backed cold storage for testing.

    Stores entries in a nested dict: ``agent_id → list[entry]``.
    Search is brute-force substring matching on content — adequate for
    tests but not for production. Production deployments should use a
    vector-backed implementation (ChromaDB, sqlite-vec, Qdrant, etc.).
    """

    def __init__(self) -> None:
        self._store: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def store(
        self,
        agent_id: str,
        messages: list[Any],
        summary: str | None = None,
        source: str = "eviction",
    ) -> None:
        for msg in messages:
            content = _extract_content(msg)
            role = _extract_role(msg)
            entry = {
                "content": content,
                "source": source,
                "summary": summary,
                "role": role,
            }
            self._store[agent_id].append(entry)

        logger.debug(
            "cold_store agent=%s count=%d source=%s",
            agent_id,
            len(messages),
            source,
        )

    async def search(
        self,
        agent_id: str,
        query: str,
        max_results: int = 5,
        source_filter: str | None = None,
    ) -> list[Any]:
        entries = self._store.get(agent_id, [])
        results = []
        query_lower = query.lower()
        for entry in entries:
            if source_filter and entry.get("source") != source_filter:
                continue
            if query_lower in entry.get("content", "").lower():
                results.append(entry)
            elif entry.get("summary") and query_lower in entry["summary"].lower():
                results.append(entry)
            if len(results) >= max_results:
                break
        return results

    async def store_explicit(
        self,
        agent_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "content": content,
            "source": "explicit",
            "summary": None,
            "role": "memory",
            "metadata": metadata or {},
        }
        self._store[agent_id].append(entry)
        logger.debug("cold_store_explicit agent=%s len=%d", agent_id, len(content))

    def clear(self, agent_id: str | None = None) -> None:
        """Clear stored entries. If ``agent_id`` is None, clear all."""
        if agent_id is None:
            self._store.clear()
        else:
            self._store.pop(agent_id, None)

    def count(self, agent_id: str) -> int:
        """Return the number of entries for an agent."""
        return len(self._store.get(agent_id, []))
