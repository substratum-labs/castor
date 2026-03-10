"""Observability: structured logging, optional OpenTelemetry tracing and metrics.

Install extras for full observability:
    pip install castor[observability]

Without the extras, all tracing/metrics calls are noops with zero overhead.
"""

from __future__ import annotations

import logging
from typing import Any


def get_logger(name: str) -> logging.Logger:
    """Get a structured logger for the given module."""
    return logging.getLogger(name)


# ── OpenTelemetry tracing (optional) ──

try:
    from opentelemetry import trace

    def get_tracer(name: str) -> trace.Tracer:
        return trace.get_tracer(name)

except ImportError:

    class _NoopSpan:
        def __getattr__(self, name: str):  # noqa: ANN204
            """Catch-all: any unimplemented Span method becomes a noop."""
            return lambda *args, **kwargs: None

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *args: object) -> None:
            pass

    class _NoopTracer:
        def start_as_current_span(self, name: str, **kwargs: Any) -> _NoopSpan:
            return _NoopSpan()

    def get_tracer(name: str) -> Any:  # type: ignore[misc]
        return _NoopTracer()


# ── OpenTelemetry metrics (optional) ──

try:
    from opentelemetry import metrics

    def get_meter(name: str) -> metrics.Meter:
        return metrics.get_meter(name)

except ImportError:

    class _NoopCounter:
        def add(
            self,
            amount: float = 1,
            attributes: dict | None = None,
        ) -> None:
            pass

    class _NoopHistogram:
        def record(
            self,
            amount: float,
            attributes: dict | None = None,
        ) -> None:
            pass

    class _NoopMeter:
        def create_counter(self, name: str, **kwargs: Any) -> _NoopCounter:
            return _NoopCounter()

        def create_histogram(self, name: str, **kwargs: Any) -> _NoopHistogram:
            return _NoopHistogram()

        def create_up_down_counter(self, name: str, **kwargs: Any) -> _NoopCounter:
            return _NoopCounter()

    def get_meter(name: str) -> Any:  # type: ignore[misc]
        return _NoopMeter()
