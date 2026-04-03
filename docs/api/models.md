# Data Models

Pydantic models that form the kernel's data layer. All models are serializable for checkpoint persistence.

## Checkpoint Models

### AgentCheckpoint

The core state object. Key methods:

- `fork(at_step=N)` -- Create a new checkpoint rewound to step N for time-travel replay.
- `budget_used(resource)` / `budget_remaining(resource)` -- Budget introspection.

::: castor.models.checkpoint.AgentCheckpoint

### SyscallRecord

Each record includes a `needs_review` flag (set at execution time in speculative mode) and an optional `review_reason`.

::: castor.models.checkpoint.SyscallRecord

### CastorMessage

::: castor.models.checkpoint.CastorMessage

## Capability Models

### Capability

::: castor.models.capability.Capability

### SyscallRequest

::: castor.models.capability.SyscallRequest

### SyscallResponse

::: castor.models.capability.SyscallResponse

## Result Models

### SyscallResult

::: castor.models.result.SyscallResult
