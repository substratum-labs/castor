# Pydantic-AI + Castor Guard Layer

A finance trading agent demo showing how Castor's security features integrate with pydantic-ai's type-safe agent framework.

## What This Demonstrates

| Feature | Level 1 (Guard) | Level 2 (Resilient) |
|---------|-----------------|---------------------|
| Budget enforcement | Per-tool cost tracking, hard caps | Same + budget replay on resume |
| HITL gates | Blocking approval (reject = error) | Non-blocking suspend/resume |
| Crash recovery | - | Checkpoint/replay, zero wasted API calls |
| Typed output | `TradeDecision` Pydantic model | Same |

## Architecture

```
pydantic_ai.Agent(result_type=TradeDecision)
  └── CastorGuardedToolset(WrapperToolset)        # L1
        └── or CastorResilientToolset(WrapperToolset)  # L2
              └── FunctionToolset([fetch_price, execute_trade, ...])
```

**Key design:** Castor lives entirely in the **Toolset** layer. Zero changes to `Agent`. The `WrapperToolset.call_tool()` override is the single interception point — equivalent to smolagents' `execute_tool_call()`.

For L2, a shared `SyscallJournal` coordinates replay state between the toolset and the `ReplayModel` (which wraps pydantic-ai's `Model` for LLM call recording).

## Quick Start

### Level 1: Budget + HITL Guard

```python
from pydantic_ai import Agent
from pydantic_ai.toolsets.function import FunctionToolset
from examples.pydantic_ai_guard.guard import CastorGuardedToolset
from examples.pydantic_ai_guard.tools import fetch_price, execute_trade

guarded = CastorGuardedToolset(
    wrapped=FunctionToolset([fetch_price, execute_trade]),
    budgets={"api_calls": 5.0, "trade_usd": 10_000.0},
    tool_policies={
        "fetch_price":   {"resource": "api_calls", "cost": 1.0},
        "execute_trade": {"resource": "trade_usd", "cost": 500.0, "destructive": True},
    },
    hitl_policy=lambda name, args: input(f"Approve {name}? [y/n] ") == "y",
)

agent = Agent("openai:gpt-4o", toolsets=[guarded])
result = await agent.run("Buy $500 of AAPL")
print(guarded.budget_summary())
```

### Level 2: Checkpoint/Replay + HITL Suspend

```python
from examples.pydantic_ai_guard.deep_guard import (
    CastorResilientToolset, ReplayModel, HITLSuspendError,
)

resilient = CastorResilientToolset(
    wrapped=FunctionToolset([fetch_price, execute_trade]),
    budgets={"api_calls": 5.0, "trade_usd": 10_000.0},
    tool_policies={...},
)
replay_model = ReplayModel(inner_model=real_model, journal=resilient.journal)
agent = Agent(replay_model, toolsets=[resilient])

try:
    result = await agent.run("Buy AAPL")
except HITLSuspendError as e:
    # Checkpoint saved — human reviews e.checkpoint.pending_hitl
    # Resume later with hitl_approved_request=approved
    pass
```

## Running the Demos

```bash
uv run python -m examples.pydantic_ai_guard.demo       # L1: 3 acts
uv run python -m examples.pydantic_ai_guard.demo_deep   # L2: crash recovery + HITL
```

## Running the Tests

```bash
uv run pytest examples/pydantic_ai_guard/ -v   # 12 tests (4 L1 + 8 L2)
```

## Tool Policies

```python
tool_policies = {
    "tool_name": {
        "resource": "api_calls",   # which budget category
        "cost": 1.0,               # cost per invocation
        "destructive": False,      # True = requires HITL approval
    },
}
```

Tools without a policy entry run freely (no budget check, no HITL gate).

## Comparison with smolagents Integration

| Aspect | smolagents | pydantic-ai |
|--------|-----------|------------|
| Hook point | Subclass Agent, override `execute_tool_call()` | `WrapperToolset.call_tool()` (no Agent subclass) |
| Model wrapping | `ReplayModel(Model)` | `ReplayModel(Model)` (same pattern) |
| Composition | Inheritance | Toolset wrapping (composition) |
| Typed output | Strings | `result_type=TradeDecision` (Pydantic model) |
| State sharing | Instance variables | `SyscallJournal` (shared mutable object) |

## Files

| File | Lines | Purpose |
|------|-------|---------|
| `models.py` | Pydantic output models (TradeDecision, RiskAssessment) |
| `tools.py` | Stub trading tools (fetch_price, analyze_risk, execute_trade) |
| `guard.py` | L1: CastorGuardedToolset — budget + HITL |
| `deep_guard.py` | L2: CastorResilientToolset + ReplayModel + SyscallJournal |
| `demo.py` | L1 demo (3 acts: vanilla, guarded, exhaustion) |
| `demo_deep.py` | L2 demo (crash recovery + HITL suspend/resume) |
| `test_guard.py` | 4 L1 tests |
| `test_deep_guard.py` | 8 L2 tests |
