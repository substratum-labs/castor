# Capability Manager

Budget tracking, delegation, and enforcement. Each resource type (e.g. `"api"`, `"disk"`) has an independent spending limit.

## Usage

```python
from castor import Castor

kernel = Castor()
cp = await kernel.run(agent, budgets={"api": 50.0, "disk": 20.0})

# After execution
print(cp.budget_used("api"))       # e.g. 12.0
print(cp.budget_remaining("api"))  # e.g. 38.0
```

## CapabilityManager

::: castor.capability.manager.CapabilityManager
