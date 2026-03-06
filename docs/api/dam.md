# Dam (Tool Registry & Validation)

The Dam subsystem handles tool registration, Pydantic input validation, and execution dispatch.

## Registering Tools

```python
from castor import castor_tool

@castor_tool(consumes="api", cost_per_use=1.0, destructive=True)
async def send_email(to: str, subject: str, body: str) -> str:
    """Send an email. Destructive tools require HITL approval."""
    return f"Sent to {to}"
```

## castor_tool

::: castor.dam.decorator.castor_tool

## CastorDam

::: castor.dam.validator.CastorDam

## ToolMetadata

::: castor.dam.registry.ToolMetadata

## ToolRegistry

::: castor.dam.registry.ToolRegistry
