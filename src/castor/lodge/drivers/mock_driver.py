"""In-memory mock driver for Lodge testing."""

from __future__ import annotations

import json
from typing import Any

from castor.lodge.driver import SemanticMemoryDriver


class InMemoryDriver(SemanticMemoryDriver):
    """Dict-based storage with substring search. For testing only."""

    def __init__(self) -> None:
        self._store: dict[str, list[str]] = {}

    async def ingest(self, messages: list[dict[str, Any]], pid: str) -> str:
        entries = self._store.setdefault(pid, [])
        for msg in messages:
            entries.append(json.dumps(msg, ensure_ascii=False))
        return f"Ingested {len(messages)} messages for pid={pid}."

    async def search(self, query: str, pid: str) -> str:
        entries = self._store.get(pid, [])
        matches = [e for e in entries if query.lower() in e.lower()]
        if not matches:
            return "No matching memories found."
        return "\n".join(matches)
