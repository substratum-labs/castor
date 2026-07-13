"""Paper A evaluation: recoverable process / effectively-once effect semantics.

Run the S-Pay matrix::

    python -m castor.evals.paper_a.matrix --out results/paper_a
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from castor.evals.paper_a.matrix import TrialResult, run_matrix, run_trial

__all__ = ["TrialResult", "run_matrix", "run_trial"]


def __getattr__(name: str):
    if name in __all__:
        from castor.evals.paper_a import matrix as _matrix

        return getattr(_matrix, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
