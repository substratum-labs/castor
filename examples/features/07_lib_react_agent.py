"""Demo 07 — ReAct Agent & run_task: LLM-driven tool use with castor.lib.

Showcases two patterns:
  - react() — Level 1: explicit ReAct loop with tool list
  - run_task() — Level 0: one-sentence goal, auto-discovers tools

The LLM tool is a scripted mock (no API key needed).

Run:
    uv run python examples/features/07_lib_react_agent.py
"""

import asyncio

from castor import Castor, castor_tool
from castor.lib import react, run_task

# ── 1. Register tools ──

_search_db = {
    "weather paris": "Paris: 22°C, sunny",
    "weather tokyo": "Tokyo: 18°C, cloudy",
}


@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> str:
    """Look up information."""
    return _search_db.get(query.lower(), f"No results for '{query}'")


@castor_tool(consumes="api", cost_per_use=0.5)
async def calculator(expression: str) -> str:
    """Evaluate a math expression."""
    return str(eval(expression))  # noqa: S307 — demo only


# ── 2. Scripted LLM mock ──
# The LLM tool outputs ACTION/FINISH responses in sequence.

_llm_step = 0
_llm_script: list[str] = []


def _set_script(script: list[str]) -> None:
    global _llm_step, _llm_script
    _llm_step = 0
    _llm_script = script


@castor_tool(consumes="api", cost_per_use=0.1)
async def llm_inference(prompt: str, system: str = "") -> str:
    """Scripted LLM that follows a predetermined script."""
    global _llm_step
    if _llm_step >= len(_llm_script):
        return "FINISH: Script exhausted"
    response = _llm_script[_llm_step]
    _llm_step += 1
    print(f"  [LLM] {response}")
    return response


# ── 3. Run demos ──


async def main() -> None:
    kernel = Castor(tools=[search, calculator, llm_inference])

    # --- Demo A: react() — explicit tool list ---
    print("=== react() — ReAct Loop ===")
    print('  Goal: "What is the weather in Paris?"')
    print()

    _set_script([
        'ACTION: search({"query": "weather paris"})',
        "FINISH: The weather in Paris is 22°C and sunny.",
    ])

    async def react_agent() -> str:
        return await react(
            "What is the weather in Paris?",
            tools=["search", "calculator"],
        )

    cp = await kernel.run(react_agent, budgets={"api": 20.0})
    print(f"\n  Result: {cp.result}")
    print(f"  Steps: {len(cp.syscall_log)} syscalls")

    # --- Demo B: run_task() — one sentence, auto-discover tools ---
    print("\n=== run_task() — Level 0 API ===")
    print('  Goal: "Calculate 42 * 17"')
    print()

    _set_script([
        'ACTION: calculator({"expression": "42 * 17"})',
        "FINISH: 42 * 17 = 714",
    ])

    async def task_agent() -> str:
        # run_task auto-discovers tools from Gate registry
        return await run_task("Calculate 42 * 17")

    cp = await kernel.run(task_agent, budgets={"api": 20.0})
    print(f"\n  Result: {cp.result}")
    print(f"  Steps: {len(cp.syscall_log)} syscalls")


if __name__ == "__main__":
    asyncio.run(main())
