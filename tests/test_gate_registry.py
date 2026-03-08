"""Tests for Castor Dam tool registry."""

import pytest

from castor.gate.registry import ToolMetadata, ToolNotFoundError, ToolRegistry


def make_metadata(name: str = "test_tool", **kwargs) -> ToolMetadata:
    defaults = {
        "tool_name": name,
        "consumes": "network_read",
        "cost_per_use": 1.0,
    }
    defaults.update(kwargs)
    return ToolMetadata(**defaults)


class TestToolMetadata:
    def test_defaults(self):
        meta = make_metadata()
        assert meta.tool_name == "test_tool"
        assert meta.consumes == "network_read"
        assert meta.cost_per_use == 1.0
        assert meta.requires_hitl is False
        assert meta.destructive is False
        assert meta.input_schema == {}
        assert meta.func is None
        assert meta.is_async is False

    def test_custom_values(self):
        def my_fn():
            pass

        meta = make_metadata(
            name="delete_files",
            consumes="disk_delete",
            cost_per_use=5.0,
            requires_hitl=True,
            destructive=True,
            func=my_fn,
            is_async=False,
        )
        assert meta.tool_name == "delete_files"
        assert meta.destructive is True
        assert meta.requires_hitl is True
        assert meta.func is my_fn


class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        meta = make_metadata("search")
        registry.register(meta)
        assert registry.get("search") is meta

    def test_get_missing_raises(self):
        registry = ToolRegistry()
        with pytest.raises(ToolNotFoundError, match="no_such_tool"):
            registry.get("no_such_tool")

    def test_has_tool(self):
        registry = ToolRegistry()
        assert registry.has_tool("search") is False
        registry.register(make_metadata("search"))
        assert registry.has_tool("search") is True

    def test_list_tools_sorted(self):
        registry = ToolRegistry()
        registry.register(make_metadata("zebra"))
        registry.register(make_metadata("alpha"))
        registry.register(make_metadata("middle"))
        assert registry.list_tools() == ["alpha", "middle", "zebra"]

    def test_list_tools_empty(self):
        registry = ToolRegistry()
        assert registry.list_tools() == []

    def test_overwrite_registration(self):
        registry = ToolRegistry()
        meta1 = make_metadata("tool", cost_per_use=1.0)
        meta2 = make_metadata("tool", cost_per_use=5.0)
        registry.register(meta1)
        registry.register(meta2)
        assert registry.get("tool").cost_per_use == 5.0
