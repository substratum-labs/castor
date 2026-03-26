"""Tests for InMemoryJournal and JournalProtocol conformance."""

from __future__ import annotations

from castor.kernel.journal import InMemoryJournal
from castor.models.checkpoint import SyscallRecord


def _record(tool: str = "test", response: str = "ok") -> SyscallRecord:
    return SyscallRecord(
        request={"tool_name": tool, "arguments": {}},
        response=response,
    )


class TestInMemoryJournal:
    def test_conforms_to_protocol(self):
        journal = InMemoryJournal([])
        # runtime_checkable doesn't check __len__, so verify manually
        assert hasattr(journal, "append")
        assert hasattr(journal, "get")
        assert hasattr(journal, "__len__")
        assert hasattr(journal, "scan_from")

    def test_empty(self):
        journal = InMemoryJournal([])
        assert len(journal) == 0

    def test_append_returns_index(self):
        journal = InMemoryJournal([])
        idx0 = journal.append(_record("a"))
        idx1 = journal.append(_record("b"))
        assert idx0 == 0
        assert idx1 == 1
        assert len(journal) == 2

    def test_get(self):
        journal = InMemoryJournal([])
        journal.append(_record("a", "result_a"))
        journal.append(_record("b", "result_b"))
        assert journal.get(0).response == "result_a"
        assert journal.get(1).response == "result_b"

    def test_scan_from_beginning(self):
        journal = InMemoryJournal([])
        journal.append(_record("a"))
        journal.append(_record("b"))
        journal.append(_record("c"))
        entries = list(journal.scan_from(0))
        assert len(entries) == 3
        assert entries[0] == (0, journal.get(0))
        assert entries[2] == (2, journal.get(2))

    def test_scan_from_middle(self):
        journal = InMemoryJournal([])
        journal.append(_record("a"))
        journal.append(_record("b"))
        journal.append(_record("c"))
        entries = list(journal.scan_from(2))
        assert len(entries) == 1
        assert entries[0][0] == 2

    def test_scan_from_past_end(self):
        journal = InMemoryJournal([])
        journal.append(_record("a"))
        entries = list(journal.scan_from(5))
        assert entries == []

    def test_wraps_existing_list(self):
        """InMemoryJournal wraps the same list object — zero-copy."""
        log: list[SyscallRecord] = []
        journal = InMemoryJournal(log)
        journal.append(_record("a"))
        assert len(log) == 1
        assert log[0].request["tool_name"] == "a"

    def test_pre_populated_list(self):
        """Journal created from a list that already has records."""
        log = [_record("a"), _record("b")]
        journal = InMemoryJournal(log)
        assert len(journal) == 2
        assert journal.get(0).request["tool_name"] == "a"
        journal.append(_record("c"))
        assert len(journal) == 3
        assert len(log) == 3
