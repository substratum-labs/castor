"""Stub trading tools for the finance demo.

These return deterministic fake data so the demo runs without
a real market data API. Each tool is a plain async function
compatible with pydantic-ai's FunctionToolset.
"""


async def fetch_price(ticker: str) -> str:
    """Fetch the latest price and volume for a stock ticker."""
    prices = {
        "AAPL": ("$187.44", "52.3M"),
        "GOOGL": ("$141.80", "28.1M"),
        "TSLA": ("$248.50", "114.7M"),
        "MSFT": ("$378.91", "19.8M"),
    }
    price, volume = prices.get(ticker, ("$100.00", "1.0M"))
    return f"{ticker}: price={price}, volume={volume}, market=OPEN"


async def analyze_risk(ticker: str, position_usd: float) -> str:
    """Analyze risk for a proposed position size."""
    if position_usd > 5000:
        level, max_pos = "HIGH", 5000.0
    elif position_usd > 1000:
        level, max_pos = "MEDIUM", 3000.0
    else:
        level, max_pos = "LOW", position_usd
    return (
        f"Risk assessment for {ticker}: level={level}, "
        f"max_recommended_position=${max_pos:.0f}, "
        f"factors=[volatility, sector_exposure, concentration]"
    )


async def execute_trade(ticker: str, action: str, amount_usd: float) -> str:
    """Execute a trade. DESTRUCTIVE — requires human approval in production."""
    return (
        f"EXECUTED: {action} ${amount_usd:.2f} of {ticker} "
        f"at market price. Order ID: TXN-2024-00042"
    )


async def check_portfolio() -> str:
    """Check current portfolio holdings and cash balance."""
    return (
        "Portfolio: AAPL=$2,500 (10 shares), GOOGL=$1,418 (10 shares), "
        "cash=$6,082, total_value=$10,000"
    )
