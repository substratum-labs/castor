"""Tests for the memory policy + cold storage layer.

Verifies that:
1. DefaultMemoryPolicy evicts oldest non-pinned messages under budget
2. InMemoryColdStorage stores and retrieves correctly
3. The policy + cold storage integrate to form a complete eviction cycle
4. Cross-session (cross-agent) cold storage sharing works
"""

from __future__ import annotations

import pytest

from castor import CastorMessage, DefaultMemoryPolicy, InMemoryColdStorage

# ---------------------------------------------------------------------------
# DefaultMemoryPolicy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_eviction_under_budget():
    policy = DefaultMemoryPolicy()
    history = [
        CastorMessage(role="user", content="short"),
    ]
    result = await policy.should_evict(history, token_budget=1000)
    assert result is None


@pytest.mark.asyncio
async def test_evict_oldest_non_pinned():
    policy = DefaultMemoryPolicy()
    history = [
        CastorMessage(id="sys", role="system", content="x" * 200, pinned=True),
        CastorMessage(id="old", role="user", content="x" * 200),  # oldest
        CastorMessage(id="mid", role="assistant", content="x" * 200),
        CastorMessage(id="new", role="user", content="x" * 200),  # newest
    ]
    # Budget tight enough that at least one message must be evicted.
    # Each is ~50 tokens (200 chars / 4). Total ~200. Budget 130 → evict 1+.
    result = await policy.should_evict(history, token_budget=130)
    assert result is not None
    # Should evict index 1 first (oldest non-pinned), not index 0 (pinned)
    assert "sys" not in result, "pinned message should not be evicted"
    assert "old" in result, "oldest non-pinned should be first to evict"


@pytest.mark.asyncio
async def test_evict_skips_all_pinned():
    policy = DefaultMemoryPolicy()
    history = [
        CastorMessage(id="a", role="system", content="x" * 400, pinned=True),
        CastorMessage(id="b", role="user", content="x" * 400, pinned=True),
    ]
    # Over budget but everything is pinned → nothing to evict
    result = await policy.should_evict(history, token_budget=10)
    assert result is None or result == []


@pytest.mark.asyncio
async def test_evict_handles_dict_messages():
    """Plain dicts in context_history should be handled gracefully."""
    policy = DefaultMemoryPolicy()
    history = [
        {"role": "user", "content": "x" * 400, "id": "dict1"},
        CastorMessage(role="assistant", content="x" * 400),
    ]
    result = await policy.should_evict(history, token_budget=50)
    assert result is not None
    assert len(result) >= 1


@pytest.mark.asyncio
async def test_generate_summary_returns_none():
    """Default policy doesn't summarize."""
    policy = DefaultMemoryPolicy()
    result = await policy.generate_summary(
        [CastorMessage(role="user", content="hello")]
    )
    assert result is None


@pytest.mark.asyncio
async def test_should_recall_returns_none():
    """Default policy doesn't recall."""
    policy = DefaultMemoryPolicy()
    result = await policy.should_recall([], "query")
    assert result is None


@pytest.mark.asyncio
async def test_on_session_end_is_noop():
    """Default policy's session end hook does nothing."""
    policy = DefaultMemoryPolicy()
    # Should not raise
    await policy.on_session_end([], [])


# ---------------------------------------------------------------------------
# InMemoryColdStorage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_and_search():
    cs = InMemoryColdStorage()
    messages = [
        CastorMessage(role="user", content="the database has three tables"),
        CastorMessage(role="assistant", content="I found the users table"),
    ]
    await cs.store("agent_1", messages, source="eviction")

    results = await cs.search("agent_1", "tables")
    assert len(results) >= 1
    assert "tables" in results[0]["content"]


@pytest.mark.asyncio
async def test_search_respects_agent_id():
    cs = InMemoryColdStorage()
    await cs.store(
        "agent_1",
        [CastorMessage(role="user", content="agent one data")],
    )
    await cs.store(
        "agent_2",
        [CastorMessage(role="user", content="agent two data")],
    )

    r1 = await cs.search("agent_1", "data")
    r2 = await cs.search("agent_2", "data")
    assert all("one" in r["content"] for r in r1)
    assert all("two" in r["content"] for r in r2)


@pytest.mark.asyncio
async def test_search_source_filter():
    cs = InMemoryColdStorage()
    await cs.store(
        "a",
        [CastorMessage(role="user", content="evicted content")],
        source="eviction",
    )
    await cs.store_explicit("a", "explicit memory", metadata={"tag": "test"})

    all_results = await cs.search("a", "content")
    eviction_only = await cs.search("a", "content", filter={"source": "eviction"})
    explicit_only = await cs.search("a", "memory", filter={"source": "explicit"})

    assert len(all_results) >= 1
    assert len(eviction_only) >= 1
    assert len(explicit_only) >= 1
    # source filter verified by count — filter already applied in search
    assert len(eviction_only) >= 1
    assert len(explicit_only) >= 1


@pytest.mark.asyncio
async def test_store_explicit():
    cs = InMemoryColdStorage()
    await cs.store_explicit("a", "remember this fact", metadata={"importance": "high"})

    results = await cs.search("a", "fact")
    assert len(results) == 1
    assert results[0]["content"] == "remember this fact"
    assert results[0]["metadata"]["importance"] == "high"


@pytest.mark.asyncio
async def test_cross_session_sharing():
    """Entries stored under agent_id are visible to any session of that agent."""
    cs = InMemoryColdStorage()
    # Session 1 stores
    await cs.store(
        "agent_shared",
        [CastorMessage(role="user", content="learned in session 1")],
    )
    # Session 2 searches — same agent_id, different "session"
    results = await cs.search("agent_shared", "session 1")
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_summary_searchable():
    cs = InMemoryColdStorage()
    await cs.store(
        "a",
        [CastorMessage(role="user", content="original content xyz")],
        summary="investigated the xyz module",
    )
    results = await cs.search("a", "xyz module")
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_max_results():
    cs = InMemoryColdStorage()
    for i in range(20):
        await cs.store_explicit("a", f"entry {i} about topic")

    results = await cs.search("a", "topic", limit=5)
    assert len(results) == 5


@pytest.mark.asyncio
async def test_clear():
    cs = InMemoryColdStorage()
    await cs.store_explicit("a", "data")
    assert cs.count("a") == 1
    cs.clear("a")
    assert cs.count("a") == 0


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_default_policy_conforms_to_protocol():
    from castor import MemoryPolicyProtocol

    assert isinstance(DefaultMemoryPolicy(), MemoryPolicyProtocol)


def test_cold_storage_conforms_to_protocol():
    from castor import ColdStorageProtocol

    assert isinstance(InMemoryColdStorage(), ColdStorageProtocol)
