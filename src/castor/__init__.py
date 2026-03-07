"""Castor: A secure microkernel for LLM Agents."""

__version__ = "0.1.0"

# Core public API
# ── API stability markers ──
from castor.api_status import experimental, stable
from castor.capability.manager import CapabilityManager
from castor.core import Castor, CastorTask
from castor.dam.decorator import castor_tool
from castor.dam.registry import ToolMetadata
from castor.dam.validator import CastorDam
from castor.hitl_policies import auto_approve, auto_reject, interactive
from castor.llm.wrapper import LLMSyscall, StreamingLLMSyscall
from castor.lodge.core import CastorLodge
from castor.models.capability import Capability, SyscallRequest, SyscallResponse
from castor.models.checkpoint import (
    AgentCheckpoint,
    CastorMessage,
    SuspendInterrupt,
    SyscallRecord,
)
from castor.models.result import SyscallResult
from castor.stream.agent_registry import (
    AgentNotFoundError,
    AgentRegistry,
    castor_agent,
    default_agent_registry,
)
from castor.stream.hitl import HITLHandler
from castor.stream.persistence import (
    CheckpointStore,
    CheckpointStoreProtocol,
    MemoryCheckpointStore,
)
from castor.stream.proxy import SyscallProxy
from castor.stream.runner import AgentRunner

stable(Castor)
stable(SyscallProxy)
stable(AgentCheckpoint)
stable(SyscallRecord)
stable(Capability)
stable(SyscallRequest)
stable(SyscallResponse)
stable(CastorDam)
stable(CapabilityManager)
stable(HITLHandler)
stable(AgentRunner)
stable(CheckpointStore)
stable(CheckpointStoreProtocol)
stable(MemoryCheckpointStore)
stable(ToolMetadata)
stable(castor_tool)
stable(SuspendInterrupt)
stable(CastorMessage)
stable(SyscallResult)
stable(auto_approve)
stable(auto_reject)
stable(interactive)

experimental(CastorLodge)
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
    "AgentRunner",
    "Capability",
    "CapabilityManager",
    "Castor",
    "CastorDam",
    "CastorLodge",
    "CastorMessage",
    "CastorTask",
    "CheckpointStore",
    "CheckpointStoreProtocol",
    "HITLHandler",
    "LLMSyscall",
    "StreamingLLMSyscall",
    "SuspendInterrupt",
    "SyscallProxy",
    "SyscallRecord",
    "SyscallRequest",
    "SyscallResponse",
    "SyscallResult",
    "MemoryCheckpointStore",
    "ToolMetadata",
    "auto_approve",
    "auto_reject",
    "castor_agent",
    "castor_tool",
    "default_agent_registry",
    "interactive",
]
