"""Tests for the @castor_tool decorator."""

import pytest

from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry


@pytest.fixture
def registry():
    return ToolRegistry()


class TestCastorToolDecorator:
    def test_registers_sync_function(self, registry):
        @castor_tool(consumes="network_read", cost_per_use=1.0, registry=registry)
        def web_search(query: str, max_results: int = 10) -> list:
            return []

        assert registry.has_tool("web_search")
        meta = registry.get("web_search")
        assert meta.consumes == "network_read"
        assert meta.cost_per_use == 1.0
        assert meta.is_async is False
        assert meta.func is web_search

    def test_registers_async_function(self, registry):
        @castor_tool(consumes="api_call", cost_per_use=2.0, registry=registry)
        async def fetch_url(url: str) -> str:
            return ""

        meta = registry.get("fetch_url")
        assert meta.is_async is True
        assert meta.cost_per_use == 2.0

    def test_destructive_and_hitl_flags(self, registry):
        @castor_tool(
            consumes="disk_delete",
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        def delete_files(paths: list[str]) -> int:
            return 0

        meta = registry.get("delete_files")
        assert meta.destructive is True
        assert meta.requires_hitl is True

    def test_schema_has_required_fields(self, registry):
        @castor_tool(consumes="test", registry=registry)
        def greet(name: str, greeting: str = "Hello") -> str:
            return f"{greeting}, {name}!"

        meta = registry.get("greet")
        schema = meta.input_schema
        assert "properties" in schema
        assert "name" in schema["properties"]
        assert "greeting" in schema["properties"]
        # 'name' is required, 'greeting' has default
        assert "name" in schema.get("required", [])

    def test_schema_field_types(self, registry):
        @castor_tool(consumes="test", registry=registry)
        def compute(x: int, y: float, label: str = "result") -> dict:
            return {}

        schema = registry.get("compute").input_schema
        props = schema["properties"]
        assert props["x"]["type"] == "integer"
        assert props["y"]["type"] == "number"
        assert props["label"]["type"] == "string"

    def test_function_still_callable(self, registry):
        @castor_tool(consumes="test", registry=registry)
        def add(a: int, b: int) -> int:
            return a + b

        assert add(3, 4) == 7

    def test_async_function_still_callable(self, registry):
        @castor_tool(consumes="test", registry=registry)
        async def async_add(a: int, b: int) -> int:
            return a + b

        import asyncio

        result = asyncio.get_event_loop().run_until_complete(async_add(3, 4))
        assert result == 7

    def test_metadata_attached_to_function(self, registry):
        @castor_tool(consumes="test", registry=registry)
        def my_tool(x: int) -> int:
            return x

        assert hasattr(my_tool, "_castor_metadata")
        assert my_tool._castor_metadata.tool_name == "my_tool"

    def test_no_params_function(self, registry):
        @castor_tool(consumes="test", registry=registry)
        def ping() -> str:
            return "pong"

        schema = registry.get("ping").input_schema
        # No required fields, no properties (or empty properties)
        assert schema.get("required", []) == []
