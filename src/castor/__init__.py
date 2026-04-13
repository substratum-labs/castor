"""Castor: A secure microkernel for LLM Agents."""

__version__ = "0.5.1"

# Core public API
# -- API stability markers --
from castor.api_status import experimental, stable
from castor.capability.manager import CapabilityManager
from castor.core import Castor, CastorTask
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolMetadata
from castor.gate.validator import SyscallGate
from castor.hitl_policies import auto_approve, auto_reject, interactive
from castor.kernel.journal import InMemoryJournal
from castor.kernel.summary import ExecutionSummary
from castor.llm.wrapper import LLMSyscall, StreamingLLMSyscall
from castor.mmu.core import MMU
from castor.models.capability import Capability, SyscallRequest, SyscallResponse
from castor.models.checkpoint import (
    AgentCheckpoint,
    CastorMessage,
    SuspendInterrupt,
    SyscallRecord,
    compute_invocation_id,
)
from castor.models.result import SyscallResult
from castor.protocols import (
    AgentRegistryProtocol,
    BudgetProtocol,
    CheckpointStoreProtocol,
    GateProtocol,
    JournalProtocol,
    MMUProtocol,
    RunnerProtocol,
)
from castor.scheduler.agent_registry import (
    AgentNotFoundError,
    AgentRegistry,
    castor_agent,
    default_agent_registry,
)
from castor.scheduler.hitl import HITLHandler
from castor.scheduler.persistence import (
    CheckpointStore,
    MemoryCheckpointStore,
)
from castor.scheduler.proxy import SyscallProxy
from castor.scheduler.runner import AgentRunner, default_runner_factory

stable(Castor)
stable(SyscallProxy)
stable(AgentCheckpoint)
stable(SyscallRecord)
stable(Capability)
stable(SyscallRequest)
stable(SyscallResponse)
stable(SyscallGate)
stable(CapabilityManager)
stable(HITLHandler)
stable(AgentRunner)
stable(CheckpointStore)
stable(CheckpointStoreProtocol)
stable(GateProtocol)
stable(BudgetProtocol)
stable(JournalProtocol)
stable(InMemoryJournal)
stable(MemoryCheckpointStore)
stable(ToolMetadata)
stable(castor_tool)
stable(SuspendInterrupt)
stable(CastorMessage)
stable(SyscallResult)
stable(auto_approve)
stable(auto_reject)
stable(interactive)

experimental(MMUProtocol)
experimental(AgentRegistryProtocol)
experimental(RunnerProtocol)
experimental(MMU)
experimental(CastorTask)
experimental(LLMSyscall)
experimental(StreamingLLMSyscall)
experimental(AgentRegistry)
experimental(castor_agent)
experimental(AgentNotFoundError)
experimental(default_agent_registry)

__all__ = [
    "AgentCheckpoint",
    "AgentNotFoundError",
    "AgentRegistry",
    "AgentRegistryProtocol",
    "AgentRunner",
    "BudgetProtocol",
    "Capability",
    "CapabilityManager",
    "Castor",
    "ExecutionSummary",
    "SyscallGate",
    "MMU",
    "MMUProtocol",
    "CastorMessage",
    "CastorTask",
    "CheckpointStore",
    "CheckpointStoreProtocol",
    "GateProtocol",
    "HITLHandler",
    "InMemoryJournal",
    "JournalProtocol",
    "LLMSyscall",
    "StreamingLLMSyscall",
    "SuspendInterrupt",
    "SyscallProxy",
    "SyscallRecord",
    "SyscallRequest",
    "SyscallResponse",
    "SyscallResult",
    "compute_invocation_id",
    "MemoryCheckpointStore",
    "ToolMetadata",
    "auto_approve",
    "auto_reject",
    "castor_agent",
    "castor_tool",
    "default_agent_registry",
    "default_runner_factory",
    "interactive",
    "RunnerProtocol",
]
