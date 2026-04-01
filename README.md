# Castor

[![CI](https://github.com/substratum-labs/castor/actions/workflows/ci.yml/badge.svg)](https://github.com/substratum-labs/castor/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/castor-kernel)](https://pypi.org/project/castor-kernel/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

<p align="center">
  <img src="assets/security_levels.png" alt="Castor: Same agent, three security levels" width="900">
</p>

**The secure execution layer for AI agents.** Budgets that cap spending. Human approval when limits are reached. Pause anywhere, resume later, replay deterministically.

Castor intercepts every tool call your agent makes, enforces resource limits, and suspends for human review when budgets run out. Your agent's business logic stays untouched. It's not a framework; it's the layer underneath.

---

## 🚀 Quick Start

```bash
pip install castor-kernel
```

```python
import asyncio
from castor import Castor, castor_tool
from castor.lib import tool, budget

# 1. Define tools: declare what costs money and what's dangerous
@castor_tool(consumes="api", cost_per_use=1)    # each call deducts 1 from "api" budget
async def search(query: str) -> list[str]:
    return [f"Result for: {query}"]

@castor_tool(consumes="disk", cost_per_use=1, destructive=True)  # deducts 1, suspends when budget exhausted
async def delete_file(path: str) -> str:
    return f"Deleted {path}"

# 2. Write your agent (plain async function, no special base class)
async def my_agent() -> str:
    results = await tool("search", query="old logs")
    await tool("delete_file", path="/tmp/old1")  # auto-executes if budget allows
    await tool("delete_file", path="/tmp/old2")
    return f"Cleaned up old logs"

# 3. Run with a budget (or use auto_budget=100 to infer limits from tool metadata)
async def main():
    kernel = Castor(tools=[search, delete_file])
    cp = await kernel.run(my_agent, budgets={"api": 10, "disk": 3})  # max 10 searches, 3 deletions

    print(cp.status)  # SUSPENDED_FOR_HITL, waiting for human

    # 4. Human approves, then resume from where it stopped
    await kernel.approve(cp)
    cp = await kernel.run(my_agent, checkpoint=cp)
    print(cp.result)

asyncio.run(main())
```

The agent calls `delete_file`, a destructive tool. Within budget, it auto-executes. When the budget runs out, Castor suspends execution, saves state, and waits for human approval. The kernel then replays from the top: cached responses for everything already executed, live execution from the suspension point. The agent doesn't know it was paused.

## 💡 Philosophy

Agent frameworks give LLMs tools. They don't control how those tools are used. You can add guardrails (wrappers, hooks, approval flows), but the agent still owns execution. The guardrails are advisory.

Castor inverts this. **The agent doesn't call tools. It requests them.** Every side effect is a syscall that passes through a kernel. The kernel validates, budgets, gates, and logs before anything executes. This makes Castor a secure execution layer.

But Castor is more than security. It's a **microkernel** that borrows from operating systems to give agents capabilities they can't get from guardrails alone: deterministic replay, process checkpointing, and context memory management.

| | OS Concept | Castor Analog |
|:---:|---|---|
| 🏗️ | User / Kernel space | LLM agent / Castor engine |
| 📞 | System calls | `proxy.syscall()` / `tool()` |
| 🎟️ | Capabilities | Depletable budget tokens |
| ⏯️ | Process scheduling | Checkpoint/replay with HITL |
| 🧠 | Virtual memory | Context window MMU |

Four subsystems handle these concerns: [Gate](https://substratum-labs.github.io/castor-docs/architecture/overview) (syscall validation), [Scheduler](https://substratum-labs.github.io/castor-docs/architecture/checkpoint-replay) (checkpoint/replay), [Capability Manager](https://substratum-labs.github.io/castor-docs/architecture/capability-model) (budgets), and [MMU](https://substratum-labs.github.io/castor-docs/architecture/mmu) (context memory). Everything else (policy, tools, agents) lives in user space.

## 🛡️ Guard Any Framework

Already using an agent framework? Castor works as a guard layer. Override your framework's tool execution hook, add budget + HITL, done.

<details>
<summary>LangChain / LangGraph</summary>

```python
from langgraph.prebuilt import ToolNode
from castor.capability.manager import CapabilityManager

cap = CapabilityManager()
caps = cap.create_capabilities({"api": 10, "disk": 3})
policies = {
    "web_search":   {"resource": "api",  "cost": 1},
    "delete_file":  {"resource": "disk", "cost": 1, "destructive": True},
}

async def castor_guard(request, execute):
    name = request.tool_call["name"]
    p = policies.get(name, {})
    if p.get("resource"):
        cap.deduct(caps, p["resource"], p["cost"])        # budget
    if p.get("destructive"):                               # HITL
        if input(f"Allow {name}? [y/n] ").strip() != "y":
            raise RuntimeError(f"{name} rejected")
    return await execute(request)

node = ToolNode(tools=tools, awrap_tool_call=castor_guard)
```

</details>

<details>
<summary>CrewAI</summary>

```python
from crewai.hooks import register_before_tool_call_hook
from castor.capability.manager import CapabilityManager

cap = CapabilityManager()
caps = cap.create_capabilities({"api": 10, "disk": 3})
policies = { ... }  # same as above

def castor_hook(context):
    name, args = context.tool_name, context.tool_input
    p = policies.get(name, {})
    if p.get("resource"):
        cap.deduct(caps, p["resource"], p["cost"])        # budget
    if p.get("destructive"):                               # HITL
        if input(f"Allow {name}? [y/n] ").strip() != "y":
            return False                                   # block
    return None                                            # proceed

register_before_tool_call_hook(castor_hook)
```

</details>

Same pattern for any framework: intercept, deduct, gate, delegate. See [`examples/framework_guards/`](examples/framework_guards/) for 7 frameworks including smolagents, pydantic-ai, OpenAI Agents SDK, AutoGen, and Google ADK.

## 🔧 Also Works As

<details>
<summary>CLI: run agents from the command line</summary>

```bash
castor run agent.py --budget api=50
```

```bash
castor ps                          # list agents
castor inspect <pid>               # view checkpoint state
castor approve <pid>               # approve pending action
castor reject <pid> --reason "..."  # reject with feedback
castor modify <pid> --feedback "..." # modify with guidance
```

</details>

<details>
<summary>MCP Server: guard any MCP-compatible agent</summary>

```python
# tools.py
from castor import castor_tool

@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> list[str]:
    return [f"Result for: {query}"]

@castor_tool(consumes="disk", destructive=True)
def delete_files(paths: list[str]) -> int:
    return len(paths)
```

```bash
castor-mcp --tools-module tools
```

Or add to Claude Desktop:

```json
{
  "mcpServers": {
    "castor": {
      "command": "castor-mcp",
      "args": ["--tools-module", "tools"]
    }
  }
}
```

</details>

<details>
<summary>SyscallProxy: direct proxy for streaming, preemption, sub-agents</summary>

```python
from castor import Castor, castor_tool, SyscallProxy

@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> list[str]:
    return [f"Result for: {query}"]

async def my_agent(proxy: SyscallProxy) -> str:
    results = await proxy.search(query="hello")
    remaining = proxy.capabilities["api"].remaining
    return f"Found: {results} ({remaining} budget left)"

kernel = Castor(tools=[search])
cp = await kernel.run(my_agent, budgets={"api": 50.0})
```

</details>

## 🔒 Security Scope

Castor provides **application-layer control**: it gates what the agent *intends* to do (tool calls, budgets, approval). It does **not** sandbox the process (filesystem, network). For defense in depth, run Castor inside a container or use [Roche](https://github.com/substratum-labs/roche), a sandbox orchestrator designed for AI agents. Castor controls intent; your infrastructure controls capability.

## 📚 Documentation

- **[API Reference](https://substratum-labs.github.io/castor/)**: All modules and classes
- **[Architecture & Guides](https://substratum-labs.github.io/castor-docs/)**: Whitepaper, deep dives, getting started

## 🤝 Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 🛠️ Development

```bash
git clone https://github.com/substratum-labs/castor.git
cd castor && uv sync
uv run pytest
uv run ruff check src/
```

## 📄 License

Apache 2.0. See [LICENSE](LICENSE).
