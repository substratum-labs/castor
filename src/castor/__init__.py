"""Castor: A secure microkernel for LLM Agents."""

__version__ = "0.1.0"

# Core public API
# ── API stability markers ──
from castor.api_status import experimental, stable
from castor.capability.manager import CapabilityManager
from castor.dam.decorator import castor_tool
from castor.dam.validator import CastorDam
from castor.llm.wrapper import LLMSyscall
from castor.lodge.core import CastorLodge
from castor.models.capability import Capability, SyscallRequest, SyscallResponse
from castor.models.checkpoint import (
    AgentCheckpoint,
    CastorMessage,
    SuspendInterrupt,
    SyscallRecord,
)
from castor.stream.agent_registry import AgentNotFoundError, AgentRegistry, castor_agent
from castor.stream.hitl import HITLHandler
from castor.stream.persistence import CheckpointStore
from castor.stream.proxy import SyscallProxy
from castor.stream.runner import AgentRunner

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
stable(castor_tool)
stable(SuspendInterrupt)
stable(CastorMessage)

experimental(CastorLodge)
experimental(LLMSyscall)
experimental(AgentRegistry)
experimental(castor_agent)
experimental(AgentNotFoundError)

__all__ = [
    "AgentCheckpoint",
    "AgentNotFoundError",
    "AgentRegistry",
    "AgentRunner",
    "Capability",
    "CapabilityManager",
    "CastorDam",
    "CastorLodge",
    "CastorMessage",
    "CheckpointStore",
    "HITLHandler",
    "LLMSyscall",
    "SuspendInterrupt",
    "SyscallProxy",
    "SyscallRecord",
    "SyscallRequest",
    "SyscallResponse",
    "castor_agent",
    "castor_tool",
]
