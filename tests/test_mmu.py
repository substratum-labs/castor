"""Unit tests for MMU: token counting, eviction logic, pause/resume."""

from __future__ import annotations

import pytest

from castor.budget.manager import BudgetManager
from castor.gate.registry import ToolRegistry
from castor.mmu.cold_storage import InMemoryColdStorage
from castor.mmu.core import MEM_EVICT, MEM_PIN, MEM_RECALL, MEM_STORE, MMU
from castor.mmu.token_counter import CharCountEstimator, TokenCounter
from castor.models.checkpoint import AgentCheckpoint, CastorMessage

# ── TokenCounter ──


class TestCharCountEstimator:
    def test_basic_counting(self):
        counter = CharCountEstimator()
        assert counter.count("hello world") == max(1, len("hello world") // 4)

    def test_empty_string(self):
        counter = CharCountEstimator()
        assert counter.count("") == 1  # max(1, 0)

    def test_satisfies_protocol(self):
        counter = CharCountEstimator()
        assert isinstance(counter, TokenCounter)


# ── CastorMessage ──


class TestCastorMessage:
    def test_defaults(self):
        msg = CastorMessage(role="user", content="hello")
        assert msg.pinned is False
        assert msg.token_count == 0

    def test_pinned(self):
        msg = CastorMessage(role="system", content="you are helpful", pinned=True)
        assert msg.pinned is True

    def test_serialization(self):
        msg = CastorMessage(role="user", content="test", token_count=10)
        data = msg.model_dump()
        restored = CastorMessage.model_validate(data)
        assert restored == msg


# ── MMU helpers ──


def _make_mmu(watermark: int = 100) -> tuple[MMU, InMemoryColdStorage, ToolRegistry]:
    registry = ToolRegistry()
    cold = InMemoryColdStorage()
    mmu = MMU(
        registry,
        cold_storage=cold,
        hard_watermark=watermark,
        agent_id="test-agent",
    )
    return mmu, cold, registry


def _make_checkpoint(
    messages: list[CastorMessage] | None = None,
) -> AgentCheckpoint:
    budget_mgr = BudgetManager()
    caps = budget_mgr.create_budgets({"system": 100.0})
    cp = AgentCheckpoint(
        pid="mmu-test-001",
        status="RUNNING",
        agent_function_name="test_agent",
        capabilities=caps,
    )
    if messages:
        cp.context_history = list(messages)
    return cp


# ── Token counting ──


class TestMMUTokenCounting:
    def test_total_tokens_uses_token_count_field(self):
        mmu, _, _ = _make_mmu()
        cp = _make_checkpoint(
            [
                CastorMessage(role="user", content="x", token_count=50),
                CastorMessage(role="assistant", content="y", token_count=30),
            ]
        )
        assert mmu.total_tokens(cp) == 80

    def test_total_tokens_falls_back_to_counter(self):
        mmu, _, _ = _make_mmu()
        cp = _make_checkpoint(
            [CastorMessage(role="user", content="a" * 40)]  # ~10 tokens
        )
        assert mmu.total_tokens(cp) == 10

    def test_plain_dicts_ignored(self):
        mmu, _, _ = _make_mmu()
        cp = _make_checkpoint()
        cp.context_history = [
            CastorMessage(role="user", content="x", token_count=10),
            {"role": "system", "content": "not counted"},
        ]
        assert mmu.total_tokens(cp) == 10


# ── FIFO selection ──


class TestFIFOSelection:
    def test_selects_oldest_non_pinned(self):
        mmu, _, _ = _make_mmu(watermark=20)
        cp = _make_checkpoint(
            [
                CastorMessage(
                    role="system", content="sys", pinned=True, token_count=10
                ),
                CastorMessage(role="user", content="old", token_count=15),
                CastorMessage(role="user", content="new", token_count=15),
            ]
        )
        indices = mmu._fifo_select(cp, target=20)
        assert 0 not in indices  # pinned
        assert 1 in indices  # oldest non-pinned

    def test_pinned_never_selected(self):
        mmu, _, _ = _make_mmu(watermark=5)
        cp = _make_checkpoint(
            [
                CastorMessage(role="system", content="x" * 100, pinned=True),
                CastorMessage(role="user", content="y" * 100, pinned=True),
            ]
        )
        indices = mmu._fifo_select(cp, target=5)
        assert indices == []

    def test_empty_history(self):
        mmu, _, _ = _make_mmu()
        cp = _make_checkpoint()
        indices = mmu._fifo_select(cp, target=0)
        assert indices == []


# ── Apply methods ──


class TestApplyMethods:
    def test_apply_eviction_removes_by_index(self):
        mmu, _, _ = _make_mmu()
        cp = _make_checkpoint(
            [
                CastorMessage(role="user", content="a", token_count=10),
                CastorMessage(role="user", content="b", token_count=10),
                CastorMessage(role="user", content="c", token_count=10),
            ]
        )
        removed = mmu.apply_eviction(cp, [0, 2])
        assert len(cp.context_history) == 1
        assert cp.context_history[0].content == "b"
        assert len(removed) == 2
        assert removed[0].content == "a"
        assert removed[1].content == "c"

    def test_apply_pin(self):
        mmu, _, _ = _make_mmu()
        cp = _make_checkpoint([CastorMessage(role="user", content="x")])
        assert not cp.context_history[0].pinned
        mmu.apply_pin(cp, 0)
        assert cp.context_history[0].pinned

    def test_apply_recall_inserts_before_last(self):
        mmu, _, _ = _make_mmu()
        cp = _make_checkpoint(
            [
                CastorMessage(role="user", content="first"),
                CastorMessage(role="user", content="last"),
            ]
        )
        mmu.apply_recall(
            cp,
            [{"role": "system", "content": "recalled fact"}],
        )
        assert len(cp.context_history) == 3
        assert cp.context_history[1].content == "recalled fact"
        assert cp.context_history[2].content == "last"

    @pytest.mark.asyncio
    async def test_persist_evicted_stores_to_cold(self):
        mmu, cold, _ = _make_mmu()
        msgs = [CastorMessage(role="user", content="evicted data")]
        await mmu.persist_evicted(msgs, summary="test summary")
        results = await cold.search("test-agent", "evicted")
        assert len(results) >= 1


# ── Pause / Resume ──


class TestPauseResume:
    def test_pause_stops_auto_evict(self):
        mmu, _, _ = _make_mmu()
        mmu.pause_auto_evict()
        assert mmu._auto_evict_paused is True

    def test_resume_restores_auto_evict(self):
        mmu, _, _ = _make_mmu()
        mmu.pause_auto_evict()
        mmu.resume_auto_evict()
        assert mmu._auto_evict_paused is False

    def test_resume_idempotent(self):
        mmu, _, _ = _make_mmu()
        mmu.resume_auto_evict()  # already not paused
        assert mmu._auto_evict_paused is False


# ── Tool registration ──


class TestToolRegistration:
    def test_all_four_tools_registered(self):
        _, _, registry = _make_mmu()
        names = set(registry.list_tools())
        assert MEM_EVICT in names
        assert MEM_RECALL in names
        assert MEM_PIN in names
        assert MEM_STORE in names

    def test_kernel_tool_names_contains_all_four(self):
        mmu, _, _ = _make_mmu()
        assert mmu.kernel_tool_names == {MEM_EVICT, MEM_RECALL, MEM_PIN, MEM_STORE}


# ── on_session_end ──


class TestOnSessionEnd:
    @pytest.mark.asyncio
    async def test_calls_policy_on_session_end(self):
        from unittest.mock import AsyncMock

        mmu, _, _ = _make_mmu()
        mmu._policy.on_session_end = AsyncMock()
        await mmu.on_session_end([], [])
        mmu._policy.on_session_end.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exception_logged_not_raised(self):
        from unittest.mock import AsyncMock

        mmu, _, _ = _make_mmu()
        mmu._policy.on_session_end = AsyncMock(side_effect=RuntimeError("boom"))
        # Should NOT raise
        await mmu.on_session_end([], [])
