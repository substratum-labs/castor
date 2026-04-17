"""Integration tests for Castor → Mnemos bridge.

Starts a real `mnemosd` subprocess and runs an agent through Castor
that issues inference calls via `MnemosLLMSyscall`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

# Skip the entire module if mnemos gencode cannot load (e.g. protobuf
# runtime/gencode version mismatch when autogen extra is active instead
# of mnemos extra — they're marked mutually exclusive in pyproject.toml).
# importorskip() only catches ImportError; protobuf VersionError slips past
# it, so we try the real gencode import and set pytestmark directly.
try:
    from mnemos.client import MnemosClient  # noqa: E402
except Exception as _mnemos_err:  # ImportError OR protobuf VersionError
    pytestmark = pytest.mark.skip(
        reason=f"mnemos not usable in this environment: {_mnemos_err!s}"
    )
else:
    from castor.core import Castor  # noqa: E402
    from castor.gate.decorator import castor_tool  # noqa: E402
    from castor.mnemos import MnemosCastor, MnemosLLMSyscall  # noqa: E402

# Path to the mnemosd binary built from the sister mnemos repo
MNEMOSD_BIN = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "mnemos", "target", "debug", "mnemosd"
    )
)
PORT = 50299  # Distinct port to avoid clashing with mnemos's own tests


@pytest.fixture(scope="module")
def mnemosd_server():
    """Start mnemosd subprocess for the test module."""
    if not os.path.exists(MNEMOSD_BIN):
        pytest.skip(
            f"mnemosd binary not found at {MNEMOSD_BIN} — "
            "run `cargo build -p mnemos-server` in the mnemos repo first"
        )

    proc = subprocess.Popen(
        [
            MNEMOSD_BIN,
            "--port",
            str(PORT),
            "--total-blocks",
            "64",
            "--block-size",
            "16",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1)
    if proc.poll() is not None:
        out = proc.stdout.read().decode() if proc.stdout else ""
        err = proc.stderr.read().decode() if proc.stderr else ""
        pytest.fail(f"mnemosd failed to start: {out} {err}")

    yield proc

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
async def mnemos_client(mnemosd_server):
    """Connected MnemosClient for use in tests."""
    client = MnemosClient(host="localhost", port=PORT)
    await client.connect()
    yield client
    await client.close()


async def test_castor_mnemos_basic_inference(mnemos_client):
    """Agent issues an inference call through Castor → Mnemos."""
    kernel = Castor()
    syscall = MnemosLLMSyscall(
        registry=kernel.gate.registry,
        client=mnemos_client,
        model_id="test-model",
    )

    async def agent(proxy):
        result = await syscall.infer(
            proxy,
            tokens=[1, 2, 3],
            max_new_tokens=5,
        )
        return result

    cp = await kernel.run(agent, budgets={"api_usd": 10.0})
    assert cp.status == "COMPLETED"
    assert cp.result["status"] == "complete"
    assert len(cp.result["tokens"]) > 0

    # Cleanup
    await syscall.drop_for(cp.pid)


async def test_castor_mnemos_journal_logged(mnemos_client):
    """Mnemos syscall should be logged in Castor's journal."""
    kernel = Castor()
    syscall = MnemosLLMSyscall(
        registry=kernel.gate.registry,
        client=mnemos_client,
        model_id="test-model",
    )

    async def agent(proxy):
        return await syscall.infer(proxy, tokens=[10, 20], max_new_tokens=3)

    cp = await kernel.run(agent, budgets={"api_usd": 10.0})
    assert cp.status == "COMPLETED"
    assert len(cp.syscall_log) >= 1
    record = cp.syscall_log[0]
    assert record.request["tool_name"] == "mnemos_inference"

    await syscall.drop_for(cp.pid)


async def test_castor_mnemos_multiple_calls_share_context(mnemos_client):
    """Multiple inference calls from the same agent should share a Mnemos context."""
    kernel = Castor()
    syscall = MnemosLLMSyscall(
        registry=kernel.gate.registry,
        client=mnemos_client,
        model_id="test-model",
    )

    async def agent(proxy):
        r1 = await syscall.infer(proxy, tokens=[1, 2, 3], max_new_tokens=3)
        r2 = await syscall.infer(proxy, tokens=[4, 5, 6], max_new_tokens=3)
        return {"first": r1, "second": r2}

    cp = await kernel.run(agent, budgets={"api_usd": 10.0})
    assert cp.status == "COMPLETED"
    assert cp.result["first"]["status"] == "complete"
    assert cp.result["second"]["status"] == "complete"

    # Both calls should reuse the same handle
    assert syscall.lifecycle.has(cp.pid)

    await syscall.drop_for(cp.pid)
    assert not syscall.lifecycle.has(cp.pid)


async def test_castor_mnemos_drop_idempotent(mnemos_client):
    """drop_for should be idempotent."""
    kernel = Castor()
    syscall = MnemosLLMSyscall(
        registry=kernel.gate.registry,
        client=mnemos_client,
        model_id="test-model",
    )

    async def agent(proxy):
        return await syscall.infer(proxy, tokens=[1], max_new_tokens=2)

    cp = await kernel.run(agent, budgets={"api_usd": 10.0})
    await syscall.drop_for(cp.pid)
    await syscall.drop_for(cp.pid)  # second call should not raise


# --- D: MnemosCastor adapter tests ---


async def test_pin_unpin_noop_without_handle(mnemos_client):
    """pin_for/unpin_for on a pid with no registered handle should be no-ops."""
    kernel = Castor()
    syscall = MnemosLLMSyscall(
        registry=kernel.gate.registry,
        client=mnemos_client,
        model_id="test-model",
    )
    # Nothing registered for this pid
    await syscall.pin_for("nonexistent-pid")
    await syscall.unpin_for("nonexistent-pid")
    # Just verifying no exception raised


async def test_pin_unpin_on_real_handle(mnemos_client):
    """pin_for/unpin_for should succeed against a real Mnemos context."""
    kernel = Castor()
    syscall = MnemosLLMSyscall(
        registry=kernel.gate.registry,
        client=mnemos_client,
        model_id="test-model",
    )

    async def agent(proxy):
        return await syscall.infer(proxy, tokens=[1, 2, 3], max_new_tokens=2)

    cp = await kernel.run(agent, budgets={"api_usd": 10.0})
    assert syscall.lifecycle.has(cp.pid)
    # Round-trip pin/unpin — should not raise
    await syscall.pin_for(cp.pid)
    await syscall.unpin_for(cp.pid)
    await syscall.drop_for(cp.pid)


async def test_mnemos_castor_drops_on_completion(mnemos_client):
    """MnemosCastor.run() should auto-drop context on COMPLETED."""
    kernel = Castor()
    syscall = MnemosLLMSyscall(
        registry=kernel.gate.registry,
        client=mnemos_client,
        model_id="test-model",
    )
    mkernel = MnemosCastor(kernel, syscall)

    async def agent(proxy):
        return await syscall.infer(proxy, tokens=[1, 2, 3], max_new_tokens=2)

    cp = await mkernel.run(agent, budgets={"api_usd": 10.0})
    assert cp.status == "COMPLETED"
    # Auto-dropped — no manual drop_for needed
    assert not syscall.lifecycle.has(cp.pid)


async def test_mnemos_castor_hitl_pin_flow(mnemos_client):
    """MnemosCastor should pin on HITL suspend, unpin on approve."""

    # A destructive tool requires HITL by default
    @castor_tool(requires_hitl=True, cost_per_use=0.5)
    def sensitive_action(target: str) -> str:
        return f"acted on {target}"

    kernel = Castor(tools=[sensitive_action])
    syscall = MnemosLLMSyscall(
        registry=kernel.gate.registry,
        client=mnemos_client,
        model_id="test-model",
    )
    mkernel = MnemosCastor(kernel, syscall)

    async def agent(proxy):
        # First an LLM call (populates Mnemos context)
        await syscall.infer(proxy, tokens=[1, 2, 3], max_new_tokens=2)
        # Then a destructive tool call — triggers HITL suspend
        return await proxy.syscall("sensitive_action", {"target": "db"})

    cp = await mkernel.run(agent, budgets={"api_usd": 10.0, "_default": 10.0})
    # Agent suspended waiting for HITL
    assert cp.status == "SUSPENDED_FOR_HITL"
    # Context still registered — and should be pinned (we trust gRPC call succeeded)
    assert syscall.lifecycle.has(cp.pid)

    # Approve — unpins first, then Castor executes the suspended syscall
    await mkernel.approve(cp)
    # After approve, agent should have completed — we need to resume
    cp2 = await mkernel.run(agent, checkpoint=cp)
    assert cp2.status == "COMPLETED"
    # Auto-dropped after completion
    assert not syscall.lifecycle.has(cp2.pid)


async def test_mnemos_castor_run_until_complete_with_auto_approve(mnemos_client):
    """run_until_complete should pin/unpin across HITL and auto-drop at end."""

    @castor_tool(requires_hitl=True, cost_per_use=0.5)
    def another_action(value: int) -> int:
        return value * 2

    kernel = Castor(tools=[another_action])
    syscall = MnemosLLMSyscall(
        registry=kernel.gate.registry,
        client=mnemos_client,
        model_id="test-model",
    )
    mkernel = MnemosCastor(kernel, syscall)

    async def agent(proxy):
        await syscall.infer(proxy, tokens=[1, 2, 3], max_new_tokens=2)
        return await proxy.syscall("another_action", {"value": 21})

    async def auto_approve(cp):
        return ("approve", None)

    cp = await mkernel.run_until_complete(
        agent,
        on_hitl=auto_approve,
        budgets={"api_usd": 10.0, "_default": 10.0},
    )
    assert cp.status == "COMPLETED"
    # Auto-dropped
    assert not syscall.lifecycle.has(cp.pid)
