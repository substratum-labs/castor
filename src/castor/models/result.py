"""SyscallResult: structured wrapper for HITL/destructive tool responses."""

from __future__ import annotations

from typing import Any


class SyscallResult:
    """Structured result for HITL/destructive tool calls.

    Returned by tools with ``requires_hitl=True`` or ``destructive=True``
    when ``structured_results=True`` is set on the kernel.

    Usage::

        result = await proxy.send_email(to="team@co.com", subject="Q4", body=report)
        if result.ok:
            print(f"Sent: {result.value}")
        elif result.rejected:
            print(f"Rejected: {result.feedback}")
        elif result.modified:
            print(f"Modified: {result.feedback}")
        elif result.exhausted:
            print(f"Budget exceeded: {result.resource}")
    """

    __slots__ = ("_value", "_status", "_feedback", "_resource")

    def __init__(
        self,
        value: Any = None,
        *,
        status: str = "ok",
        feedback: str | None = None,
        resource: str | None = None,
    ) -> None:
        self._value = value
        self._status = status
        self._feedback = feedback
        self._resource = resource

    @property
    def value(self) -> Any:
        """The tool's return value (``None`` if rejected/exhausted)."""
        return self._value

    @property
    def ok(self) -> bool:
        """True if the tool executed successfully."""
        return self._status == "ok"

    @property
    def rejected(self) -> bool:
        """True if a human rejected the tool call."""
        return self._status == "HITL_REJECTED"

    @property
    def modified(self) -> bool:
        """True if a human approved with modification feedback."""
        return self._status == "HITL_MODIFIED"

    @property
    def exhausted(self) -> bool:
        """True if the tool was blocked by budget exhaustion."""
        return self._status == "INSUFFICIENT_CAPABILITY"

    @property
    def feedback(self) -> str | None:
        """Human feedback (for rejected/modified) or error message."""
        return self._feedback

    @property
    def resource(self) -> str | None:
        """The resource type that was exhausted (budget errors only)."""
        return self._resource

    def __repr__(self) -> str:
        if self.ok:
            return f"SyscallResult(value={self._value!r})"
        return f"SyscallResult(status={self._status!r}, feedback={self._feedback!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SyscallResult):
            return (
                self._status == other._status
                and self._value == other._value
                and self._feedback == other._feedback
            )
        return NotImplemented
