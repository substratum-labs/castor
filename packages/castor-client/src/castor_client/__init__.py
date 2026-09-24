"""Castor OS AISA v0.1 IPC Client."""

from __future__ import annotations

from .client import (
    MAX_FRAME_BYTES,
    AisaClient,
    AisaClientError,
    AisaConnectionError,
    AisaCorrelationError,
    AisaFrameTooLargeError,
    AisaGatewayError,
    AisaProtocolError,
    AisaTimeoutError,
)
from .session import AgentSession, OperatorSession

__version__ = "0.6.0a1"

__all__ = [
    "MAX_FRAME_BYTES",
    "AisaClient",
    "AisaClientError",
    "AisaConnectionError",
    "AisaCorrelationError",
    "AisaFrameTooLargeError",
    "AisaGatewayError",
    "AisaProtocolError",
    "AisaTimeoutError",
    "AgentSession",
    "OperatorSession",
    "__version__",
]
