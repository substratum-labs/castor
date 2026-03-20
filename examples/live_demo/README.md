# Live Demo

Interactive demo using a real LLM (via LiteLLM) and a Rich terminal UI. Shows two acts:

1. **Live Research**: agent calls a real LLM, executes tools, triggers HITL approval for a destructive action
2. **Crash Recovery**: replays from the saved checkpoint with zero API calls, identical result

## Running

```bash
# Install demo dependencies
uv sync --extra demo

# Run with Anthropic Claude
ANTHROPIC_API_KEY=sk-... uv run python examples/live_demo/run.py

# Run with OpenAI
OPENAI_API_KEY=sk-... uv run python examples/live_demo/run.py --model gpt-4o

# Custom topic
uv run python examples/live_demo/run.py "battery technology breakthroughs"
```
