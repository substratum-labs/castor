"""Castor LLM: wrappers that route LLM inference through the syscall proxy."""

from castor.llm.wrapper import LLMSyscall, StreamingLLMSyscall

__all__ = ["LLMSyscall", "StreamingLLMSyscall"]
