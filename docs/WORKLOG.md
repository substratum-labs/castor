# Castor — Worklog

A living collaboration surface between agents. Organized by state, not chronology.
Newest entries first within each section. Prune aggressively — git is the permanent record.

**Conventions:** Tag entries `[CC]` (Claude Code) or `[RA]` (Research Agent) for provenance.

---

## Current Focus

- [CC] **M4 COMPLETE** — All Phase 1 milestones (M1–M4) delivered. 170 tests, 0 lint errors.
- [CC] **Async spawn/join** — `spawn_agent_async` + `join_agent` syscalls for fan-out/fan-in parallelism. Budget delegated at spawn, reclaimed at join. HITL propagation at join time. Replay serves cached handle/result.
- [CC] **CLI for HITL** — `castor list|show|reject|modify` via `[project.scripts]` entry. Approve excluded (requires runtime). Child HITL guarded with `is_child_hitl()` check.
- [CC] **Code review fixes** — PID collision between sync/async spawns fixed (shared counter). FAILED status on async child exception. Budget leak guard in async spawn. CLI child HITL guard. Mixed-spawn regression tests.

---

## Open Questions

_Questions needing exploration or a design decision. Research Agent can pick these up._

- ~~**Lodge design (M3):**~~ RESOLVED — FIFO eviction with watermark, HAL via `SemanticMemoryDriver` ABC, page-in via `search_memory` syscall.
- ~~**Sub-agent spawning (M4):**~~ RESOLVED — Separate `spawn_agent_async` + `join_agent` syscalls. Child HITL propagates at join time via `_propagate_child_suspension`.
- ~~**Agent return value:**~~ RESOLVED — Stored in `AgentCheckpoint.result`.
- **Phase 2 planning:** When to start Rust/PyO3 core? Which subsystem first (Dam validation is hot path)?
- **Async spawn observability:** Child checkpoints not persisted at spawn time (only at join). Orphaned tasks on parent preemption produce warnings but are GC'd.

---

## Research Notes

For **Lodge design (M3)**, 

Castor Lodge (M3) 架构实施指南：面向 Coding AgentTarget: Coding Agent (Claude 3.5 Sonnet / GPT-4o etc.)Phase: M3 (The Agentic MMU - Virtual Memory for LLMs)Context: Project Castor 是一个基于微内核和强确定性重放（Replay）的 Agent OS。0. 架构师的最高指令 (Prime Directives)在开始编写任何 Lodge 代码之前，你必须遵守以下三大架构铁律：策略与机制分离 (HAL)： CastorLodge 是一个内存控制器 (MMU)。你绝对不允许在 Lodge 的核心代码中 import 任何具体的 Embedding 模型（如 OpenAI embeddings）或向量数据库 SDK。所有的提取和存储脏活，必须委托给 SemanticMemoryDriver 接口。所有副作用必须经过 Proxy (The Syscall Barrier)： Lodge 对记忆的换出（Page-Out）绝对不允许绕过 SyscallProxy 直接写库。换出动作必须是一个内部的系统调用工具（例如 sys_kernel_page_out），通过 proxy.syscall() 路由执行。Proxy 层会自动处理重放逻辑，Lodge 内部严禁出现 is_replaying 的判断代码。绝对锁定 (Pinned VRAM)： SystemPrompt 和带 HITL 标记的人类干预记录，绝对不允许被换出。1. 核心接口与抽象定义 (The HAL Interfaces)任务 1.1：实现驱动接口契约在 castor/lodge/driver.py 中定义抽象基类。这是 Lodge 向下兼容 Mem0/Pinecone 的硬件抽象层。from abc import ABC, abstractmethod
from typing import List, Dict, Any

class SemanticMemoryDriver(ABC):
    @abstractmethod
    async def ingest(self, evicted_messages: List[Dict[str, Any]], pid: str) -> bool:
        """接收被内核踢出的冗余对话，在后台进行向量化/实体提取并存储。"""
        pass
        
    @abstractmethod
    async def search(self, query: str, pid: str, **kwargs) -> str:
        """根据查询意图，从向量库中检索语义记忆，并返回格式化的文本结果。"""
        pass
任务 1.2：实现 Token 计数器协议在 castor/lodge/token_counter.py 中定义计算 Token 的协议，不要将系统与 tiktoken 死锁。from typing import Protocol

class TokenCounter(Protocol):
    def count(self, text: str) -> int:
        ...

# 提供一个基于 tiktoken 的默认实现 (TiktokenCounter)
任务 1.3：实现 Mock 驱动在 castor/lodge/drivers/mock_sqlite_driver.py 中，实现一个基于简单文本追加和匹配的 Dummy Driver，用于集成测试。2. 状态模型升级 (State Model Modifications)在 castor.models 中，将现有的 context_history (原本是 list[dict]) 升级为严格的 Pydantic 模型，以支持 MMU 元数据。注意处理向下兼容或迁移逻辑。任务 2.1：引入 CastorMessagefrom pydantic import BaseModel

class CastorMessage(BaseModel):
    role: str
    content: str
    pinned: bool = False  # 如果为 True，永远不能被 Lodge 换出
    token_count: int = 0  # 由 TokenCounter 计算的消耗量
    # ... (其他 M1/M2 已有的字段)
要求： AgentRunner 在组装 SystemPrompt 时，必须强制设置 pinned=True。3. Lodge 控制器核心逻辑 (The MMU Controller)在 castor/lodge/core.py 中实现 CastorLodge 类。任务 3.1：注册内核级 Syscall 工具在 Castor Dam 中注册一个隐藏的、内核专属的工具 sys_kernel_page_out。这个工具的真正实现逻辑就是调用底层 driver 的 ingest。@castor_tool(consumes="system_budget", internal=True)
async def sys_kernel_page_out(evicted_messages: List[Dict[str, Any]], ctx: SyscallContext) -> str:
    """内核级缺页中断处理例程，LLM 不可见"""
    driver = ctx.lodge.get_driver()
    await driver.ingest(evicted_messages, pid=ctx.checkpoint.pid)
    return "Kernel page-out successful"
任务 3.2：实现 Token 监控与淘汰算法实现 check_and_evict(self, proxy: SyscallProxy, checkpoint: AgentCheckpoint) 方法。淘汰算法 (V1)： 计算当前 context_history 的总 Token。如果超过水位线，找到 pinned == False 的消息，采用 FIFO 选出最老的 N 条作为 evicted_msgs。通过 Proxy 执行换出： ```pythonasync def check_and_evict(self, proxy: SyscallProxy, checkpoint: AgentCheckpoint):if self.total_tokens(checkpoint) > self.watermark:evicted_msgs = self._select_victims(checkpoint)  # 依赖 SyscallProxy 的统一路由！
  # 如果是重放状态，Proxy 会自动拦截并返回历史 response，绝不会执行底层 Driver！
  # 如果是真实执行，Proxy 会执行 sys_kernel_page_out 并自动将记录追加到 syscall_log 中。
  syscall_req = {
      "name": "sys_kernel_page_out", 
      "arguments": {"evicted_messages": [m.model_dump() for m in evicted_msgs]}
  }
  await proxy.syscall(request=syscall_req)

  # 内存截断 (确保在 Proxy 调用成功后执行)
  checkpoint.context_history = [m for m in checkpoint.context_history if m not in evicted_msgs]

**任务 3.3：注入触发时机 (The Paging Hook)**
在 `castor/runner.py` (`AgentRunner` 的主循环中)，在组装上下文发送给 `llm_inference` **之前**，显式调用 Lodge 的钩子：
```python
# Inside AgentRunner loop, before calling LLM:
await self.lodge.check_and_evict(self.proxy, self.checkpoint)
# 然后再发起真正的 llm_generate syscall...
4. 主动换入机制 (Active Page-In)任务 4.1：注册 search_memory 工具利用 Castor Dam 机制，为 Agent 提供查询冷存储的系统调用。@castor_tool(consumes="memory_read_budget")
async def search_memory(query: str, ctx: SyscallContext) -> str:
    """
    当你需要回忆过去的偏好、历史记录或被遗忘的上下文时，调用此工具。
    """
    # 同样享受 SyscallProxy 的 Replay 保护
    driver = ctx.lodge.get_driver()
    result = await driver.search(query, pid=ctx.checkpoint.pid)
    return result
5. Coding Agent 的执行步骤计划 (Step-by-Step Plan)请按照以下顺序提交 PR / 编写代码，确保每一步都有完整的 pytest 覆盖：Step 1: 修改 models.py 引入 CastorMessage；在 castor/lodge/ 下建立包结构，实现 TokenCounter 协议。Step 2: 实现 SemanticMemoryDriver 基类和 MockSQLiteDriver。Step 3: 实现内部系统工具 sys_kernel_page_out 以及 CastorLodge.check_and_evict() 的核心逻辑。确保它完美复用 proxy.syscall()，严禁手写 is_replaying 分支。Step 4: 在 AgentRunner 的执行循环中正确注入 check_and_evict Hook。实现 @castor_tool search_memory。Step 5 (Integration Test): 编写极限集成测试：向 Agent 注入超过阈值的对话，断言 Lodge 成功触发了 Page-Out，断言 Pinned 信息存活，断言 Agent 能通过 search_memory 准确找回被换出的信息。最重要的是，挂起该进程并执行完整的 Replay 测试，断言其状态游标 _replay_index 完美同步，没有任何底层的非预期网络调用。执行吧！保持内核边界的绝对纯洁性。

---

## Decisions Made

_Resolved questions with brief rationale. Prune once absorbed into code/docs._

- [CC] **LLM calls as syscalls** — Non-deterministic LLM calls must go through proxy to get logged in `syscall_log`. `LLMSyscall` wrapper registers a `@castor_tool` backed by user's async callable. Prevents `ReplayDivergenceError` on resume.
- [CC] **Transactional deduction** — `deduct()` before `execute()` with `refund()` on failure. Avoids `asyncio.shield()` complexity. `refund()` clamps at zero for safety.
- [CC] **`__annotations__` defensiveness** — Both `_generate_schema` and `_build_input_model` use `getattr(func, '__annotations__', None) or {}` to handle mocks and other callables without annotations.
- [CC] **HITL modification** — Never mutate `pending_hitl` args. Log `HITL_MODIFIED` and let LLM re-plan. Preserves replay determinism.
- [CC] **SuspendInterrupt naming** — It's an interrupt, not an error. `# noqa: N818`.

---

## Next Actions

_Concrete tasks ready for implementation. Ordered by priority._

1. **Lodge context pager (M3)** — Token counting, pinning, paging threshold, eviction, page-in. Design docs exist in `docs/`. Needs research on token counting approach first (see Open Questions).
2. **Sub-agent spawning** — Data models ready (`child_checkpoint` in `SyscallRecord`). Needs `spawn_agent` syscall handler in `SyscallProxy`, capability delegation to child, child suspension propagation.
3. **Agent return value** — Store result of `await agent_fn(proxy)` somewhere in `AgentCheckpoint`.
4. **CLI/API for HITL** — Currently requires programmatic `HITLHandler` calls. Needs a user-facing interface.

---

## Architecture Snapshot

```
Agent Function
  └─► SyscallProxy  (only interface to kernel)
        ├─ Dam        — tool registry, Pydantic validation, execution
        ├─ Stream     — checkpoint/replay, HITL, persistence
        ├─ Lodge      — context paging (stub)
        ├─ Capability — budget tracking, delegation, refund
        └─ LLM        — replay-safe inference wrapper
```

**Milestones:** M1 (Dam+Cap) ✅ | M2 (Stream) ✅ | M3 (Lodge) ⏳ | M4 (Integration) ⚙️

**Stats:** 100 tests | 0 lint errors | 15 public API exports | Python 3.11+ / Pydantic V2 / SQLite

---

## Data Models (quick reference)

```
AgentCheckpoint { pid, status, agent_function_name, capabilities, syscall_log, pending_hitl, context_history, preemption_* }
SyscallRecord   { request, response, was_hitl, child_checkpoint }
Capability      { resource_type, max_budget, current_usage }
SyscallResponse { status, result_payload, feedback_message, human_feedback }
```

---

## Build

```bash
uv sync && uv run pytest tests/ -v && uv run ruff check src/ tests/
```
