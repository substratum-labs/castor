# LLM Wrappers

Route LLM inference through the SyscallProxy for deterministic replay. Without these wrappers, LLM calls bypass the syscall log and break replay.

## Non-Streaming

```python
from castor import LLMSyscall

async def call_claude(model: str, prompt: str) -> str:
    response = await client.messages.create(
        model=model, max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

llm = LLMSyscall(registry, call_fn=call_claude, consumes="api_usd",
                  cost_per_use=0.03)

# In agent:
answer = await llm.infer(proxy, model="claude-3-5-sonnet", prompt="...")
```

## Streaming (Token-Level Preemption)

```python
from castor import StreamingLLMSyscall

async def stream_claude(model: str, prompt: str):
    with client.messages.stream(model=model, ...) as stream:
        for text in stream.text_stream:
            yield text

llm = StreamingLLMSyscall(
    registry, stream_fn=stream_claude, consumes="api_usd",
    cost_per_use=0.03, cost_per_token=0.0001,
    on_chunk=lambda chunk, acc: print(chunk, end=""),
)
```

## LLMSyscall

::: castor.llm.wrapper.LLMSyscall

## StreamingLLMSyscall

::: castor.llm.wrapper.StreamingLLMSyscall
