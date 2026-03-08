"""ContextVar bridge: implicit SyscallProxy access for castor.lib functions."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from castor.scheduler.proxy import SyscallProxy

_proxy_var: ContextVar[SyscallProxy] = ContextVar("castor_proxy")


def get_proxy() -> SyscallProxy:
    """Return the current SyscallProxy.

    Raises RuntimeError if called outside ``Castor.run()``.
    """
    try:
        return _proxy_var.get()
    except LookupError:
        raise RuntimeError(
            "castor.lib functions must be called inside Castor.run()"
        ) from None


def set_proxy(proxy: SyscallProxy) -> None:
    """Set the current SyscallProxy (called by AgentRunner)."""
    _proxy_var.set(proxy)
