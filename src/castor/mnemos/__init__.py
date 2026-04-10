"""Mnemos inference engine integration for Castor.

This module provides a Castor LLM tool wrapper that routes inference
through Mnemos (the agent-aware inference engine) instead of a direct
LLM provider.

Architecture::

    Agent → proxy.syscall("mnemos_inference", ...)
                  ↓
            SyscallProxy (validation, kernel, journal)
                  ↓
            MnemosLLMSyscall.func
                  ↓
            MnemosClient.execute(handle, tokens, hint)
                  ↓
            gRPC → mnemosd → MnemosEngine

Usage::

    from mnemos.client import MnemosClient
    from castor.mnemos import MnemosLLMSyscall

    client = MnemosClient(host="localhost", port=50052)
    await client.connect()

    syscall = MnemosLLMSyscall(
        registry=tool_registry,
        client=client,
        model_id="llama-3-70b",
    )

    async def my_agent(proxy):
        result = await syscall.infer(
            proxy, tokens=[1, 2, 3], max_new_tokens=64
        )
"""

from castor.mnemos.lifecycle import ContextLifecycleManager
from castor.mnemos.wrapper import MnemosLLMSyscall

__all__ = [
    "ContextLifecycleManager",
    "MnemosLLMSyscall",
]
