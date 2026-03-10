"""Demo 02 — HITL Feedback: Approve, Reject, or Redirect your agent.

Level 2 (SyscallProxy) — uses the raw proxy API for full kernel control.
See examples 06-08 for Level 1 (castor.lib) equivalents.

The agent proposes. The human disposes. The agent adapts.
Three identical agents run with three different human decisions.

Run:
    uv run python examples/02_hitl_feedback.py
"""

import asyncio

from castor import Castor, SyscallProxy, castor_tool

# ── Output helpers ──


def _h(text: str) -> None:
    print(f"\n\033[1;36m=== {text} ===\033[0m")


def _ok(text: str) -> None:
    print(f"  \033[32m[OK]\033[0m {text}")


def _warn(text: str) -> None:
    print(f"  \033[33m[!!]\033[0m {text}")


# ── 1. Register tools ──


@castor_tool(consumes="api", cost_per_use=1.0)
async def research(topic: str) -> str:
    return f"Findings on '{topic}': 3 key insights discovered"


@castor_tool(
    consumes="api",
    cost_per_use=2.0,
    destructive=True,
    requires_hitl=True,
)
async def send_email(to: str, subject: str, body: str) -> str:
    return f"Email sent to {to}: '{subject}'"


@castor_tool(consumes="api", cost_per_use=0.5)
async def save_draft(title: str, content: str) -> str:
    return f"Draft saved: '{title}'"


# ── 2. Define adaptive agent ──


async def email_agent(proxy: SyscallProxy) -> str:
    findings = await proxy.research(topic="Q4 results")

    result = await proxy.send_email(
        to="team@company.com",
        subject="Q4 Results Summary",
        body=f"Here is the full report. {findings}",
    )

    # Handle rejection: save as draft instead
    if result.rejected:
        draft = await proxy.save_draft(
            title="Q4 Email Draft",
            content=f"Original email (rejected: {result.feedback}). {findings}",
        )
        return f"Email rejected. {draft}"

    # Handle modification: revise and resend
    if result.modified:
        result = await proxy.send_email(
            to="team@company.com, manager@company.com",
            subject="Q4 Results (Revised)",
            body=f"Brief summary per feedback: '{result.feedback}'. {findings}",
        )
        return f"Email revised and sent. {result.value}"

    return f"Email approved and sent. {result.value}"


# ── 3. Run three scenarios ──

kernel = Castor(tools=[research, send_email, save_draft], structured_results=True)


async def run_scenario(
    name: str,
    decision: str,
    feedback: str = "",
) -> dict:
    """Run the agent with a specific HITL decision and return outcome."""
    # Run 1: agent hits send_email, suspends
    cp = await kernel.run(email_agent, budgets={"api": 20.0}, pid=f"email-{name}")

    print(f"  Agent wants: send_email(to={cp.pending_args['to']!r})")

    # Apply human decision
    if decision == "approve":
        await kernel.approve(cp)
        _ok("Human: APPROVED")
    elif decision == "reject":
        kernel.reject(cp, reason=feedback)
        _warn(f'Human: REJECTED ("{feedback}")')
    elif decision == "modify":
        kernel.modify(cp, feedback=feedback)
        _warn(f'Human: MODIFIED ("{feedback}")')

    # Resume
    cp = await kernel.run(email_agent, checkpoint=cp)

    # Modification may trigger a second HITL (revised send_email)
    if cp.is_suspended:
        _ok("Revised email needs approval -> auto-approving")
        await kernel.approve(cp)
        cp = await kernel.run(email_agent, checkpoint=cp)

    return {
        "scenario": name,
        "decision": decision,
        "syscalls": len(cp.syscall_log),
        "result": cp.result,
        "budget_used": cp.budget_used("api"),
    }


async def main() -> None:
    results = []

    _h("Scenario A: APPROVE")
    results.append(await run_scenario("A", "approve"))

    _h("Scenario B: REJECT")
    results.append(
        await run_scenario(
            "B",
            "reject",
            "Too informal for external stakeholders",
        )
    )

    _h("Scenario C: MODIFY")
    results.append(
        await run_scenario(
            "C",
            "modify",
            "Make it shorter, CC the manager",
        )
    )

    # ── Comparison table ──
    _h("Comparison")
    print(f"  {'Scenario':<12} {'Decision':<10} {'Syscalls':<10} {'Cost':<8} Result")
    print(f"  {'─' * 12} {'─' * 10} {'─' * 10} {'─' * 8} {'─' * 30}")
    for r in results:
        result_short = str(r["result"])[:45]
        print(
            f"  {r['scenario']:<12} {r['decision']:<10} {r['syscalls']:<10} "
            f"{r['budget_used']:<8.1f} {result_short}"
        )


if __name__ == "__main__":
    asyncio.run(main())
