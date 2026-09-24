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
    "__version__",
]
