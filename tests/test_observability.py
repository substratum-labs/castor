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


class TestLoggingIntegration:
    async def test_syscall_emits_log(self, caplog):
        """Syscall execution emits structured log messages."""
        from castor.capability.manager import CapabilityManager
        from castor.gate.decorator import castor_tool
        from castor.gate.registry import ToolRegistry
        from castor.gate.validator import SyscallGate
        from castor.models.checkpoint import AgentCheckpoint
        from castor.stream.proxy import SyscallProxy

        registry = ToolRegistry()

        @castor_tool(consumes="test", cost_per_use=1.0, registry=registry)
        def search(query: str) -> list:
            return [f"result for {query}"]

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        caps = cap_mgr.create_capabilities({"test": 100.0})
        cp = AgentCheckpoint(
            pid="test-001",
            status="RUNNING",
            agent_function_name="test",
            capabilities=caps,
        )
        proxy = SyscallProxy(cp, gate, cap_mgr)

        with caplog.at_level(logging.DEBUG, logger="castor.stream"):
            await proxy.syscall("search", {"query": "hello"})

        assert any("syscall_complete" in r.message for r in caplog.records)
