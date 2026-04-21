"""Cold storage backends for evicted / explicit memory.

AISA §2.2 shape — messages are addressed by ``memory_id`` and support
``read``, ``search``, ``delete`` alongside ``store`` and ``store_explicit``.

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


def _extract_id(msg: Any) -> str:
    if hasattr(msg, "id"):
        return msg.id
    if isinstance(msg, dict):
        return msg.get("id", "")
    return ""


class InMemoryColdStorage:
    """Dict-backed cold storage for testing.

    Stores entries in ``agent_id → {memory_id → entry}``. Search is
    brute-force substring matching — adequate for tests only.
    """

    def __init__(self) -> None:
        self._store: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    async def store(
        self,
        agent_id: str,
        messages: list[Any],
        summary: str | None = None,
        source: str = "eviction",
    ) -> None:
        for msg in messages:
            mid = _extract_id(msg)
            content = _extract_content(msg)
            role = _extract_role(msg)
            entry = {
                "memory_id": mid,
                "content": content,
                "source": source,
                "summary": summary,
                "role": role,
                "metadata": {},
            }
            key = mid or f"_anon_{len(self._store[agent_id])}"
            self._store[agent_id][key] = entry

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
        limit: int = 5,
        filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        entries = self._store.get(agent_id, {})
        results: list[dict[str, Any]] = []
        query_lower = query.lower()

        for entry in entries.values():
            # Apply metadata filter
            if filter:
                skip = False
                for k, v in filter.items():
                    if entry.get(k) != v and entry.get("metadata", {}).get(k) != v:
                        skip = True
                        break
                if skip:
                    continue

            content = entry.get("content", "")
            summary = entry.get("summary", "") or ""
            if query_lower in content.lower() or query_lower in summary.lower():
                results.append(
                    {
                        "memory_id": entry.get("memory_id", ""),
                        "content": content,
                        "score": 1.0,
                        "metadata": entry.get("metadata", {}),
                    }
                )
            if len(results) >= limit:
                break

        return results

    async def read(
        self,
        agent_id: str,
        memory_id: str,
    ) -> dict[str, Any] | None:
        entries = self._store.get(agent_id, {})
        entry = entries.get(memory_id)
        if entry is None:
            return None
        return {
            "memory_id": entry.get("memory_id", memory_id),
            "content": entry.get("content", ""),
            "role": entry.get("role", "unknown"),
            "metadata": entry.get("metadata", {}),
            "source": entry.get("source", ""),
        }

    async def delete(
        self,
        agent_id: str,
        memory_id: str,
    ) -> bool:
        entries = self._store.get(agent_id, {})
        if memory_id in entries:
            del entries[memory_id]
            return True
        return False

    async def store_explicit(
        self,
        agent_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        memory_id: str | None = None,
    ) -> None:
        key = memory_id or f"_explicit_{len(self._store[agent_id])}"
        entry = {
            "memory_id": key,
            "content": content,
            "source": "explicit",
            "summary": None,
            "role": "memory",
            "metadata": metadata or {},
        }
        self._store[agent_id][key] = entry
        logger.debug(
            "cold_store_explicit agent=%s id=%s len=%d",
            agent_id,
            key,
            len(content),
        )

    def clear(self, agent_id: str | None = None) -> None:
        if agent_id is None:
            self._store.clear()
        else:
            self._store.pop(agent_id, None)

    def count(self, agent_id: str) -> int:
        return len(self._store.get(agent_id, {}))
