"""Basic return and risk metrics for a price series."""

import numpy as np
import pandas as pd


def daily_returns(prices: pd.Series) -> pd.Series:
    """Simple daily percentage returns from a price series."""
    return prices.pct_change().dropna()


def annualized_return(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Compound annual growth rate implied by a periodic return series."""
    growth = (1 + returns).prod()
    n_periods = len(returns)
    if n_periods == 0:
        return 0.0
    return growth ** (periods_per_year / n_periods) - 1


def annualized_volatility(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Annualized standard deviation of returns."""
    return returns.std(ddof=1) * np.sqrt(periods_per_year)


def sharpe_ratio(
    returns: pd.Series, risk_free_rate: float = 0.0, periods_per_year: int = 252
) -> float:
    """Annualized Sharpe ratio given a periodic risk-free rate."""
    excess = returns - risk_free_rate / periods_per_year
    volatility = excess.std(ddof=1)
    if volatility == 0:
        return 0.0
    return (excess.mean() / volatility) * np.sqrt(periods_per_year)


def max_drawdown(prices: pd.Series) -> float:
    """Largest peak-to-trough decline as a negative fraction."""
    cumulative_max = prices.cummax()
    drawdown = prices / cumulative_max - 1
    return drawdown.min()
