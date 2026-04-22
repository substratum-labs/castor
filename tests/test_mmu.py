"""Unit tests for MMU: token counting, ID-based addressing, pause/resume.

AISA §2.2 shape — all 7 syscalls with memory_id addressing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from castor.budget.manager import BudgetManager
from castor.gate.registry import ToolRegistry
from castor.mmu.cold_storage import InMemoryColdStorage
from castor.mmu.core import (
    MEM_DELETE,
    MEM_EVICT,
    MEM_PROMOTE,
    MEM_PROTECT,
    MEM_READ,
    MEM_SEARCH,
    MEM_WRITE,
    MMU,
)
from castor.mmu.token_counter import CharCountEstimator, TokenCounter
from castor.models.checkpoint import AgentCheckpoint, CastorMessage, compute_memory_id


class TestCharCountEstimator:
    def test_basic(self):
        assert CharCountEstimator().count("hello world") == max(1, 11 // 4)

    def test_empty(self):
        assert CharCountEstimator().count("") == 1

    def test_protocol(self):
        assert isinstance(CharCountEstimator(), TokenCounter)


class TestCastorMessage:
    def test_defaults(self):
        msg = CastorMessage(role="user", content="hello")
        assert msg.pinned is False and msg.id == ""

    def test_with_id(self):
        assert CastorMessage(id="abc", role="user", content="x").id == "abc"

    def test_roundtrip(self):
        msg = CastorMessage(id="x", role="user", content="t", token_count=10)
        assert CastorMessage.model_validate(msg.model_dump()) == msg


class TestComputeMemoryId:
    def test_deterministic(self):
        a = compute_memory_id("p", 0, "user", "hi")
        assert a == compute_memory_id("p", 0, "user", "hi")
        assert len(a) == 32

    def test_different_inputs(self):
        base = compute_memory_id("p", 0, "user", "hi")
        assert compute_memory_id("q", 0, "user", "hi") != base
        assert compute_memory_id("p", 1, "user", "hi") != base
        assert compute_memory_id("p", 0, "user", "bye") != base


def _make_mmu(wm: int = 100):
    reg = ToolRegistry()
    cold = InMemoryColdStorage()
    mmu = MMU(reg, cold_storage=cold, hard_watermark=wm, agent_id="t")
    return mmu, cold, reg


def _make_cp(msgs=None):
    bm = BudgetManager()
    cp = AgentCheckpoint(
        pid="test",
        status="RUNNING",
        agent_function_name="a",
        capabilities=bm.create_budgets({"s": 100}),
    )
    if msgs:
        cp.context_history = list(msgs)
    return cp


class TestTokenCounting:
    def test_field(self):
        mmu, _, _ = _make_mmu()
        cp = _make_cp([CastorMessage(id="a", role="u", content="x", token_count=50)])
        assert mmu.total_tokens(cp) == 50

    def test_fallback(self):
        mmu, _, _ = _make_mmu()
        cp = _make_cp([CastorMessage(id="a", role="u", content="a" * 40)])
        assert mmu.total_tokens(cp) == 10


class TestFIFOSelectIds:
    def test_skips_pinned(self):
        mmu, _, _ = _make_mmu(wm=20)
        cp = _make_cp(
            [
                CastorMessage(
                    id="s", role="sys", content="s", pinned=True, token_count=10
                ),
                CastorMessage(id="old", role="u", content="o", token_count=15),
            ]
        )
        ids = mmu._fifo_select_ids(cp, target=10)
        assert "s" not in ids and "old" in ids


class TestApplyMethods:
    def test_eviction_by_id(self):
        mmu, _, _ = _make_mmu()
        cp = _make_cp(
            [
                CastorMessage(id="a", role="u", content="1"),
                CastorMessage(id="b", role="u", content="2"),
            ]
        )
        r = mmu.apply_eviction(cp, "a")
        assert r.content == "1" and len(cp.context_history) == 1

    def test_protect(self):
        mmu, _, _ = _make_mmu()
        cp = _make_cp([CastorMessage(id="x", role="u", content="t")])
        mmu.apply_protect(cp, "x", True)
        assert cp.context_history[0].pinned
        mmu.apply_protect(cp, "x", False)
        assert not cp.context_history[0].pinned

    def test_write(self):
        mmu, _, _ = _make_mmu()
        cp = _make_cp()
        mmu.apply_write(cp, CastorMessage(id="n", role="memory", content="f"))
        assert cp.context_history[0].id == "n"

    def test_promote_inserts(self):
        mmu, _, _ = _make_mmu()
        cp = _make_cp(
            [
                CastorMessage(id="a", role="u", content="first"),
                CastorMessage(id="b", role="u", content="last"),
            ]
        )
        mmu.apply_promote(cp, CastorMessage(id="r", role="sys", content="recalled"))
        assert len(cp.context_history) == 3
        assert cp.context_history[1].id == "r"

    @pytest.mark.asyncio
    async def test_persist_evicted(self):
        mmu, cold, _ = _make_mmu()
        await mmu.persist_evicted(CastorMessage(id="e", role="u", content="data"))
        assert cold.count("t") >= 1


class TestToolRegistration:
    def test_all_seven(self):
        _, _, reg = _make_mmu()
        names = set(reg.list_tools())
        for n in [
            MEM_WRITE,
            MEM_READ,
            MEM_SEARCH,
            MEM_DELETE,
            MEM_EVICT,
            MEM_PROMOTE,
            MEM_PROTECT,
        ]:
            assert n in names

    def test_kernel_names(self):
        mmu, _, _ = _make_mmu()
        assert len(mmu.kernel_tool_names) == 0


class TestPauseResume:
    def test_pause(self):
        mmu, _, _ = _make_mmu()
        mmu.pause_auto_evict()
        assert mmu._auto_evict_paused

    def test_resume_idempotent(self):
        mmu, _, _ = _make_mmu()
        mmu.resume_auto_evict()
        assert not mmu._auto_evict_paused


class TestOnSessionEnd:
    @pytest.mark.asyncio
    async def test_calls_policy(self):
        mmu, _, _ = _make_mmu()
        mmu._policy.on_session_end = AsyncMock()
        await mmu.on_session_end([], [])
        mmu._policy.on_session_end.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exception_swallowed(self):
        mmu, _, _ = _make_mmu()
        mmu._policy.on_session_end = AsyncMock(side_effect=RuntimeError("x"))
        await mmu.on_session_end([], [])
