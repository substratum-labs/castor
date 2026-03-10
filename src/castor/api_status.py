"""API stability markers for Castor public interfaces.

@stable — Will not break between minor versions.
@experimental — May change in future versions.
"""

from typing import TypeVar

T = TypeVar("T")


def stable(obj: T) -> T:
    """Mark as stable public API."""
    obj.__api_status__ = "stable"  # type: ignore[attr-defined]
    return obj


def experimental(obj: T) -> T:
    """Mark as experimental — may change."""
    obj.__api_status__ = "experimental"  # type: ignore[attr-defined]
    return obj
