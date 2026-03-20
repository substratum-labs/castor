# Gate (Tool Registry & Validation)

The Gate subsystem handles tool registration, Pydantic input validation, and execution dispatch.

## Registering Tools

```python
from castor import castor_tool

@castor_tool(consumes="api", cost_per_use=1.0, destructive=True)
async def send_email(to: str, subject: str, body: str) -> str:
    """Send an email. Destructive tools require HITL approval."""
    return f"Sent to {to}"
```

## castor_tool

::: castor.gate.decorator.castor_tool

## SyscallGate

::: castor.gate.validator.SyscallGate

## ToolMetadata

::: castor.gate.registry.ToolMetadata

## ToolRegistry

::: castor.gate.registry.ToolRegistry
