"""Tests for SyscallGate validation and execution engine."""

import pytest
from pydantic import ValidationError

from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def gate(registry):
    return SyscallGate(registry)


def register_tool(registry, func, **kwargs):
    """Helper to register a tool via decorator."""
    defaults = {"consumes": "test", "registry": registry}
    defaults.update(kwargs)
    return castor_tool(**defaults)(func)


class TestValidation:
    def test_valid_input(self, registry, gate):
        def search(query: str, limit: int = 10) -> list:
            return []

        register_tool(registry, search)
        result = gate.validate("search", {"query": "hello"})
        assert result == {"query": "hello", "limit": 10}

    def test_valid_input_all_fields(self, registry, gate):
        def search(query: str, limit: int = 10) -> list:
            return []

        register_tool(registry, search)
        result = gate.validate("search", {"query": "hello", "limit": 5})
        assert result == {"query": "hello", "limit": 5}

    def test_missing_required_field(self, registry, gate):
        def search(query: str) -> list:
            return []

        register_tool(registry, search)
        with pytest.raises(ValidationError):
            gate.validate("search", {})

    def test_wrong_type(self, registry, gate):
        def compute(x: int) -> int:
            return x

        register_tool(registry, compute)
        with pytest.raises(ValidationError):
            gate.validate("compute", {"x": "not_a_number"})

    def test_extra_fields_ignored(self, registry, gate):
        def ping() -> str:
            return "pong"

        register_tool(registry, ping)
        result = gate.validate("ping", {})
        assert result == {}


class TestExecution:
    async def test_sync_tool_execution(self, registry, gate):
        def add(a: int, b: int) -> int:
            return a + b

        register_tool(registry, add)
        result = await gate.execute("add", {"a": 3, "b": 4})
        assert result == 7

    async def test_async_tool_execution(self, registry, gate):
        async def fetch(url: str) -> str:
            return f"content of {url}"

        register_tool(registry, fetch)
        result = await gate.execute("fetch", {"url": "https://example.com"})
        assert result == "content of https://example.com"

    async def test_tool_with_defaults(self, registry, gate):
        def greet(name: str, greeting: str = "Hello") -> str:
            return f"{greeting}, {name}!"

        register_tool(registry, greet)
        validated = gate.validate("greet", {"name": "World"})
        result = await gate.execute("greet", validated)
        assert result == "Hello, World!"


class TestValidationErrorFormatting:
    def test_format_missing_field(self, registry, gate):
        def search(query: str) -> list:
            return []

        register_tool(registry, search)
        try:
            gate.validate("search", {})
        except ValidationError as e:
            response = gate.format_validation_error("search", e)
            assert response.status == "VALIDATION_ERROR"
            assert "query" in response.feedback_message
            assert "fix the arguments" in response.feedback_message

    def test_format_wrong_type(self, registry, gate):
        def compute(x: int) -> int:
            return x

        register_tool(registry, compute)
        try:
            gate.validate("compute", {"x": "bad"})
        except ValidationError as e:
            response = gate.format_validation_error("compute", e)
            assert response.status == "VALIDATION_ERROR"
            assert "x" in response.feedback_message


class TestGetToolMeta:
    def test_get_metadata(self, registry, gate):
        def my_tool(x: int) -> int:
            return x

        register_tool(registry, my_tool, cost_per_use=5.0, destructive=True)
        meta = gate.get_tool_meta("my_tool")
        assert meta.cost_per_use == 5.0
        assert meta.destructive is True
