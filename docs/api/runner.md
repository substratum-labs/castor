# Runner & Persistence

The execution runtime and checkpoint storage.

## AgentRunner

The low-level agent execution loop. Most users should use `Castor.run()` instead of interacting with the runner directly.

::: castor.stream.runner.AgentRunner

## CheckpointStore

SQLite-backed checkpoint persistence for crash recovery.

```python
from castor import Castor

# Auto-create store
kernel = Castor(store="sqlite:///checkpoints.db")

# Save after execution
cp = await kernel.run(agent, budgets={"api": 50.0})
await kernel.save(cp)

# Load and resume
cp = kernel.load("agent-001")
cp = await kernel.run(agent, checkpoint=cp)
```

::: castor.stream.persistence.CheckpointStore
