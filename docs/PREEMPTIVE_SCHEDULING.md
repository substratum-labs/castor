# Preemptive Scheduling in Castor

Design discussion resolving DESIGN_REVIEW.md Critical Question #2: "Preemptive" Is Actually Cooperative.

## The Problem

The original design documents called Castor Stream a "preemptive" scheduler, but what was described was interruption only at syscall boundaries — cooperative scheduling. The agent yields control at defined points (syscalls), and the kernel decides whether to proceed or suspend.

This left three risk scenarios unaddressed:

### Scenario 1: Infinite Reasoning Loop

```
Agent Function:
  syscall("search", {"q": "climate data"})    # kernel sees this
  ... LLM starts "thinking" ...                # kernel is blind
  ... LLM generates 50K tokens of reasoning ...# still blind
  ... never makes another syscall ...          # deadlock
```

The kernel never gets control back. No watchdog, no timer, no kill signal.

### Scenario 2: Slow Tool Execution

```
Agent Function:
  syscall("http_fetch", {"url": "slow-api.com"})  # enters kernel
  ... tool hangs for 10 minutes ...                # kernel is executing, but stuck
  ... HITL request arrives, can't be processed ... # blocked
```

### Scenario 3: Mid-Execution Abort

```
Human watches the agent streaming its plan:
  "I will now delete all files in /prod..."
  Human: STOP!
  But the kernel only checks at the next syscall boundary.
```

## Approaches Considered

### Approach A: Watchdog Timers

Add deadline timers at three levels:

- **`inference_timeout`**: Max time between two consecutive syscalls
- **`tool_timeout`**: Max execution time for any single tool (`asyncio.wait_for()`)
- **`total_agent_deadline`**: Global wall-clock limit

**Verdict:** Too weak. Only implements kernel-enforced deadlines, not preemptive semantics. Passive (waits for a deadline then kills) rather than active (interrupts on demand for any reason).

### Approach B: Streaming Interception

Kernel sits between the agent and the LLM API, intercepting every token:

```
Agent  --request-->  Kernel Proxy  --request-->  LLM API
Agent  <--tokens---  Kernel Proxy  <--stream---  LLM API
                          |
                     inspect each token
                     human abort -> cancel stream
```

**Verdict:** Rejected. This forces a design choice — either:
1. The agent routes LLM calls through the kernel (adds complexity to every agent), or
2. LLM inference becomes a syscall (the agent can't even "think" without kernel permission)

Both violate Castor's microkernel principle: **minimum mechanism in the kernel, maximum freedom in user space.** Castor controls side effects, not reasoning.

### Approach C: Checkpoint/Replay + `asyncio.Task.cancel()` (Chosen)

The key insight: **checkpoint/replay gives us preemption for free.**

In a traditional OS, preemption is hard because you must save arbitrary state (registers, stack, heap). In Castor's checkpoint/replay model, the `syscall_log` already captures all externally-visible state. Everything between two syscalls is pure recomputable work — given the same cached syscall responses, the agent will produce the same local variables, the same decisions, the same next syscall.

Therefore: **cancel the agent at any point, resume from the last checkpoint, lose nothing.**

## Chosen Design: Preemption via Task Cancellation

### Mechanism

`asyncio.Task.cancel()` injects a `CancelledError` at the next `await` point. In Python 3.9+, `CancelledError` is a `BaseException` (not `Exception`), so it won't be caught by typical `except Exception:` handlers. It propagates cleanly through the entire call stack — from the agent code, through `SyscallProxy`, down into tool implementations.

For I/O-bound LLM agents, the time between `await` points is typically milliseconds of CPU work (JSON parsing, string formatting). So `task.cancel()` is effectively immediate.

### Where Preemption Actually Matters: LLM Streaming

In a typical agent execution cycle, the time distribution is:

```
[LLM inference: 5-30 seconds] → [tool execution: 0.1-2 seconds] → repeat
```

90%+ of wall-clock time is spent in the LLM streaming call. This is where preemption matters most — and where it works most naturally. An LLM streaming call is an async iteration:

```python
async for chunk in llm_stream(...):
    partial_response += chunk
    # Each iteration hits __anext__() — an await point
    # CancelledError can be injected here at every chunk
```

Each chunk boundary is an `await` point. `task.cancel()` takes effect within one chunk latency (typically 10-100ms). No special `await asyncio.sleep(0)` is needed — real LLM streaming APIs (aiohttp, httpx) have natural `await` points at every chunk.

### Agent Timeline Under Preemption

```
Agent timeline:

  ===syscall 1===  ===LLM streaming===  ===syscall 2===  ===LLM streaming===
       |           ↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑         |           ↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑
  checkpoint    every chunk is a         checkpoint     every chunk is a
  consistent    preemption point         consistent     preemption point
```

- **During LLM streaming** (dominant time window): Each chunk boundary is an `await` point. Preemption is fine-grained — effectively token-level.
- **Between syscalls** (other computation): Agent is doing local computation or awaiting I/O. Fully cancellable at any `await`.
- **During tool execution**: If cancelled mid-tool, the tool execution is interrupted. The result is NOT logged to `syscall_log`. On resume, the agent replays and re-issues the same syscall.

### How Cancellation Propagates Through the Call Stack

When a tool is executing, `task.cancel()` propagates through the entire stack:

```
AgentRunner.run()
  └── agent_fn(proxy)                          # agent code
        └── proxy.syscall("http_fetch", ...)   # proxy
              └── castor_dam.execute(...)       # dam
                    └── tool_impl(...)          # tool code
                          └── await aiohttp.get(...)  ← CancelledError injected here
                                ↓ propagates up through every frame
                          → tool_impl()     ← propagates
                        → dam.execute()     ← propagates
                      → proxy.syscall()     ← propagates
                    → agent_fn()            ← propagates
                  → AgentRunner.run()       ← caught, sets PREEMPTED
```

The tool's I/O is interrupted, connections are closed, and no result is logged. On resume, the entire syscall is re-issued.

### Tool Execution and Interruptability

The kernel, not the tool author, is responsible for ensuring tools are interruptable. Since `task.cancel()` propagates down to whatever `await` is active inside the tool, most tools are interruptable natively:

| Tool type | Why it's interruptable |
|---|---|
| Async I/O (HTTP, DB, file) | `await` points in I/O operations |
| Subprocess (shell, scripts) | `await proc.communicate()` + kernel can `proc.kill()` |
| LLM API calls | `await` points at every stream chunk |

The edge case is purely synchronous CPU-bound tools with no `await` points. These block the event loop — an asyncio anti-pattern regardless of Castor. The standard solution is `run_in_executor`, but even then the thread continues running after cancellation (Python threads cannot be forcibly killed). For true interruptability of CPU-bound work, the kernel runs it in a `ProcessPoolExecutor` and kills the worker process:

```python
# Inside castor_dam.execute() for sync CPU-bound tools:
result = await loop.run_in_executor(process_pool, tool_fn, args)
# On preemption: kill the worker process via os.kill(pid, SIGKILL)
```

In practice, CPU-bound tools are rare in LLM agent workloads. The vast majority of time is spent in LLM streaming and I/O-bound tool calls, both of which are natively interruptable.

### Why No Shielded Critical Sections

An earlier version proposed `asyncio.shield()` around tool execution to prevent cancellation mid-tool. This is unnecessary because the fast/slow path separation already handles it:

- **Slow path** (destructive, HITL-required): These raise `SuspendInterrupt` **before** `execute()`. They never reach tool execution without human approval. Cancellation can't double-execute them.
- **Fast path** (safe, non-destructive): Search, read, query, compute. These are inherently **idempotent**. If cancelled mid-execution and re-issued on replay, the re-execution is safe.

The simpler model: cancel anywhere, resume from last completed syscall, the fast/slow split protects against double-execution of dangerous operations.

### Preemption Triggers

| Trigger | Example |
|---|---|
| Human abort | User hits "stop" button |
| Budget exhaustion | Capability depleted mid-run |
| Deadline exceeded | Wall-clock timeout |
| Priority scheduling | Higher-priority agent needs resources |
| Policy violation | Content filter, safety check |

All triggers are kernel-side decisions. The agent never sees them.

### AgentRunner Implementation

```python
class AgentRunner:
    def __init__(self):
        self._task: asyncio.Task | None = None

    async def run(self, agent_fn, checkpoint: AgentCheckpoint):
        proxy = SyscallProxy(checkpoint)
        try:
            result = await agent_fn(proxy)
            checkpoint.status = "COMPLETED"
        except SuspendInterrupt:
            pass  # cooperative suspend (HITL), checkpoint already set
        except asyncio.CancelledError:
            # preemption lands here
            checkpoint.status = "PREEMPTED"
            # checkpoint.syscall_log is consistent up to last completed syscall
            # resume later via replay — identical to HITL resume

    def preempt(self, reason: str, payload: dict | None = None):
        """Kernel calls this to preempt the agent immediately."""
        if self._task and not self._task.done():
            # Store preemption context on checkpoint before cancelling
            checkpoint = self._current_checkpoint
            checkpoint.preemption_reason = reason
            checkpoint.preemption_payload = payload
            self._task.cancel()
```

### SyscallProxy — No Changes Needed

The existing `SyscallProxy.syscall()` method requires no modification for preemption. The cancellation mechanism is entirely external to the proxy:

```python
class SyscallProxy:
    async def syscall(self, tool_name: str, arguments: dict) -> Any:
        request = {"tool_name": tool_name, "arguments": arguments}

        # Replay path — serve cached response
        if self._replay_index < len(self.checkpoint.syscall_log):
            record = self.checkpoint.syscall_log[self._replay_index]
            assert record.request == request
            self._replay_index += 1
            return record.response

        # HITL check — cooperative suspend
        if tool_meta.requires_hitl:
            self.checkpoint.pending_hitl = request
            self.checkpoint.status = "SUSPENDED_FOR_HITL"
            raise SuspendInterrupt(self.checkpoint)

        # Fast path — execute tool
        result = await castor_dam.execute(validated)
        # CancelledError can fire at the await above — that's fine.
        # If it does, result is never logged, and replay will re-execute.

        self.checkpoint.syscall_log.append(
            SyscallRecord(request=request, response=result)
        )
        return result
```

### Agent Code — Zero Complexity Added

```python
async def my_agent(proxy: SyscallProxy) -> str:
    data = await proxy.syscall("search", {"q": "climate data"})
    report = await proxy.syscall("summarize", {"text": data})
    await proxy.syscall("send_email", {"to": "boss", "body": report})
    return "done"
```

The agent has no idea preemption exists. No error handling, no hooks, no registration. The kernel manages everything transparently.

## OS Analogy

| OS Preemption | Castor Preemption |
|---|---|
| Timer interrupt fires | Kernel calls `task.cancel()` |
| CPU saves registers to PCB | `CancelledError` caught; checkpoint already consistent |
| Kernel decides: resume or switch | Kernel decides: resume, queue, or terminate |
| Resume: restore registers from PCB | Resume: replay agent from `syscall_log` |

The non-preemptible window (between `await` points) is analogous to briefly disabled interrupts in an OS kernel — typically microseconds for I/O-bound agents.

## Preemption Context: Awareness on Resume

### The Problem with Blind Resume

A naive preemption model discards all work since the last syscall and resumes via blind replay. The agent has no idea it was interrupted, why, or what partial progress it made.

An alternative architecture (kernel-owned agent loop with token-level streaming interception) solves this by saving partial LLM output and injecting interrupt reasons into the agent's message history. However, that approach couples the kernel to the LLM streaming protocol and removes agent flexibility — violating the microkernel principle.

Castor adopts the useful ideas without the architectural compromise: **preemption metadata on the checkpoint**.

### Checkpoint Extension

Add preemption context fields to `AgentCheckpoint` — outside the `syscall_log`, not part of the deterministic replay:

```python
class AgentCheckpoint(BaseModel):
    # ... existing fields ...
    syscall_log: list[SyscallRecord] = []           # deterministic replay

    # Preemption context (informational, not part of replay)
    preemption_reason: str | None = None             # why it was preempted
    preemption_payload: dict | None = None            # data from the interrupter
    partial_work: str | None = None                   # mid-thought output, if any
```

- **`preemption_reason`**: Why the agent was interrupted (e.g., `"HUMAN_ABORT"`, `"BUDGET_EXHAUSTED"`, `"PRIORITY_PREEMPT"`)
- **`preemption_payload`**: Structured data from the interrupter (e.g., `{"instruction": "Stop deletion, report progress"}`)
- **`partial_work`**: Any mid-thought output the agent had produced before interruption (e.g., a partial LLM response). Saved as a hint, not as part of the deterministic log.

### Resume Flow with Context

```
Resume flow:
  1. Replay syscall_log (deterministic — serve cached responses)
  2. Catch up to where the agent left off
  3. If preemption_reason exists:
     → Inject context into agent's next interaction:
       "You were interrupted. Reason: {reason}. Data: {payload}.
        Partial progress: {partial_work}. Handle this before continuing."
  4. Agent continues with awareness of what happened
  5. Clear preemption fields after injection
```

This gives the agent the ability to adapt — it might change its plan, report status, or handle a higher-priority task — without any special handling in the agent code. The injection is done by the kernel (SyscallProxy or AgentRunner), not by the agent.

### Why This Preserves Replay Determinism

The preemption context is **not** part of the `syscall_log`. It doesn't affect replay of previous syscalls. It's injected *after* replay catches up, as new context for the agent's next action. This means:

- Replaying a checkpoint without preemption context produces the same syscall sequence up to the preemption point
- The preemption context only affects the agent's behavior *after* the preemption point — which is inherently non-deterministic anyway (the LLM will generate different tokens)

## New Agent Status

Add `PREEMPTED` to the `AgentCheckpoint.status` enum:

```python
status: Literal["RUNNING", "SUSPENDED_FOR_HITL", "PREEMPTED", "COMPLETED", "FAILED"]
```

`PREEMPTED` is handled identically to `SUSPENDED_FOR_HITL` for resume purposes — replay from `syscall_log`, re-execute from where it left off. The difference is that `PREEMPTED` may carry preemption context (reason, payload, partial work) that is injected on resume.

## Comparison with Alternative Architecture

An alternative approach (analyzed from an external reference implementation) places the kernel in direct control of the LLM streaming loop:

```python
# Alternative: kernel owns the agent loop
async def _run_agent_loop(self, process):
    async for chunk in llm_stream(process.messages):
        partial_response += chunk
        await asyncio.sleep(0)  # explicit yield point
```

| Aspect | Alternative (kernel-owned loop) | Castor (microkernel) |
|---|---|---|
| Agent flexibility | Fixed loop pattern | Arbitrary async functions |
| Kernel complexity | High (embeds LLM protocol) | Low (only manages tasks) |
| Preemption granularity | Token-level (explicit) | Token-level (natural await points) |
| Replay determinism | None | Full (syscall_log) |
| Partial state saving | Built-in | Via preemption metadata |
| Agent complexity | Must follow kernel's loop | Zero — just use `proxy.syscall()` |

Castor achieves the same preemption granularity without coupling the kernel to the LLM streaming protocol. The `async for chunk in stream:` pattern naturally provides `await` points at every chunk — no `asyncio.sleep(0)` workaround needed.

## Decision Record

| Decision | Chosen Approach | Rationale |
|---|---|---|
| Preemption mechanism | `asyncio.Task.cancel()` | True preemption with zero agent complexity |
| Shielded syscalls | Not needed | Fast/slow path separation prevents double-execution of destructive tools |
| Streaming interception | Rejected | Violates microkernel principle; unnecessary — LLM streams have natural await points |
| Kernel-owned agent loop | Rejected | Removes agent flexibility; couples kernel to LLM protocol |
| Watchdog timers | Subsumed | Deadlines are one trigger for `task.cancel()`, not a separate mechanism |
| Tool interruptability | Kernel's responsibility | Async I/O: native. Subprocess: `proc.kill()`. CPU-bound: `ProcessPoolExecutor` + kill |
| Preemption context | Metadata on checkpoint | Agent-aware resume without breaking replay determinism |
| Terminology | "Preemptive scheduling" (accurate) | Kernel can preempt at any point; checkpoint/replay makes resume transparent |
