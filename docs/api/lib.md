# castor.lib — Agent Developer API

The standard library for agent developers. Import from `castor.lib` to write agents with zero kernel imports.

!!! note "Operator vs. Agent Developer"
    `castor.lib` is deliberately NOT re-exported from `castor.__init__`. The `castor` package is for **operators** (kernel setup, tool registration). `castor.lib` is for **agent developers** (tool calls, patterns, spawning).

## Quick Usage

```python
from castor.lib import tool, chat, budget, parallel, spawn, join

async def my_agent() -> str:
    results = await tool("web_search", query="castor")
    summary = await chat(f"Summarize: {results}")
    remaining = budget("api")
    return f"Done ({remaining} budget left): {summary}"
```

No `SyscallProxy` in the signature — the proxy is accessed via a `ContextVar` set by the kernel at runtime.

## Three API Levels

| Level | Function | Style |
|-------|----------|-------|
| **Level 0** | `run_task()` | One sentence in, result out |
| **Level 1** | `react()`, `parallel()`, `supervisor()`, etc. | Pattern-based composition |
| **Level 2** | `tool()`, `chat()`, `spawn()`, `join()` | Direct primitive calls |

## Primitives

::: castor.lib.primitives.tool

::: castor.lib.primitives.chat

::: castor.lib.primitives.budget

::: castor.lib.primitives.try_tool

## Spawn

::: castor.lib.spawn.spawn

::: castor.lib.spawn.join

## Patterns

::: castor.lib.patterns.parallel

::: castor.lib.patterns.react

::: castor.lib.patterns.map_reduce

::: castor.lib.patterns.plan_execute

::: castor.lib.patterns.conversation

::: castor.lib.patterns.supervisor

## Level 0: run_task

::: castor.lib.run_task.run_task
