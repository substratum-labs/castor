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

# Skip the entire module if mnemos package is not installed
mnemos = pytest.importorskip("mnemos")
from mnemos.client import MnemosClient  # noqa: E402

from castor.core import Castor  # noqa: E402
from castor.mnemos import MnemosLLMSyscall  # noqa: E402

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
