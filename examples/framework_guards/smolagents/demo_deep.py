"""smolagents + Castor Deep Integration Demo (Level 2)

Two acts demonstrating checkpoint/replay crash recovery and HITL suspend/resume:
1. Crash Recovery -- agent crashes mid-run, resumes from checkpoint
2. HITL Suspend/Resume -- destructive tool triggers suspension, human approves

Run: uv run python examples/framework_guards/smolagents/demo_deep.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Add project root so that ``examples`` is importable as a package.
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from smolagents.models import ChatMessage, Model  # noqa: E402

from examples.framework_guards.smolagents.deep_guard import (  # noqa: E402
    CastorResilientAgent,
    HITLSuspendError,
)
from examples.framework_guards.smolagents.tools import (  # noqa: E402
    read_file,
    send_message,
    web_search,
    write_file,
)


# ---------------------------------------------------------------------------
# TrackingModel -- scripted LLM that counts real (non-replay) calls
# ---------------------------------------------------------------------------
class TrackingModel(Model):
    """Model that returns scripted responses and tracks real call count."""

    def __init__(self, responses: list[str]):
        super().__init__(model_id="tracking")
        self._responses = responses
        self._call_index = 0
        self.call_count = 0

    def generate(self, messages: list[dict[str, Any]], **kwargs) -> ChatMessage:
        self.call_count += 1
        idx = self._call_index
        self._call_index += 1
        content = self._responses[idx] if idx < len(self._responses) else "(fallback)"
        return ChatMessage(role="assistant", content=content)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TOOLS = [web_search, read_file, write_file, send_message]

POLICIES = {
    "web_search": {"resource": "network", "cost": 1.0},
    "read_file": {"resource": "disk", "cost": 0.5},
    "write_file": {"resource": "disk", "cost": 2.0},
    "send_message": {"resource": "network", "cost": 1.5, "destructive": True},
}

BUDGETS = {"network": 20.0, "disk": 10.0}

DIVIDER = "=" * 60


def banner(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


def tag(label: str) -> str:
    """Return a formatted [REPLAY] or [LIVE] tag."""
    return f"[{label}]"


# ---------------------------------------------------------------------------
# Act 1: Crash Recovery
# ---------------------------------------------------------------------------
def act1() -> None:
    banner("ACT 1: Crash Recovery -- checkpoint/replay")

    print("\n  --- Phase 1: Agent runs normally, then crashes ---\n")

    # Scripted LLM responses for the full run (3 in Phase 1, 1 in Phase 2)
    model1 = TrackingModel(
        [
            "I'll search for relevant research papers.",
            "Let me read the source file.",
            "I'll write the summary now.",
            "Done! Let me notify the team.",
        ]
    )

    agent1 = CastorResilientAgent(
        tools=TOOLS,
        model=model1,
        budgets=BUDGETS,
        tool_policies=POLICIES,
    )

    # Step 1: LLM call
    result = agent1.model.generate([{"role": "user", "content": "Summarize research"}])
    print(f"  {tag('LIVE')} LLM -> {result.content!r}")

    # Step 2: web_search
    result = agent1.execute_tool_call("web_search", {"query": "LLM safety research"})
    print(f"  {tag('LIVE')} web_search -> {result!r}")

    # Step 3: LLM call
    result = agent1.model.generate([{"role": "user", "content": "Now read the file"}])
    print(f"  {tag('LIVE')} LLM -> {result.content!r}")

    # Step 4: read_file
    result = agent1.execute_tool_call("read_file", {"filename": "paper.txt"})
    print(f"  {tag('LIVE')} read_file -> {result!r}")

    # Step 5: LLM call
    result = agent1.model.generate([{"role": "user", "content": "Write summary"}])
    print(f"  {tag('LIVE')} LLM -> {result.content!r}")

    # Step 6: write_file
    result = agent1.execute_tool_call(
        "write_file", {"filename": "summary.md", "content": "# LLM Safety Summary"}
    )
    print(f"  {tag('LIVE')} write_file -> {result!r}")

    # --- "Crash" ---
    n_records = len(agent1._checkpoint.syscall_log)
    print(f"\n  *** Agent crashed! {n_records} syscalls in checkpoint. ***")

    # --- Phase 2: Resume from checkpoint ---
    print("\n  --- Phase 2: New agent resumes from checkpoint ---\n")

    # Deep copy simulates loading from SQLite
    saved_checkpoint = agent1._checkpoint.model_copy(deep=True)

    model2 = TrackingModel(
        [
            "Done! Let me notify the team.",
        ]
    )

    agent2 = CastorResilientAgent(
        tools=TOOLS,
        model=model2,
        budgets=BUDGETS,
        tool_policies=POLICIES,
        checkpoint=saved_checkpoint,
    )

    # Replay all 6 recorded syscalls
    # Step 1: LLM (replay)
    result = agent2.model.generate([{"role": "user", "content": "Summarize research"}])
    print(f"  {tag('REPLAY')} LLM -> {result.content!r}")

    # Step 2: web_search (replay)
    result = agent2.execute_tool_call("web_search", {"query": "LLM safety research"})
    print(f"  {tag('REPLAY')} web_search -> {result!r}")

    # Step 3: LLM (replay)
    result = agent2.model.generate([{"role": "user", "content": "Now read the file"}])
    print(f"  {tag('REPLAY')} LLM -> {result.content!r}")

    # Step 4: read_file (replay)
    result = agent2.execute_tool_call("read_file", {"filename": "paper.txt"})
    print(f"  {tag('REPLAY')} read_file -> {result!r}")

    # Step 5: LLM (replay)
    result = agent2.model.generate([{"role": "user", "content": "Write summary"}])
    print(f"  {tag('REPLAY')} LLM -> {result.content!r}")

    # Step 6: write_file (replay)
    result = agent2.execute_tool_call(
        "write_file", {"filename": "summary.md", "content": "# LLM Safety Summary"}
    )
    print(f"  {tag('REPLAY')} write_file -> {result!r}")

    # Step 7: NEW LLM call (live)
    result = agent2.model.generate([{"role": "user", "content": "Notify team"}])
    print(f"  {tag('LIVE')}   LLM -> {result.content!r}")

    # Step 8: NEW send_message (live -- not destructive-gated because we
    # want to show it completing; Act 2 shows the HITL gate)
    # Actually send_message IS destructive in policies -- use write_file instead
    # to show a clean live continuation without HITL.
    result = agent2.execute_tool_call(
        "write_file", {"filename": "notify.txt", "content": "Team notified."}
    )
    print(f"  {tag('LIVE')}   write_file -> {result!r}")

    # Summary
    llm_replayed = sum(
        1
        for rec in agent2._checkpoint.syscall_log[:6]
        if rec.request["tool_name"] == "__llm__"
    )
    tools_replayed = sum(
        1
        for rec in agent2._checkpoint.syscall_log[:6]
        if rec.request["tool_name"] != "__llm__"
    )
    print(
        f"\n  Recovered. {llm_replayed} LLM calls replayed (0 real)."
        f" {tools_replayed} tools replayed (0 real)."
    )
    print(
        f"  Then continued with {model2.call_count} new LLM call(s) + 1 new tool call."
    )


# ---------------------------------------------------------------------------
# Act 2: HITL Suspend / Resume
# ---------------------------------------------------------------------------
def act2() -> None:
    banner("ACT 2: HITL Suspend / Resume")

    print("\n  --- Phase 1: Agent runs until destructive tool triggers HITL ---\n")

    model1 = TrackingModel(
        [
            "Let me research this topic first.",
            "Now I'll message the research channel.",
        ]
    )

    agent1 = CastorResilientAgent(
        tools=TOOLS,
        model=model1,
        budgets=BUDGETS,
        tool_policies=POLICIES,
    )

    # Step 1: LLM call
    result = agent1.model.generate([{"role": "user", "content": "Research and share"}])
    print(f"  {tag('LIVE')} LLM -> {result.content!r}")

    # Step 2: web_search
    result = agent1.execute_tool_call("web_search", {"query": "transformer safety"})
    print(f"  {tag('LIVE')} web_search -> {result!r}")

    # Step 3: LLM call
    result = agent1.model.generate([{"role": "user", "content": "Share findings"}])
    print(f"  {tag('LIVE')} LLM -> {result.content!r}")

    # Step 4: send_message -> HITL suspension!
    suspended_checkpoint = None
    try:
        agent1.execute_tool_call(
            "send_message",
            {"recipient": "#research", "body": "New safety findings: ..."},
        )
    except HITLSuspendError as exc:
        suspended_checkpoint = exc.checkpoint
        print(
            f"\n  *** SUSPENDED! ***"
            f"\n  Pending: {suspended_checkpoint.pending_tool}"
            f" to {suspended_checkpoint.pending_args['recipient']}"
            f"\n  Suspended: {suspended_checkpoint.is_suspended}"
        )

    assert suspended_checkpoint is not None

    # --- Phase 2: Human approves, agent resumes ---
    print("\n  --- Phase 2: Human approves, agent resumes ---\n")

    # Deep copy simulates SQLite round-trip
    resumed_checkpoint = suspended_checkpoint.model_copy(deep=True)
    approved_request = resumed_checkpoint.pending_hitl

    # Clear HITL state (human approved)
    resumed_checkpoint.pending_hitl = None
    resumed_checkpoint.status = "RUNNING"

    model2 = TrackingModel([])  # no new LLM calls expected

    agent2 = CastorResilientAgent(
        tools=TOOLS,
        model=model2,
        budgets=BUDGETS,
        tool_policies=POLICIES,
        checkpoint=resumed_checkpoint,
        hitl_approved_request=approved_request,
    )

    # Replay Step 1: LLM (cached)
    result = agent2.model.generate([{"role": "user", "content": "Research and share"}])
    print(f"  {tag('REPLAY')} LLM -> {result.content!r}")

    # Replay Step 2: web_search (cached)
    result = agent2.execute_tool_call("web_search", {"query": "transformer safety"})
    print(f"  {tag('REPLAY')} web_search -> {result!r}")

    # Replay Step 3: LLM (cached)
    result = agent2.model.generate([{"role": "user", "content": "Share findings"}])
    print(f"  {tag('REPLAY')} LLM -> {result.content!r}")

    # Step 4: send_message (LIVE -- HITL approved)
    result = agent2.execute_tool_call(
        "send_message",
        {"recipient": "#research", "body": "New safety findings: ..."},
    )
    print(f"  {tag('LIVE')}   send_message -> {result!r}")

    # Audit log
    print(f"\n  Audit log ({len(agent2.audit_log)} entries):")
    for entry in agent2.audit_log:
        mode = "REPLAY" if entry["replayed"] else "LIVE"
        tool_name = entry["tool"]
        print(f"    [{mode:6s}] {tool_name}")

    # Budget summary
    print("\n  Budget summary:")
    for resource, info in agent2.budget_summary().items():
        remaining = info["remaining"]
        print(
            f"    {resource}: {info['used']:.1f}"
            f" / {info['max']:.1f}"
            f"  (remaining {remaining:.1f})"
        )


# ---------------------------------------------------------------------------
# Epilogue: Level 1 -> Level 2 progression
# ---------------------------------------------------------------------------
def epilogue() -> None:
    banner("LEVEL 1 -> LEVEL 2: What changed?")
    print("""
  Level 1 (guard.py):     Budget + HITL blocking
  Level 2 (deep_guard.py): + Checkpoint/replay + HITL suspend/resume

  Code diff:

    Level 1:
      agent = CastorGuardedAgent(
          tools=TOOLS, model=model,
          budgets={"network": 10.0, "disk": 5.0},
          tool_policies={...},
          hitl_policy=my_policy,
      )

    Level 2:
      agent = CastorResilientAgent(
          tools=TOOLS, model=model,
          budgets={"network": 10.0, "disk": 5.0},
          tool_policies={...},
          checkpoint_store=CheckpointStore("sqlite:///agent.db"),
      )

  One extra parameter: checkpoint_store=CheckpointStore("sqlite:///agent.db")

  What you get:
    - Crash recovery: agent resumes from last checkpoint
    - HITL suspend/resume: agent suspends, human approves, agent replays + continues
    - Deterministic replay: every LLM + tool call is cached and replayed
    - Budget consistency: capabilities are rebuilt from replay
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    act1()
    act2()
    epilogue()
