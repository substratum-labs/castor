"""In-memory journal — Level 0 implementation.

Wraps ``checkpoint.syscall_log`` (a plain list) behind the
``JournalProtocol`` interface.  Zero-copy: the journal holds a
reference to the *same* list object, so checkpoint serialisation
works unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator

from castor.models.checkpoint import SyscallRecord


class InMemoryJournal:
    """Level 0 journal backed by a plain Python list."""

    __slots__ = ("_log",)

    def __init__(self, syscall_log: list[SyscallRecord]) -> None:
        self._log = syscall_log

    def append(self, record: SyscallRecord) -> int:
        """Append a record and return its index."""
        self._log.append(record)
        return len(self._log) - 1

    def get(self, index: int) -> SyscallRecord:
        """Get a record by index."""
        return self._log[index]

    def __len__(self) -> int:
        return len(self._log)

    def scan_from(self, index: int) -> Iterator[tuple[int, SyscallRecord]]:
        """Iterate ``(index, record)`` pairs starting from *index*."""
        for i in range(index, len(self._log)):
            yield i, self._log[i]
