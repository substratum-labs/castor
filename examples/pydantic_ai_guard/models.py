"""Typed output models for the finance trading agent.

Pydantic-AI validates agent output against these models, ensuring
the LLM returns structured, type-safe trading decisions — not raw strings.
"""

from typing import Literal

from pydantic import BaseModel, Field


class TradeDecision(BaseModel):
    """A validated trading decision produced by the agent."""

    action: Literal["BUY", "SELL", "HOLD"]
    ticker: str
    amount: float = Field(ge=0, description="Dollar amount to trade")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence 0-1")
    reasoning: str


class RiskAssessment(BaseModel):
    """Risk analysis for a given position."""

    ticker: str
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    max_position: float = Field(ge=0, description="Max recommended position in USD")
    factors: list[str] = Field(default_factory=list)


class PortfolioStatus(BaseModel):
    """Current portfolio snapshot."""

    holdings: dict[str, float] = Field(
        default_factory=dict, description="ticker -> USD value"
    )
    cash: float = Field(ge=0)
    total_value: float = Field(ge=0)
