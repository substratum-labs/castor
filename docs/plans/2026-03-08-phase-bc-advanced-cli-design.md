# Phase B Advanced + Phase C CLI Design

## Summary

Phase B advanced adds high-level agent patterns to `castor.lib` (parallel, react, map_reduce, plan_execute, conversation, supervisor, run_task). Phase C replaces the single-file CLI with a `cli/` package supporting `castor run`, `castor ps`, `castor approve`, etc.

Implementation order: Phase B first (patterns are kernel capability), then Phase C (CLI is thin shell on top).

## Design Decisions

1. **Scope**: All 6 patterns + run_task + full CLI — implement everything to attract users
2. **CLI structure**: `cli/` package with submodules (run, process, hitl, resume), not monolithic file
3. **Agent loading**: `file:func` explicit + convention fallback (`agent`/`main`), like uvicorn
4. **react() tools**: Explicit `tools=["search", "calc"]` parameter — no implicit discovery for safety
5. **run_task() LLM**: Requires pre-registered LLM tool in Gate — no built-in LLM dependency
6. **Primitive types only**: All `castor.lib` signatures use `str`, `dict`, `list`, `int`, `float` — future cross-language POSIX compat

## Phase B Advanced: `castor.lib.patterns` + `castor.lib.run_task`

### File: `src/castor/lib/patterns.py`

Six pattern functions built on existing primitives (`tool()`, `chat()`, `spawn()`, `join()`):

```python
async def parallel(*tool_calls: tuple[str, dict]) -> list[Any]:
    """Fan-out/fan-in: concurrent tool calls, results in order."""

async def react(
    goal: str,
    tools: list[str],
    *,
    max_steps: int = 10,
    tool_name: str = "llm_inference",
) -> str:
    """ReAct loop: Think -> Act -> Observe until FINISH."""

async def map_reduce(
    items: list[Any],
    map_tool: str,
    reduce_tool: str,
) -> Any:
    """Parallel map over items, then reduce to single result."""

async def plan_execute(
    goal: str,
    planner_tool: str,
    executor_tools: list[str],
    *,
    tool_name: str = "llm_inference",
) -> str:
    """Planner generates step list, executor runs each step."""

async def conversation(
    system: str,
    *,
    max_turns: int = 20,
    tool_name: str = "llm_inference",
    input_tool: str = "user_input",
) -> list[dict]:
    """Multi-turn chat: user_input -> LLM -> repeat."""

async def supervisor(
    task: str,
    agents: list[str],
    *,
    tool_name: str = "llm_inference",
    max_rounds: int = 5,
) -> str:
    """Supervisor LLM delegates to sub-agents, collects results."""
```

### File: `src/castor/lib/run_task.py`

```python
async def run_task(
    goal: str,
    *,
    tools: list[str] | None = None,
    max_steps: int = 10,
    tool_name: str = "llm_inference",
) -> str:
    """Level 0 API: one-sentence goal, auto ReAct execution.

    - tools=None -> discover all registered tools (excluding LLM tool)
    - tools=[...] -> use only specified tools
    - Delegates to react() internally
    """
```

### Key Design Points

- All LLM calls go through `chat(prompt, tool_name=...)` — no LLM dependency
- `parallel()` uses `spawn()` + `join()` — leverages existing async spawn
- `react()` parses `ACTION: tool_name(args)` and `FINISH: result` from LLM output
- `run_task()` wraps `react()` — tool auto-discovery via proxy Gate access

## Phase C: CLI Package

### File Structure

```
src/castor/cli/
  __init__.py    # main() + argparse root parser
  run.py         # castor run
  process.py     # castor ps, inspect, kill
  hitl.py        # castor approve, reject, modify
  resume.py      # castor resume
```

### Commands

```bash
# Agent execution
castor run agent.py                          # convention: find agent() or main()
castor run agent.py:my_func                  # explicit function
castor run --budget api_usd=0.50 agent.py    # budget constraints
castor run --hitl interactive agent.py       # HITL policy (auto/interactive)
castor run --store sqlite:///castor.db agent.py  # persistence

# Process management
castor ps                                    # list agent processes
castor inspect <pid>                         # checkpoint details
castor kill <pid>                            # preempt (requires run_async mode)

# HITL
castor approve <pid>
castor reject <pid> --reason "too dangerous"
castor modify <pid> --feedback "use X instead"

# Resume
castor resume <pid>                          # resume from checkpoint
```

### Key Design Points

- Agent loading: `importlib` dynamic import, signature detection for new/legacy mode
- `--budget`: parse `key=value` pairs into `dict[str, float]`
- `--hitl interactive`: uses `run_until_complete()` + terminal interaction
- `--store` required for ps/inspect/resume (default: `sqlite:///castor.db`)
- Child HITL not supported via CLI (requires runtime)
- Old `src/castor/cli.py` replaced; pyproject.toml entry point updated

## Testing

- **Phase B**: `tests/test_lib_patterns.py`, `tests/test_lib_run_task.py` — mock tools, no real LLM
- **Phase C**: `tests/test_cli/` directory — test_run.py, test_process.py, test_hitl.py, test_resume.py
- Patterns tests: mock LLM returns preset ACTION/FINISH sequences
- CLI tests: invoke `main()` with argv mock or subprocess
