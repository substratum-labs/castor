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

`asyncio.Task.cancel()` injects a `CancelledError` at the next `await` point. In Python 3.9+, `CancelledError` is a `BaseException` (not `Exception`), so it won't be caught by typical `except Exception:` handlers. It propagates cleanly through agent code.

For I/O-bound LLM agents, the time between `await` points is typically milliseconds of CPU work (JSON parsing, string formatting). So `task.cancel()` is effectively immediate.

### Agent Timeline Under Preemption

```
Agent timeline:

  ===syscall 1===  ===compute===  ===syscall 2===  ===compute===
       |                |               |                |
  checkpoint       PREEMPT OK      checkpoint       PREEMPT OK
  consistent                       consistent
```

- **Between syscalls**: Agent is doing local computation or awaiting I/O. Fully cancellable. Work since last syscall is lost but will be recomputed identically on replay.
- **During syscall execution**: If cancelled mid-tool, the tool execution is interrupted. The result is NOT logged to `syscall_log`. On resume, the agent replays and re-issues the same syscall.

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

    def preempt(self):
        """Kernel calls this to preempt the agent immediately."""
        if self._task:
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

## New Agent Status

Add `PREEMPTED` to the `AgentCheckpoint.status` enum:

```python
status: Literal["RUNNING", "SUSPENDED_FOR_HITL", "PREEMPTED", "COMPLETED", "FAILED"]
```

`PREEMPTED` is handled identically to `SUSPENDED_FOR_HITL` for resume purposes — replay from `syscall_log`, re-execute from where it left off.

## Decision Record

| Decision | Chosen Approach | Rationale |
|---|---|---|
| Preemption mechanism | `asyncio.Task.cancel()` | True preemption with zero agent complexity |
| Shielded syscalls | Not needed | Fast/slow path separation prevents double-execution of destructive tools |
| Streaming interception | Deferred | Violates microkernel principle; adds agent-side complexity |
| Watchdog timers | Subsumed | Deadlines are one trigger for `task.cancel()`, not a separate mechanism |
| Terminology | "Preemptive scheduling" (accurate) | Kernel can preempt at any point; checkpoint/replay makes resume transparent |
