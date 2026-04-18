"""Tests for Budget Manager."""

import pytest

from castor.budget.manager import (
    BudgetExhaustedError,
    BudgetManager,
    InsufficientBudgetError,
)


@pytest.fixture
def mgr():
    return BudgetManager()


class TestCreateCapabilities:
    def test_create_from_specs(self, mgr):
        caps = mgr.create_budgets({"network_read": 100.0, "disk_delete": 10.0})
        assert len(caps) == 2
        assert caps["network_read"].max_budget == 100.0
        assert caps["network_read"].current_usage == 0.0
        assert caps["disk_delete"].max_budget == 10.0

    def test_create_empty(self, mgr):
        caps = mgr.create_budgets({})
        assert caps == {}


class TestCheck:
    def test_sufficient_budget(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        assert mgr.check(caps, "api", 5.0) is True

    def test_exact_budget(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        assert mgr.check(caps, "api", 10.0) is True

    def test_insufficient_budget(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        assert mgr.check(caps, "api", 11.0) is False

    def test_missing_resource_type_returns_true(self, mgr):
        """Missing resource = not tracked = allowed."""
        caps = mgr.create_budgets({"api": 10.0})
        assert mgr.check(caps, "nonexistent", 1.0) is True

    def test_partially_used(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        caps["api"].current_usage = 7.0
        assert mgr.check(caps, "api", 3.0) is True
        assert mgr.check(caps, "api", 4.0) is False


class TestDeduct:
    def test_deduct_within_budget(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        mgr.deduct(caps, "api", 3.0)
        assert caps["api"].current_usage == 3.0

    def test_deduct_multiple_times(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        mgr.deduct(caps, "api", 3.0)
        mgr.deduct(caps, "api", 4.0)
        assert caps["api"].current_usage == 7.0

    def test_deduct_exceeds_budget(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        mgr.deduct(caps, "api", 8.0)
        with pytest.raises(BudgetExhaustedError) as exc_info:
            mgr.deduct(caps, "api", 5.0)
        assert exc_info.value.resource_type == "api"
        assert exc_info.value.requested == 5.0
        assert exc_info.value.remaining == 2.0

    def test_deduct_missing_resource_is_noop(self, mgr):
        """Missing resource = not tracked = no enforcement."""
        caps = mgr.create_budgets({"api": 10.0})
        mgr.deduct(caps, "nonexistent", 1.0)  # Should not raise


class TestDelegate:
    def test_delegate_subset(self, mgr):
        parent = mgr.create_budgets({"api": 100.0, "disk": 50.0})
        child = mgr.delegate(parent, {"api": 20.0, "disk": 10.0})

        # Parent budget reduced
        assert parent["api"].current_usage == 20.0
        assert parent["disk"].current_usage == 10.0

        # Child gets fresh budget
        assert child["api"].max_budget == 20.0
        assert child["api"].current_usage == 0.0
        assert child["disk"].max_budget == 10.0

    def test_delegate_exceeds_parent(self, mgr):
        parent = mgr.create_budgets({"api": 10.0})
        with pytest.raises(InsufficientBudgetError) as exc_info:
            mgr.delegate(parent, {"api": 15.0})
        assert exc_info.value.resource_type == "api"
        # Parent should not be modified on failure
        assert parent["api"].current_usage == 0.0

    def test_delegate_missing_resource(self, mgr):
        parent = mgr.create_budgets({"api": 10.0})
        with pytest.raises(InsufficientBudgetError):
            mgr.delegate(parent, {"nonexistent": 5.0})

    def test_delegate_after_partial_use(self, mgr):
        parent = mgr.create_budgets({"api": 100.0})
        mgr.deduct(parent, "api", 60.0)
        child = mgr.delegate(parent, {"api": 30.0})
        assert parent["api"].current_usage == 90.0
        assert child["api"].max_budget == 30.0


class TestRefund:
    def test_refund_reverses_deduction(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        mgr.deduct(caps, "api", 3.0)
        mgr.refund(caps, "api", 3.0)
        assert caps["api"].current_usage == 0.0

    def test_refund_partial(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        mgr.deduct(caps, "api", 5.0)
        mgr.refund(caps, "api", 2.0)
        assert caps["api"].current_usage == 3.0

    def test_refund_clamps_at_zero(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        mgr.refund(caps, "api", 5.0)
        assert caps["api"].current_usage == 0.0

    def test_refund_missing_resource_is_noop(self, mgr):
        caps = mgr.create_budgets({"api": 10.0})
        mgr.refund(caps, "nonexistent", 5.0)  # should not raise


class TestReclaim:
    def test_reclaim_unused_budget(self, mgr):
        parent = mgr.create_budgets({"api": 100.0})
        child = mgr.delegate(parent, {"api": 20.0})
        # Child uses 5 of its 20
        child["api"].current_usage = 5.0
        mgr.reclaim(parent, child)
        # Parent gets back 15 unused
        assert parent["api"].current_usage == 5.0  # was 20, minus 15 returned

    def test_reclaim_fully_used(self, mgr):
        parent = mgr.create_budgets({"api": 100.0})
        child = mgr.delegate(parent, {"api": 20.0})
        child["api"].current_usage = 20.0
        mgr.reclaim(parent, child)
        # Nothing to reclaim
        assert parent["api"].current_usage == 20.0

    def test_reclaim_nothing_used(self, mgr):
        parent = mgr.create_budgets({"api": 100.0})
        child = mgr.delegate(parent, {"api": 20.0})
        # Child uses nothing
        mgr.reclaim(parent, child)
        assert parent["api"].current_usage == 0.0

    def test_full_lifecycle(self, mgr):
        """Parent delegates, child works, reclaim — parent budget consistent."""
        parent = mgr.create_budgets({"net": 100.0, "disk": 50.0})

        # Delegate
        child = mgr.delegate(parent, {"net": 30.0, "disk": 10.0})
        assert parent["net"].current_usage == 30.0
        assert parent["disk"].current_usage == 10.0

        # Child uses some
        mgr.deduct(child, "net", 12.0)
        mgr.deduct(child, "disk", 7.0)

        # Reclaim
        mgr.reclaim(parent, child)
        assert parent["net"].current_usage == 12.0  # 30 delegated - 18 returned
        assert parent["disk"].current_usage == 7.0  # 10 delegated - 3 returned
