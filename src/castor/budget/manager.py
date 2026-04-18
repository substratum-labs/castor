"""Budget Manager: budget tracking and delegation."""

from __future__ import annotations

from castor.models.budget import Budget
from castor.observability import get_logger

_logger = get_logger("castor.capability")


class BudgetExhaustedError(Exception):
    """Raised when a syscall exceeds the remaining budget."""

    def __init__(self, resource_type: str, requested: float, remaining: float):
        self.resource_type = resource_type
        self.requested = requested
        self.remaining = remaining
        super().__init__(
            f"Budget exhausted: {resource_type!r} — "
            f"requested {requested}, remaining {remaining}"
        )


class InsufficientBudgetError(Exception):
    """Raised when delegation requests more than parent has available."""

    def __init__(self, resource_type: str, requested: float, available: float):
        self.resource_type = resource_type
        self.requested = requested
        self.available = available
        super().__init__(
            f"Cannot delegate {resource_type!r}: "
            f"requested {requested}, parent has {available} available"
        )


class BudgetManager:
    """Manages capability budgets: creation, deduction, delegation, reclamation."""

    def create_budgets(self, specs: dict[str, float]) -> dict[str, Budget]:
        """Create root capabilities from {resource_type: max_budget} specs."""
        return {
            resource_type: Budget(resource_type=resource_type, max_budget=budget)
            for resource_type, budget in specs.items()
        }

    def check(
        self, capabilities: dict[str, Budget], resource_type: str, cost: float
    ) -> bool:
        """Check if sufficient budget exists for the given cost.

        Returns True if the resource type is not tracked (no budget = no limit).
        """
        cap = capabilities.get(resource_type)
        if cap is None:
            return True  # Not tracked → allowed
        return (cap.max_budget - cap.current_usage) >= cost

    def deduct(
        self, capabilities: dict[str, Budget], resource_type: str, cost: float
    ) -> None:
        """Deduct cost from a capability budget.

        No-ops if the resource type is not tracked (no budget = no limit).
        Raises BudgetExhaustedError if tracked but insufficient.
        """
        cap = capabilities.get(resource_type)
        if cap is None:
            return  # Not tracked → no enforcement

        remaining = cap.max_budget - cap.current_usage
        if remaining < cost:
            raise BudgetExhaustedError(resource_type, cost, remaining)

        cap.current_usage += cost
        _logger.debug(
            "budget_deduct",
            extra={
                "resource": resource_type,
                "cost": cost,
                "remaining": cap.max_budget - cap.current_usage,
            },
        )

    def delegate(
        self,
        parent_budgets: dict[str, Budget],
        requested: dict[str, float],
    ) -> dict[str, Budget]:
        """Partition a budget subset from parent to child.

        Deducts from parent and creates new capabilities for the child.
        Raises InsufficientBudgetError if parent can't cover the request.
        """
        # Validate all requests before modifying anything
        for resource_type, amount in requested.items():
            cap = parent_budgets.get(resource_type)
            if cap is None:
                raise InsufficientBudgetError(resource_type, amount, 0.0)
            available = cap.max_budget - cap.current_usage
            if available < amount:
                raise InsufficientBudgetError(resource_type, amount, available)

        # All checks passed — deduct from parent, create child caps
        child_budgets: dict[str, Budget] = {}
        for resource_type, amount in requested.items():
            parent_budgets[resource_type].current_usage += amount
            child_budgets[resource_type] = Budget(
                resource_type=resource_type, max_budget=amount
            )

        return child_budgets

    def refund(
        self, capabilities: dict[str, Budget], resource_type: str, cost: float
    ) -> None:
        """Reverse a prior deduction (e.g. when tool execution is interrupted)."""
        cap = capabilities.get(resource_type)
        if cap is not None:
            cap.current_usage = max(0.0, cap.current_usage - cost)
            _logger.debug(
                "budget_refund",
                extra={"resource": resource_type, "cost": cost},
            )

    def reclaim(
        self,
        parent_budgets: dict[str, Budget],
        child_budgets: dict[str, Budget],
    ) -> None:
        """Return unused child budget to parent on child completion."""
        for resource_type, child_cap in child_budgets.items():
            unused = child_cap.max_budget - child_cap.current_usage
            if unused > 0 and resource_type in parent_budgets:
                parent_budgets[resource_type].current_usage -= unused
