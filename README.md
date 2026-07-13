# Castor

[![CI](https://github.com/substratum-labs/castor/actions/workflows/ci.yml/badge.svg)](https://github.com/substratum-labs/castor/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/castor-kernel)](https://pypi.org/project/castor-kernel/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

<p align="center">
  <img src="assets/security_levels.png" alt="Castor: same agent, three execution modes from kernel primitives" width="900">
</p>

**An OS kernel for AI agents.** Treats agent execution like Unix treats processes — with syscalls, journaling, fork, memory management, scheduling, and resource accounting as kernel primitives. Any agent framework (LangChain, CrewAI, AutoGen, your own) runs on top and inherits these properties.

Castor is the kernel layer in the [Substratum Labs](https://substratumlabs.ai) ecosystem: kernel (this repo) + inference engine ([Mnemos](https://github.com/substratum-labs/mnemos)) + sandbox orchestrator ([Roche](https://github.com/substratum-labs/roche)) + platform ([castor-server](https://github.com/substratum-labs/castor-server)) + reference application ([Tiphys](https://github.com/substratum-labs/tiphys)). Sister kernel for real-time agents: [Pollux](https://github.com/substratum-labs/pollux).

---

## 🚀 Quick Start

```bash
pip install castor-kernel
```

```python
import asyncio
from castor import Castor, auto_approve
from castor.lib import tool

# Your existing tools — plain functions, no decorators needed
async def search(query: str) -> list[str]:
    return [f"Result for: {query}"]

async def delete_file(path: str) -> str:
    return f"Deleted {path}"

# Your agent — doesn't know about Castor
async def my_agent():
    results = await tool("search", query="old logs")
    await tool("delete_file", path="/tmp/old1")
    await tool("delete_file", path="/tmp/old2")
    return "Cleaned up"

async def main():
    kernel = Castor(
        tools=[search, delete_file],
        destructive=["delete_file"],       # gates require human approval
    )

    # Auto-approve for testing / trusted environments
    cp = await kernel.run_until_complete(my_agent, on_hitl=auto_approve)
    print(cp.result)  # "Cleaned up"

    # Speculative — full speed, review after
    cp = await kernel.run(my_agent, speculative=True)
    summary = kernel.scan(cp)
    print(f"{summary.total_steps} steps, {summary.flagged_count} need review")

asyncio.run(main())
```

`search` runs immediately. `delete_file` is gated — kernel inserts an HITL approval point, or flags for post-hoc review in speculative mode. **The agent doesn't know either way** — same code, different operator policy.

## Kernel Primitives

The capabilities Castor provides at the syscall layer. Build on them; don't rebuild them.

### Paper A secondary workloads

S-HITL (long suspend with approve/reject) and S-Loop (budget-runaway stop) are evaluation workloads that exercise the journal boundary. They are not separate Paper A contributions; the primary claims remain recoverable process state and effect safety.

### Journal

Every syscall (LLM call, tool invoke, memory op) is recorded in an append-only journal. The journal is the source of truth for replay, audit, and fork. Agent code cannot bypass it.

### Fork

Branch a session at any step. The prefix is shared (cached, no re-execution); the new branch diverges from there. Used for speculative execution, "what if I'd done it differently" debugging, A/B comparison of agent strategies.

```python
forked = checkpoint.fork(at_step=5)
cp2 = await kernel.run(my_agent, checkpoint=forked)
# Steps 1-4 replay from cache (free). Steps 5+ re-execute under new conditions.
```

### Memory management

Per-agent context window with backing cold storage. The kernel's MMU evicts when watermarks are exceeded; agents can also explicitly write/recall/pin via syscalls. Cold storage is namespaced by `agent_id` so memory persists across sessions — true cross-session learning is a kernel primitive, not an application hack.

### HITL gates + speculative execution

Tools can be marked `destructive`. The kernel inserts approval points before they execute, or in speculative mode runs them and flags for post-hoc review. Three operator policies — interactive approval, speculative + scan, full automation — selectable per session without changing agent code.

### Resource budget

Every syscall deducts from a budget (tokens, USD, custom resources). Budget exhaustion deterministically blocks the next syscall. Inheritable across spawn trees.

### Spawn / join

Multi-agent execution. Parent spawns children with a subset of its budget; children run, return results, unused budget refunded. Spawn tree is journaled.

### Checkpoint / replay

Suspend and resume across process restarts. Replay uses the journal — completed syscalls return cached results, no re-execution, no double-billing. Deterministic byte-identical to the original run.

Run `uv run python examples/security_levels.py` for HITL/Speculative/Time-Travel side-by-side, or `examples/features/09_fork_timeline.py` for the fork primitive in action.

## OS Analogy

| OS Concept | Castor Analog |
|---|---|
| User space / kernel space | Agent code / Castor kernel |
| System calls | `tool()` / `proxy.syscall()` |
| Process / fork / wait | Spawn / fork / join |
| Virtual memory + paging | Context window MMU + cold storage |
| Capabilities / cgroups | Depletable budget tokens |
| WAL / replay log | Journal |
| Signals / interrupts | HITL pause / preemption |
| `init` / `systemd` | castor-server |

Like Linux, your program (agent) uses libc (`castor.lib`) and never touches the kernel directly. The operator configures policy. Three roles, fully separated:

```
Tool developer:  writes plain functions (no Castor knowledge)
Agent developer: uses castor.lib.tool() (no kernel imports)
Operator:        Castor(tools=, destructive=, budgets=)
```

## CLI

Run agents from the command line — like a shell for AI agents:

```bash
castor run agent.py:main \
    --tool tools.py:search \
    --tool tools.py:delete_file --destructive \
    --budget api=50 \
    --speculative

castor ps                              # list agents
castor inspect <pid>                   # view checkpoint
castor approve <pid>                   # approve pending action
castor reject <pid> --reason "..."     # reject with feedback
```

Agent and tool code have zero Castor knowledge. The operator configures everything via CLI flags.

## Run any agent framework on Castor

Castor is a kernel, not a framework. LangChain, CrewAI, AutoGen, smolagents, pydantic-ai, openai-agents, google-adk — all run on top, all inherit kernel properties (journal, fork, memory, budget). Adapter extras ship with Castor:

```bash
pip install castor-kernel[langchain]   # or [crewai], [autogen], [smolagents], ...
```

Or write your own agent loop directly against `castor.lib.tool()`. See `examples/framework_guards/` for working integrations.

The framework runs the agent loop — Castor records, gates, budgets, and lets you fork it.

```python
from castor import Castor
from castor.lib import tool

# Your existing tools (unchanged)
async def web_search(query: str) -> str:
    return f"Results for: {query}"

async def delete_file(path: str) -> str:
    os.remove(path)
    return f"Deleted {path}"

# Your existing agent logic (unchanged) — could be a LangChain runnable, a CrewAI crew,
# a custom asyncio loop, anything that calls tools through castor.lib.tool()
async def my_agent():
    results = await tool("web_search", query="old temp files")
    for path in parse_paths(results):
        await tool("delete_file", path=path)
    return "Cleanup done"

# Operator adds Castor (one place, no changes to above)
kernel = Castor(
    tools=[web_search, delete_file],
    destructive=["delete_file"],
    budgets={"api": 20, "disk": 5},
)

cp = await kernel.run(my_agent, speculative=True)
summary = kernel.scan(cp)
print(f"{summary.total_steps} steps, {summary.flagged_count} need review")
```

The only requirement: tool calls go through `castor.lib.tool()`. The agent loop is yours.

## Sister projects

Castor is one piece of an agent OS. The full stack:

| Project | Role |
|---|---|
| **[castor-server](https://github.com/substratum-labs/castor-server)** | HTTP/SSE platform — multi-tenant agent deployment, Anthropic + OpenAI SDK adapters |
| **[Mnemos](https://github.com/substratum-labs/mnemos)** | Inference engine — GPU KV cache as a first-class OS resource |
| **[Roche](https://github.com/substratum-labs/roche)** | Sandbox orchestrator — capability-scoped Docker / Firecracker isolation |
| **[Pollux](https://github.com/substratum-labs/pollux)** | Real-time agent kernel — sister to Castor for soft + hard real-time workloads |
| **[Tiphys](https://github.com/substratum-labs/tiphys)** | Reference application — research agent built on Castor |

## Operator-layer security

Castor provides **application-layer control**: it gates what the agent *intends* to do (tool calls, budgets, approval, audit). It does **not** sandbox the process (filesystem, network). For defense in depth, run Castor inside a container or use [Roche](https://github.com/substratum-labs/roche). Castor controls intent; your sandbox controls capability.

## Documentation

- **[API Reference](https://substratum-labs.github.io/castor/)** — modules and classes
- **[Architecture & Guides](https://substratum-labs.github.io/castor-docs/)** — whitepaper, deep dives, getting started

### Paper A frozen S-Pay results

Regenerate the committed `full-n20` artifact (five systems × two faults × 20
trials) with:

```bash
uv sync --extra paper_a_eval && uv run python -m castor.evals.paper_a.matrix --out results/paper_a --label full-n20 --systems c_full c_no_op_id c_no_dedup b_naive b_langgraph --faults kill_after_commit kill_after_success --trials 20
```

`results.json` remains the row-level record; `run_manifest.json` identifies
the evaluated configuration. These are controlled S-Pay fault-injection
results, not claims about untested workloads or unconditional exactly-once
delivery.

## Contributing

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Development

```bash
git clone https://github.com/substratum-labs/castor.git
cd castor && uv sync
uv run pytest
uv run ruff check src/
```

## License

Apache 2.0. See [LICENSE](LICENSE).
