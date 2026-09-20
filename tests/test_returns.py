import pandas as pd
import pytest

from investing.returns import (
    annualized_return,
    annualized_volatility,
    daily_returns,
    max_drawdown,
    sharpe_ratio,
)


@pytest.fixture
def prices() -> pd.Series:
    return pd.Series([100, 102, 101, 105, 103, 108])


def test_daily_returns(prices):
    result = daily_returns(prices)
    assert len(result) == len(prices) - 1
    assert result.iloc[0] == pytest.approx(0.02)


def test_annualized_return(prices):
    returns = daily_returns(prices)
    result = annualized_return(returns, periods_per_year=252)
    assert result > 0


def test_annualized_volatility(prices):
    returns = daily_returns(prices)
    assert annualized_volatility(returns) > 0


def test_sharpe_ratio(prices):
    returns = daily_returns(prices)
    assert isinstance(sharpe_ratio(returns), float)


def test_max_drawdown(prices):
    result = max_drawdown(prices)
    assert result <= 0
    assert result == pytest.approx(103 / 105 - 1)
