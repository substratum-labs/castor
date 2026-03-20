# Castor + smolagents Integration Guide

Add budget enforcement, human-in-the-loop approval, and crash-safe checkpoint/replay to any [smolagents](https://github.com/huggingface/smolagents) agent — without rewriting your tools.

## The Problem

smolagents has no built-in:

- Budget/cost control for tool calls — an agent can burn unlimited API credits
- Approval gates for destructive tools — an agent can send messages, delete files, or hit external APIs without asking
- Crash recovery — if the process dies, all progress is lost

These are the kinds of gaps highlighted by [NCC Group's smolagents audit](https://www.nccgroup.com/research/autonomous-ai-agents-a-hidden-risk-in-insecure-smolagents-codeagent-usage/) (code execution without human review).

## Two Levels of Integration

Castor offers a progressive integration path. Start with Level 1 (2 minutes), upgrade to Level 2 when you need crash recovery.

| | Level 1: Guard | Level 2: Resilient |
|---|---|---|
| File | `guard.py` | `deep_guard.py` |
| Budget enforcement | Yes | Yes |
| HITL approval | Blocking (`input()`) | Suspend / persist / resume |
| Crash recovery | No | Yes (checkpoint/replay) |
| LLM call recording | No | Yes |
| SQLite persistence | No | Yes |
| Your code change | Swap constructor | Swap constructor + 1 param |

## Installation

```bash
pip install castor[smolagents]
# or with uv:
uv pip install castor[smolagents]
```

## Level 1: Budget + HITL Guard

**What it does:** Intercepts every tool call to enforce budget limits and require human approval for destructive tools. Your existing tools work unchanged.

### Quick Start

```python
from smolagents import ToolCallingAgent
from examples.smolagents_guard.guard import CastorGuardedAgent

# Before — no protection:
# agent = ToolCallingAgent(tools=my_tools, model=my_model)

# After — budget + HITL:
agent = CastorGuardedAgent(
    tools=my_tools,
    model=my_model,
    budgets={"network": 50.0, "disk": 20.0},
    tool_policies={
        "web_search":   {"resource": "network", "cost": 1.0},
        "read_file":    {"resource": "disk",    "cost": 0.5},
        "write_file":   {"resource": "disk",    "cost": 1.0, "destructive": True},
        "send_message": {"resource": "network", "cost": 2.0, "destructive": True},
    },
)

agent.run("Research battery technology and send a summary to Slack")
```

Same tools. Same model. One constructor swap.

### How It Works

`CastorGuardedAgent` subclasses smolagents' `ToolCallingAgent` and overrides one method — `execute_tool_call()`. Before every tool call, two checks run:

```
agent.run(task)
  └→ smolagents agent loop (unchanged)
       └→ execute_tool_call(tool_name, arguments)
            ├── 1. Budget check: CapabilityManager.deduct()
            │   └→ CapabilityExhaustedError if over budget
            ├── 2. HITL gate: prompt human if destructive=True
            │   └→ ToolRejectedError if rejected
            ├── 3. super().execute_tool_call()  ← original smolagents path
            └── 4. Audit log append
```

### Tool Policies

Each tool needs a policy entry:

```python
tool_policies = {
    "tool_name": {
        "resource": "network",    # which budget to deduct from
        "cost": 1.0,              # cost per call
        "destructive": False,     # if True, requires HITL approval
    },
}
```

Tools without a policy entry run freely (no budget check, no HITL gate).

### Programmatic HITL

For automation and testing, pass a `hitl_policy` callable instead of using interactive prompts:

```python
# Approve everything except send_message
def my_policy(tool_name: str, arguments: dict) -> bool:
    return tool_name != "send_message"

agent = CastorGuardedAgent(
    ...,
    hitl_policy=my_policy,  # None = interactive input()
)
```

### Checking Budget

```python
for resource, info in agent.budget_summary().items():
    print(f"{resource}: {info['used']:.1f} / {info['max']:.1f}")
```

### Handling Errors

```python
from castor.capability.manager import CapabilityExhaustedError
from examples.smolagents_guard.guard import ToolRejectedError

try:
    agent.run(task)
except CapabilityExhaustedError as e:
    print(f"Budget exceeded: {e.resource_type}")
except ToolRejectedError as e:
    print(f"Tool rejected: {e.tool_name}")
```

Note: smolagents catches tool execution errors internally and feeds them back to the LLM, so the agent may self-correct rather than raising to your code.

---

## Level 2: Checkpoint/Replay + HITL Suspend/Resume

**What it does:** Records every LLM call and tool call in a replay log. If the agent crashes, it resumes from the last checkpoint without re-calling the LLM or re-executing tools. Destructive tools suspend the agent instead of blocking — you can approve asynchronously and resume later.

### Quick Start

```python
from examples.smolagents_guard.deep_guard import CastorResilientAgent
from castor.scheduler.persistence import CheckpointStore

agent = CastorResilientAgent(
    tools=my_tools,
    model=my_model,
    budgets={"network": 50.0, "disk": 20.0},
    tool_policies=tool_policies,
    checkpoint_store=CheckpointStore("sqlite:///agent.db"),
)

agent.run("Research battery technology and send a summary to Slack")
```

One extra parameter: `checkpoint_store=CheckpointStore(...)`.

### How It Works

Two hooks intercept all side effects:

```
CastorResilientAgent
├── ReplayModel (wraps your LLM model)
│   └→ generate(): replay cached → or → call LLM + record
│
└── execute_tool_call(): replay cached → or → budget + HITL + execute + record
│
└── syscall_log: [SyscallRecord, SyscallRecord, ...]  ← the checkpoint
```

LLM calls and tool calls are interleaved in the same `syscall_log`. On resume, they replay in exact order.

### Crash Recovery

```python
from examples.smolagents_guard.deep_guard import CastorResilientAgent
from castor.scheduler.persistence import CheckpointStore

store = CheckpointStore("sqlite:///agent.db")

# First run (may crash):
agent = CastorResilientAgent(
    tools=my_tools, model=my_model,
    budgets=budgets, tool_policies=policies,
    checkpoint_store=store, pid="task-001",
)
agent.run(task)  # crashes mid-run

# Resume:
checkpoint = store.load("task-001")
agent = CastorResilientAgent(
    tools=my_tools, model=my_model,
    budgets=budgets, tool_policies=policies,
    checkpoint_store=store, checkpoint=checkpoint,
)
agent.run(task)  # replays cached calls, continues from where it left off
```

During replay:
- LLM calls return cached responses (your LLM provider is never called)
- Tool calls return cached results (your tools are never executed)
- Budget is re-deducted to keep state consistent
- Once the replay log is exhausted, execution switches to live

### HITL Suspend / Resume

Destructive tools raise `HITLSuspendError` instead of blocking:

```python
from examples.smolagents_guard.deep_guard import CastorResilientAgent, HITLSuspendError
from castor.scheduler.persistence import CheckpointStore

store = CheckpointStore("sqlite:///agent.db")

# Run until HITL suspension:
agent = CastorResilientAgent(
    tools=my_tools, model=my_model,
    budgets=budgets, tool_policies=policies,
    checkpoint_store=store, pid="task-001",
)

try:
    agent.run(task)
except HITLSuspendError as e:
    print(f"Needs approval: {e.checkpoint.pending_hitl}")
    # Checkpoint is already saved to SQLite
```

Later, after human review:

```python
# Load the suspended checkpoint
checkpoint = store.load("task-001")
approved_request = checkpoint.pending_hitl  # save before clearing

# Approve
checkpoint.pending_hitl = None
checkpoint.status = "RUNNING"

# Resume
agent = CastorResilientAgent(
    tools=my_tools, model=my_model,
    budgets=budgets, tool_policies=policies,
    checkpoint_store=store,
    checkpoint=checkpoint,
    hitl_approved_request=approved_request,
)
agent.run(task)  # replays → executes approved tool → continues
```

### Castor Concepts Used

| Castor Class | Role in Integration |
|---|---|
| `CapabilityManager` | Budget tracking — `deduct()`, `create_capabilities()` |
| `SyscallRecord` | One entry in the replay log — `{request, response}` |
| `AgentCheckpoint` | Full agent state — `syscall_log`, `capabilities`, `pending_hitl` |
| `CheckpointStore` | SQLite persistence — `save()`, `load()` |

These are Castor's real production APIs, not demo wrappers. What you learn here transfers directly to full Castor integration.

---

## Running the Demos

```bash
# Level 1: budget + HITL
uv run python examples/framework_guards/smolagents/demo.py

# Level 2: crash recovery + HITL suspend/resume
uv run python examples/framework_guards/smolagents/demo_deep.py

# Tests
uv run pytest examples/framework_guards/smolagents/ -v
```

## File Structure

```
examples/framework_guards/smolagents/
├── README.md              ← you are here
├── guard.py               ← Level 1: CastorGuardedAgent (~60 lines)
├── deep_guard.py          ← Level 2: CastorResilientAgent + ReplayModel (~215 lines)
├── tools.py               ← stub tools shared by both levels
├── demo.py                ← Level 1 demo (3 acts)
├── demo_deep.py           ← Level 2 demo (2 acts)
├── test_guard.py          ← Level 1 tests (4 tests)
└── test_deep_guard.py     ← Level 2 tests (7 tests)
```

## Known Boundaries

What this integration **cannot** do (and why):

| Limitation | Reason | Alternative |
|---|---|---|
| Token-level preemption | Requires Castor's `AgentRunner` to own the agent loop | Use full Castor for preemption |
| MMU context management | smolagents manages its own context window | Use full Castor for MMU |
| Multi-agent spawning | Requires Castor's `SyscallProxy` async pipeline | Use full Castor for sub-agents |

These limitations exist because smolagents owns its agent loop. Castor's guard layer hooks into the loop but doesn't replace it. For full control, use Castor's native `AgentRunner` + `SyscallProxy`.
