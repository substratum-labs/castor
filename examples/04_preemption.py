"""Demo 04 — Preemption: Cancel a streaming LLM mid-sentence.

Your agent is streaming tokens. A policy violation appears mid-sentence.
Castor cancels instantly, saves partial work, and charges only for tokens consumed.

Run:
    uv run python examples/04_preemption.py
"""

import asyncio

from castor import (
    AgentCheckpoint,
    AgentRunner,
    CapabilityManager,
    CastorDam,
    StreamingLLMSyscall,
    SyscallProxy,
)
from castor.dam.registry import ToolRegistry

# ── Output helpers ──


def _h(text: str) -> None:
    print(f"\n\033[1;36m=== {text} ===\033[0m")


def _ok(text: str) -> None:
    print(f"  \033[32m[OK]\033[0m {text}")


def _warn(text: str) -> None:
    print(f"  \033[33m[!!]\033[0m {text}")


def _err(text: str) -> None:
    print(f"  \033[31m[!!]\033[0m {text}")


# ── 1. Fake streaming LLM ──

# The LLM will generate a plan that includes a dangerous action mid-stream.
DANGEROUS_RESPONSE = [
    "Step 1:",
    " Back up",
    " the database.",
    "\n",
    "Step 2:",
    " Validate",
    " schema.",
    "\n",
    "Step 3:",
    " Run",
    " DELETE",
    " /production",
    "/data",
    "\n",
    "Step 4:",
    " Send",
    " notification.",
]

SAFE_RESPONSE = [
    "Step 1:",
    " Back up",
    " the database.",
    "\n",
    "Step 2:",
    " Validate",
    " schema.",
    "\n",
    "Step 3:",
    " Deploy",
    " to staging.",
    "\n",
    "Step 4:",
    " Run",
    " integration",
    " tests.",
]

_use_safe = False


async def fake_llm_stream(model: str, prompt: str):
    """Yield tokens one by one, simulating a streaming LLM."""
    source = SAFE_RESPONSE if _use_safe else DANGEROUS_RESPONSE
    for token in source:
        yield token
        await asyncio.sleep(0.01)  # simulate network latency


# ── 2. Set up kernel with content safety callback ──

registry = ToolRegistry()
_runner_ref: AgentRunner | None = None


def content_safety_check(chunk: str, accumulated: str) -> None:
    """on_chunk callback: scan accumulated text for policy violations."""
    if "DELETE /production" in accumulated:
        _err(f"POLICY VIOLATION detected in: ...{accumulated[-40:]!r}")
        if _runner_ref is not None:
            _runner_ref.preempt(
                "CONTENT_POLICY_VIOLATION",
                {"pattern": "DELETE /production", "position": len(accumulated)},
            )


llm = StreamingLLMSyscall(
    registry,
    stream_fn=fake_llm_stream,
    consumes="api_usd",
    cost_per_use=1.0,
    cost_per_token=0.05,
    on_chunk=content_safety_check,
)

dam = CastorDam(registry)
cap_mgr = CapabilityManager()


# ── 3. Define agent ──


async def planning_agent(proxy: SyscallProxy) -> str:
    ctx = proxy.preemption_context
    if ctx and ctx["reason"] == "CONTENT_POLICY_VIOLATION":
        _ok(f"Agent sees preemption reason: {ctx['reason']}")
        _ok(f"Partial work: {ctx['partial_work']!r:.60s}...")
        _ok("Agent adapts: requesting a safe plan instead")
        return await llm.infer(proxy, model="safe-model", prompt="safe plan please")
    return await llm.infer(proxy, model="gpt-4", prompt="plan deployment")


# ── 4. Run it ──


async def main() -> None:
    global _runner_ref, _use_safe

    # --- First run: dangerous content triggers preemption ---
    _h("Streaming LLM with Content Safety")
    caps = cap_mgr.create_capabilities({"api_usd": 5.0})
    checkpoint = AgentCheckpoint(
        pid="preempt-001",
        status="RUNNING",
        agent_function_name="planning_agent",
        capabilities=caps,
    )

    runner = AgentRunner(dam, cap_mgr)
    _runner_ref = runner

    print("  Streaming tokens:")
    task = await runner.run_as_task(planning_agent, checkpoint)

    try:
        await task
    except asyncio.CancelledError:
        pass

    print(f"\n  Status: \033[33m{checkpoint.status}\033[0m")
    print(f"  Preemption reason: {checkpoint.preemption_reason}")
    if checkpoint.partial_work:
        print(f"  Partial work: {checkpoint.partial_work!r:.70s}")
    budget = checkpoint.capabilities["api_usd"]
    charged = budget.current_usage
    print(f"  Budget charged: ${charged:.2f} (proportional) instead of $1.00 flat rate")

    # --- Resume: agent adapts based on preemption context ---
    _h("Resuming with safety context")
    checkpoint.status = "RUNNING"
    _use_safe = True  # Switch to safe LLM response

    runner2 = AgentRunner(dam, cap_mgr)
    _runner_ref = runner2
    checkpoint = await runner2.run(planning_agent, checkpoint)

    print(f"\n  Status: \033[32m{checkpoint.status}\033[0m")
    print(f"  Result: {checkpoint.result!r:.70s}")
    final_budget = checkpoint.capabilities["api_usd"]
    print(
        f"  Total budget: ${final_budget.current_usage:.2f}"
        f"/${final_budget.max_budget:.2f} used"
    )


if __name__ == "__main__":
    asyncio.run(main())
