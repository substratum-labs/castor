"""Castor: A secure microkernel for LLM Agents."""

__version__ = "0.1.0"

# Core public API
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
