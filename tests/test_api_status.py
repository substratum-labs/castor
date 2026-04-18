"""Tests for API stability markers."""


class TestAPIStatusDecorators:
    def test_stable_marker_on_class(self):
        from castor.api_status import stable

        @stable
        class MyClass:
            pass

        assert MyClass.__api_status__ == "stable"

    def test_experimental_marker_on_function(self):
        from castor.api_status import experimental

        @experimental
        def my_func():
            pass

        assert my_func.__api_status__ == "experimental"

    def test_stable_preserves_class(self):
        from castor.api_status import stable

        @stable
        class Foo:
            x = 42

        assert Foo.x == 42


class TestAPIStatusExports:
    def test_stable_exports(self):
        """Core stable APIs are marked."""
        from castor import (
            AgentCheckpoint,
            AgentRunner,
            BudgetManager,
            CheckpointStore,
            HITLHandler,
            SyscallGate,
            SyscallProxy,
            castor_tool,
        )

        assert getattr(SyscallProxy, "__api_status__", None) == "stable"
        assert getattr(AgentCheckpoint, "__api_status__", None) == "stable"
        assert getattr(BudgetManager, "__api_status__", None) == "stable"
        assert getattr(SyscallGate, "__api_status__", None) == "stable"
        assert getattr(HITLHandler, "__api_status__", None) == "stable"
        assert getattr(AgentRunner, "__api_status__", None) == "stable"
        assert getattr(CheckpointStore, "__api_status__", None) == "stable"
        assert getattr(castor_tool, "__api_status__", None) == "stable"

    def test_experimental_exports(self):
        """Experimental APIs are marked."""
        from castor import MMU, AgentRegistry, LLMSyscall, castor_agent

        assert getattr(MMU, "__api_status__", None) == "experimental"
        assert getattr(LLMSyscall, "__api_status__", None) == "experimental"
        assert getattr(AgentRegistry, "__api_status__", None) == "experimental"
        assert getattr(castor_agent, "__api_status__", None) == "experimental"
