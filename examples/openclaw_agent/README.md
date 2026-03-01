# OpenClaw Agent Example

A personal AI assistant built on the Castor microkernel, inspired by [OpenClaw](https://openclaw.ai/). Demonstrates all major Castor features in a realistic scenario.

## What It Does

The agent receives a research request, then:

1. **Plans** using LLM inference (replay-safe via `LLMSyscall`)
2. **Searches** the web for information (safe tool)
3. **Reads** existing notes from a local knowledge base (safe tool)
4. **Writes** a summary note (safe tool)
5. **Composes** a notification message via LLM
6. **Sends** the message to Slack (destructive — triggers HITL suspension)

If the human rejects the message send, the agent falls back to saving a draft note.

## Castor Features Demonstrated

| Feature | Where |
|---------|-------|
| `@castor_tool` registration | `tools.py` — 5 tools (safe + destructive) |
| `LLMSyscall` wrapper | `agent.py` — LLM calls routed through proxy |
| Capability budgets | `run.py` — network=50, disk=20 |
| HITL suspend/approve/reject | `run.py` — interactive CLI prompt |
| Checkpoint persistence | `run.py` — SQLite via `CheckpointStore` |
| Replay determinism | `test_openclaw.py` — LLM not re-called on resume |

## Running

```bash
# Interactive demo
uv run python examples/openclaw_agent/run.py

# Tests
uv run pytest examples/openclaw_agent/test_openclaw.py -v
```

## File Structure

```
examples/openclaw_agent/
├── README.md          ← you are here
├── agent.py           ← agent function (the "brain")
├── tools.py           ← tool definitions (the "hands")
├── run.py             ← CLI entry point with HITL interaction
└── test_openclaw.py   ← tests covering approve, reject, budget
```

## Adapting for Production

To use with a real LLM provider, replace the `fake_llm_client` in `run.py`:

```python
import openai

client = openai.AsyncOpenAI()

async def real_llm(model: str, prompt: str) -> str:
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content

llm = LLMSyscall(registry, call_fn=real_llm, consumes="api_usd", cost_per_use=0.03)
```

Similarly, replace the stub tool implementations in `tools.py` with real API calls (Slack SDK, search APIs, etc.). Castor handles the budgeting, replay safety, and HITL flow — your tools just need to do the work.
