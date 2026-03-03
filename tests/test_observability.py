"""Tests for the observability module (logging, tracing, metrics)."""

import logging

from castor.observability import get_logger, get_meter, get_tracer


class TestNoopFallback:
    def test_get_tracer_without_otel(self):
        """get_tracer returns a noop tracer when OTel is not installed."""
        tracer = get_tracer("castor.test")
        # Should not raise — noop context manager
        with tracer.start_as_current_span("test_span"):
            pass

    def test_get_meter_without_otel(self):
        """get_meter returns a noop meter when OTel is not installed."""
        meter = get_meter("castor.test")
        counter = meter.create_counter("test_counter")
        counter.add(1)  # Should not raise

    def test_get_logger(self):
        """get_logger returns a standard Python logger."""
        logger = get_logger("castor.test")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "castor.test"
