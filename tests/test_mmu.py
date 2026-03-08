"""Unit tests for MMU: token counting, drivers, and eviction logic."""

from __future__ import annotations

from castor.capability.manager import CapabilityManager
from castor.dam.registry import ToolRegistry
from castor.dam.validator import CastorDam
from castor.mmu.core import MMU, PAGE_OUT_TOOL, SEARCH_MEMORY_TOOL
from castor.mmu.drivers.mock_driver import InMemoryDriver
from castor.mmu.token_counter import CharCountEstimator, TokenCounter
from castor.models.checkpoint import AgentCheckpoint, CastorMessage
from castor.stream.proxy import SyscallProxy

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


# ── InMemoryDriver ──


class TestInMemoryDriver:
    async def test_ingest_and_search(self):
        driver = InMemoryDriver()
        await driver.ingest(
            [{"role": "user", "content": "battery tech breakthroughs"}],
            pid="agent-1",
        )
        result = await driver.search("battery", pid="agent-1")
        assert "battery" in result.lower()

    async def test_search_no_match(self):
        driver = InMemoryDriver()
        await driver.ingest(
            [{"role": "user", "content": "hello world"}],
            pid="agent-1",
        )
        result = await driver.search("quantum", pid="agent-1")
        assert "no matching" in result.lower()

    async def test_search_empty_store(self):
        driver = InMemoryDriver()
        result = await driver.search("anything", pid="agent-1")
        assert "no matching" in result.lower()

    async def test_multiple_pids_isolated(self):
        driver = InMemoryDriver()
        await driver.ingest([{"role": "user", "content": "alpha"}], pid="a")
        await driver.ingest([{"role": "user", "content": "beta"}], pid="b")
        assert "alpha" in (await driver.search("alpha", pid="a")).lower()
        assert "no matching" in (await driver.search("alpha", pid="b")).lower()


# ── MMU: eviction logic ──


def _make_lodge(
    registry: ToolRegistry,
    watermark: int = 100,
) -> tuple[MMU, InMemoryDriver]:
    driver = InMemoryDriver()
    lodge = MMU(
        registry, driver, watermark=watermark, consumes="system", cost_per_use=0.0
    )
    return lodge, driver


def _make_checkpoint(
    cap_mgr: CapabilityManager,
    messages: list[CastorMessage] | None = None,
) -> AgentCheckpoint:
    caps = cap_mgr.create_capabilities({"system": 100.0, "network": 100.0})
    cp = AgentCheckpoint(
        pid="lodge-test-001",
        status="RUNNING",
        agent_function_name="test_agent",
        capabilities=caps,
    )
    if messages:
        cp.context_history = list(messages)
    return cp


class TestMMUEviction:
    def test_total_tokens_uses_token_count_field(self):
        registry = ToolRegistry()
        lodge, _ = _make_lodge(registry, watermark=100)
        cap_mgr = CapabilityManager()
        cp = _make_checkpoint(
            cap_mgr,
            [
                CastorMessage(role="user", content="x", token_count=50),
                CastorMessage(role="assistant", content="y", token_count=30),
            ],
        )
        assert lodge.total_tokens(cp) == 80

    def test_total_tokens_falls_back_to_counter(self):
        registry = ToolRegistry()
        lodge, _ = _make_lodge(registry, watermark=100)
        cap_mgr = CapabilityManager()
        cp = _make_checkpoint(
            cap_mgr,
            [
                CastorMessage(role="user", content="a" * 40),  # ~10 tokens
            ],
        )
        assert lodge.total_tokens(cp) == 10

    def test_select_victims_fifo_unpinned(self):
        registry = ToolRegistry()
        lodge, _ = _make_lodge(registry, watermark=20)
        cap_mgr = CapabilityManager()
        cp = _make_checkpoint(
            cap_mgr,
            [
                CastorMessage(
                    role="system", content="sys", pinned=True, token_count=10
                ),
                CastorMessage(role="user", content="old", token_count=15),
                CastorMessage(role="user", content="new", token_count=15),
            ],
        )
        victims = lodge._select_victims(cp)
        assert len(victims) >= 1
        assert all(not v.pinned for v in victims)
        assert victims[0].content == "old"

    def test_pinned_never_evicted(self):
        registry = ToolRegistry()
        lodge, _ = _make_lodge(registry, watermark=5)
        cap_mgr = CapabilityManager()
        cp = _make_checkpoint(
            cap_mgr,
            [
                CastorMessage(
                    role="system",
                    content="important",
                    pinned=True,
                    token_count=50,
                ),
            ],
        )
        victims = lodge._select_victims(cp)
        assert len(victims) == 0

    def test_under_watermark_no_eviction(self):
        registry = ToolRegistry()
        lodge, _ = _make_lodge(registry, watermark=1000)
        cap_mgr = CapabilityManager()
        cp = _make_checkpoint(
            cap_mgr,
            [
                CastorMessage(role="user", content="small", token_count=5),
            ],
        )
        assert lodge.total_tokens(cp) <= lodge._watermark
        victims = lodge._select_victims(cp)
        assert len(victims) == 0


# ── MMU: eviction via syscall ──


class TestEvictionViaSyscall:
    async def test_eviction_produces_syscall_record(self):
        """Eviction goes through proxy.syscall() and produces a SyscallRecord."""
        registry = ToolRegistry()
        lodge, driver = _make_lodge(registry, watermark=10)
        dam = CastorDam(registry)
        cap_mgr = CapabilityManager()

        cp = _make_checkpoint(
            cap_mgr,
            [
                CastorMessage(
                    role="system", content="pinned", pinned=True, token_count=5
                ),
                CastorMessage(role="user", content="old message", token_count=20),
            ],
        )

        proxy = SyscallProxy(cp, dam, cap_mgr, lodge=lodge)
        await lodge.check_and_evict(proxy, cp)

        # Should have logged a sys_kernel_page_out syscall
        assert len(cp.syscall_log) == 1
        assert cp.syscall_log[0].request["tool_name"] == PAGE_OUT_TOOL

        # Pinned message survives, unpinned was evicted
        assert len(cp.context_history) == 1
        assert cp.context_history[0].pinned is True  # type: ignore[union-attr]

        # Driver received the evicted message
        stored = await driver.search("old message", pid="lodge-test-001")
        assert "old message" in stored.lower()

    async def test_search_memory_via_syscall(self):
        """search_memory tool routes through proxy and returns results."""
        registry = ToolRegistry()
        lodge, driver = _make_lodge(registry, watermark=10)
        dam = CastorDam(registry)
        cap_mgr = CapabilityManager()

        # Pre-populate driver with some data
        await driver.ingest(
            [{"role": "user", "content": "quantum computing advances"}],
            pid="lodge-test-001",
        )

        cp = _make_checkpoint(cap_mgr)
        proxy = SyscallProxy(cp, dam, cap_mgr, lodge=lodge)

        result = await proxy.syscall(
            SEARCH_MEMORY_TOOL,
            {"query": "quantum", "pid": "lodge-test-001"},
        )
        assert "quantum" in result.lower()
        assert len(cp.syscall_log) == 1
        assert cp.syscall_log[0].request["tool_name"] == SEARCH_MEMORY_TOOL

    async def test_no_eviction_when_under_watermark(self):
        """No syscall logged when under watermark."""
        registry = ToolRegistry()
        lodge, _ = _make_lodge(registry, watermark=1000)
        dam = CastorDam(registry)
        cap_mgr = CapabilityManager()

        cp = _make_checkpoint(
            cap_mgr,
            [
                CastorMessage(role="user", content="small", token_count=5),
            ],
        )

        proxy = SyscallProxy(cp, dam, cap_mgr, lodge=lodge)
        await lodge.check_and_evict(proxy, cp)

        assert len(cp.syscall_log) == 0
        assert len(cp.context_history) == 1
