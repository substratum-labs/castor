# Checkpoint/Replay Execution Model

Design discussion resolving DESIGN_REVIEW.md Critical Question #1: Coroutine Serialization.

## The Problem

Castor needs to suspend agent execution (for HITL approval) and resume it later — potentially hours or days later, possibly in a different process. The original design documents proposed "serializing the coroutine state" — pickling local variables, the call stack, and the event loop context.

**This is impossible in Python.** `asyncio` coroutines hold:

- C-level stack frames (not accessible from Python)
- References to the event loop (process-specific)
- Closures over mutable state
- File handles, network connections, and other non-serializable resources

You cannot `pickle.dumps()` a live coroutine. This was the single hardest technical challenge in the original design, and it was a `pass` in the DDD.

## Two Approaches Analyzed

### Approach A: Explicit State Machine

Convert every agent function into a state machine with serializable state.

```python
class AgentPhase(str, Enum):
    SEARCH = "search"
    ANALYZE = "analyze"
    REPORT = "report"
    DONE = "done"

class AgentState(BaseModel):
    phase: AgentPhase = AgentPhase.SEARCH
    search_results: Optional[list] = None
    analysis: Optional[str] = None

async def research_agent(state: AgentState, kernel) -> AgentState:
    if state.phase == AgentPhase.SEARCH:
        state.search_results = await kernel.execute("web_search", {"q": "topic"})
        state.phase = AgentPhase.ANALYZE
        return state

    if state.phase == AgentPhase.ANALYZE:
        state.analysis = await kernel.execute("llm_call", {"prompt": f"analyze {state.search_results}"})
        state.phase = AgentPhase.REPORT
        return state

    if state.phase == AgentPhase.REPORT:
        await kernel.execute("send_email", {"body": state.analysis})
        state.phase = AgentPhase.DONE
        return state
```

The kernel calls the function repeatedly, once per phase. State is a Pydantic model — trivially serializable.

**Pros:**
- State is explicit and inspectable
- Every intermediate value is named and typed
- Easy to reason about

**Cons:**
- **Extremely verbose.** Every agent becomes a state machine. Agent authors must manually manage phases, transitions, and intermediate storage.
- **Scales poorly.** An agent with 10 tool calls needs 10+ phases. Conditional logic (if/else on tool results) creates phase explosion.
- **Unnatural.** Agent logic is spread across phases instead of reading top-to-bottom. The cognitive load for agent authors is high.
- **Couples agent to kernel.** The agent must know it's running in a suspend/resume system and structure its code accordingly.

### Approach B: Checkpoint at Syscall Boundary (Chosen)

Agent functions are plain `async def` functions. The kernel records a log of completed syscalls. On resume, it replays the function from the top, serving cached responses.

```python
async def research_agent(proxy: SyscallProxy) -> str:
    results = await proxy.syscall("web_search", {"q": "topic"})
    analysis = await proxy.syscall("llm_call", {"prompt": f"analyze {results}"})
    await proxy.syscall("send_email", {"body": analysis})
    return "done"
```

The agent reads naturally — top to bottom, no phases, no state management. The `SyscallProxy` handles everything:

```python
class SyscallProxy:
    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint
        self._replay_index = 0

    async def syscall(self, tool_name: str, arguments: dict) -> Any:
        request = {"tool_name": tool_name, "arguments": arguments}

        # Replay: return cached response
        if self._replay_index < len(self.checkpoint.syscall_log):
            record = self.checkpoint.syscall_log[self._replay_index]
            assert record.request == request  # determinism check
            self._replay_index += 1
            return record.response

        # New syscall: validate, check HITL, execute or suspend
        # ... (see DDD Section 3.4 for full implementation)
```

**How suspend/resume works:**

```
First run:
  syscall("web_search", ...)     → executes, logs result, returns
  syscall("llm_call", ...)       → executes, logs result, returns
  syscall("send_email", ...)     → destructive! raises SuspendInterrupt
  ↓
  Coroutine destroyed. Checkpoint saved: syscall_log = [web_search, llm_call]

Resume (after human approval):
  syscall("web_search", ...)     → replay_index=0, return cached result
  syscall("llm_call", ...)       → replay_index=1, return cached result
  syscall("send_email", ...)     → replay_index=2, past cache → execute live
  ↓
  Agent completes normally
```

The agent function runs twice, but the first two syscalls are served from cache — instant, deterministic, no side effects. Only the third syscall executes live.

## Why Approach B Was Chosen

### 1. Agent Simplicity

Approach A requires agent authors to think about serialization, phases, and state management. Approach B requires nothing — write a normal async function, use `proxy.syscall()` for tools.

| Approach A | Approach B |
|---|---|
| Agent must define state model | No state model needed |
| Agent must manage phase transitions | Natural control flow |
| Conditional logic → phase explosion | Normal if/else works |
| Agent knows it's in a kernel | Agent is kernel-agnostic |

### 2. Determinism Is Free

LLM agent functions are naturally deterministic between syscalls: given the same tool responses, the agent makes the same decisions (because the LLM calls are themselves syscalls). This means replay produces the same syscall sequence without any special effort.

The determinism guarantee: on replay, the agent function produces the same sequence of `proxy.syscall()` calls. The proxy verifies this with an assertion on each replayed request.

### 3. Inspired by Proven Systems

The checkpoint/replay model is battle-tested in:

- **Temporal.io** — Durable execution for microservices. Activities (side effects) are recorded; workflows (orchestration) are replayed.
- **Azure Durable Functions** — Same model for serverless orchestration.
- **Event Sourcing** — State is the replay of an event log, not a mutable snapshot.

Castor's `syscall_log` is equivalent to Temporal's activity log. The `SyscallProxy` is equivalent to the replay-aware SDK client.

### 4. Sub-Agent Spawning Is Natural

`spawn_agent` is just another syscall. The child's entire execution (its own `AgentCheckpoint` with its own `syscall_log`) becomes the response of the parent's spawn syscall. On parent replay, the child is not re-run — its result is served from the parent's cache.

This elegantly handles nested execution: the parent doesn't need to know whether the child suspended, was preempted, or ran to completion. It just gets the result.

### 5. Preemption Is Free

Because the checkpoint (syscall_log) is always consistent up to the last completed syscall, the kernel can cancel the agent at any point and resume later. No additional mechanism is needed — the same replay that handles HITL suspension also handles preemption. See `docs/PREEMPTIVE_SCHEDULING.md`.

## Trade-offs and Limitations

### Replay Must Be Deterministic

The agent function must produce the same syscall sequence when given the same cached responses. This means:

- **No random side effects between syscalls.** If the agent calls `random.random()` to decide which tool to call, replay will diverge. In practice, this is not a problem because LLM agents make decisions via LLM calls, which are themselves syscalls.
- **No external state reads between syscalls.** If the agent reads a file directly (bypassing `proxy.syscall()`), the result may differ between runs. All external interactions must go through the proxy.

### Replay Cost

On resume, the agent function re-executes from the top. Cached syscalls return instantly, but any pure computation between syscalls is re-executed. For typical LLM agents, this computation is trivial (JSON parsing, string formatting) — milliseconds per replay.

For agents with hundreds of syscalls, replay involves hundreds of cache lookups — still fast, but the linear cost should be noted.

### Debugging Replays

A replay divergence (assertion failure in the proxy) means the agent function is non-deterministic. This can be debugged by comparing the expected request (from the log) with the actual request (from the re-execution). The proxy's assertion message includes both values.

## Decision Record

| Decision | Chosen | Rationale |
|---|---|---|
| Execution model | Checkpoint/Replay (Approach B) | Agent simplicity, natural control flow, determinism is free |
| State representation | `syscall_log` (replay journal) | Plain Pydantic model, trivially serializable |
| Replay mechanism | `SyscallProxy` serves cached responses | Transparent to agent, determinism-checked |
| Alternative rejected | Explicit State Machine (Approach A) | Too verbose, scales poorly, couples agent to kernel |
