# Human-in-the-Loop (HITL)

The HITL system suspends agent execution when a tool requires human approval, then resumes after the decision.

## Three Decision Paths

```python
# Approve — execute the blocked syscall as-is
await kernel.approve(checkpoint)

# Reject — block with feedback (agent sees rejection on replay)
kernel.reject(checkpoint, reason="Too risky")

# Modify — approve with guidance (agent re-plans on replay)
kernel.modify(checkpoint, feedback="CC the manager too")
```

## Automatic HITL Loop

```python
from castor import Castor, interactive

kernel = Castor()
cp = await kernel.run_until_complete(
    agent,
    budgets={"api": 50.0},
    on_hitl=interactive,  # prompts user in terminal
)
```

## HITLHandler

::: castor.stream.hitl.HITLHandler

## SuspendInterrupt

::: castor.models.checkpoint.SuspendInterrupt

## Built-in Policies

::: castor.hitl_policies.auto_approve

::: castor.hitl_policies.auto_reject

::: castor.hitl_policies.interactive
