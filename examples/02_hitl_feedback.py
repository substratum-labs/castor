"""Demo 02 — HITL Feedback: Approve, Reject, or Redirect your agent.

The agent proposes. The human disposes. The agent adapts.
Three identical agents run with three different human decisions.

Run:
    uv run python examples/02_hitl_feedback.py
"""

import asyncio

from castor import (
    AgentCheckpoint,
    AgentRunner,
    CapabilityManager,
    CastorDam,
    HITLHandler,
    SyscallProxy,
    castor_tool,
)
from castor.dam.registry import ToolRegistry

# ── Output helpers ──


def _h(text: str) -> None:
    print(f"\n\033[1;36m=== {text} ===\033[0m")


def _ok(text: str) -> None:
    print(f"  \033[32m[OK]\033[0m {text}")


def _warn(text: str) -> None:
    print(f"  \033[33m[!!]\033[0m {text}")


# ── 1. Register tools ──

registry = ToolRegistry()


@castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
async def research(topic: str) -> str:
    return f"Findings on '{topic}': 3 key insights discovered"


@castor_tool(
    consumes="api", cost_per_use=2.0,
    destructive=True, requires_hitl=True, registry=registry,
)
async def send_email(to: str, subject: str, body: str) -> str:
    return f"Email sent to {to}: '{subject}'"


@castor_tool(consumes="api", cost_per_use=0.5, registry=registry)
async def save_draft(title: str, content: str) -> str:
    return f"Draft saved: '{title}'"


# ── 2. Define adaptive agent ──


async def email_agent(proxy: SyscallProxy) -> str:
    findings = await proxy.syscall("research", {"topic": "Q4 results"})

    result = await proxy.syscall("send_email", {
        "to": "team@company.com",
        "subject": "Q4 Results Summary",
        "body": f"Here is the full report. {findings}",
    })

    # Handle rejection: save as draft instead
    if isinstance(result, dict) and result.get("status") == "HITL_REJECTED":
        feedback = result["human_feedback"]
        draft = await proxy.syscall("save_draft", {
            "title": "Q4 Email Draft",
            "content": f"Original email (rejected: {feedback}). {findings}",
        })
        return f"Email rejected. {draft}"

    # Handle modification: revise and resend
    if isinstance(result, dict) and result.get("status") == "HITL_MODIFIED":
        feedback = result["human_feedback"]
        result = await proxy.syscall("send_email", {
            "to": "team@company.com, manager@company.com",
            "subject": "Q4 Results (Revised)",
            "body": f"Brief summary per feedback: '{feedback}'. {findings}",
        })
        return f"Email revised and sent. {result}"

    return f"Email approved and sent. {result}"


# ── 3. Run three scenarios ──


async def run_scenario(
    name: str,
    decision: str,
    feedback: str = "",
) -> dict:
    """Run the agent with a specific HITL decision and return outcome."""
    dam = CastorDam(registry)
    cap_mgr = CapabilityManager()
    handler = HITLHandler()

    caps = cap_mgr.create_capabilities({"api": 20.0})
    checkpoint = AgentCheckpoint(
        pid=f"email-{name}", status="RUNNING",
        agent_function_name="email_agent", capabilities=caps,
    )

    # Run 1: agent hits send_email, suspends
    runner = AgentRunner(dam, cap_mgr)
    checkpoint = await runner.run(email_agent, checkpoint)

    pending = checkpoint.pending_hitl
    print(f"  Agent wants: send_email(to={pending['arguments']['to']!r})")

    # Apply human decision
    if decision == "approve":
        await handler.approve(checkpoint, dam, cap_mgr)
        _ok("Human: APPROVED")
    elif decision == "reject":
        handler.reject(checkpoint, feedback)
        _warn(f'Human: REJECTED ("{feedback}")')
    elif decision == "modify":
        handler.modify(checkpoint, feedback)
        _warn(f'Human: MODIFIED ("{feedback}")')

    # Resume
    runner2 = AgentRunner(dam, cap_mgr)
    checkpoint = await runner2.run(email_agent, checkpoint)

    # Modification may trigger a second HITL (revised send_email)
    if checkpoint.status == "SUSPENDED_FOR_HITL":
        _ok("Revised email needs approval -> auto-approving")
        await handler.approve(checkpoint, dam, cap_mgr)
        runner3 = AgentRunner(dam, cap_mgr)
        checkpoint = await runner3.run(email_agent, checkpoint)

    return {
        "scenario": name,
        "decision": decision,
        "syscalls": len(checkpoint.syscall_log),
        "result": checkpoint.result,
        "budget_used": checkpoint.capabilities["api"].current_usage,
    }


async def main() -> None:
    results = []

    _h("Scenario A: APPROVE")
    results.append(await run_scenario("A", "approve"))

    _h("Scenario B: REJECT")
    results.append(await run_scenario(
        "B", "reject", "Too informal for external stakeholders",
    ))

    _h("Scenario C: MODIFY")
    results.append(await run_scenario(
        "C", "modify", "Make it shorter, CC the manager",
    ))

    # ── Comparison table ──
    _h("Comparison")
    print(f"  {'Scenario':<12} {'Decision':<10} {'Syscalls':<10} {'Cost':<8} Result")
    print(f"  {'─' * 12} {'─' * 10} {'─' * 10} {'─' * 8} {'─' * 30}")
    for r in results:
        result_short = str(r["result"])[:45]
        print(f"  {r['scenario']:<12} {r['decision']:<10} {r['syscalls']:<10} "
              f"{r['budget_used']:<8.1f} {result_short}")


if __name__ == "__main__":
    asyncio.run(main())
