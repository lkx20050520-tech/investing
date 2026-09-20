"""
信号引擎
========

把所有标的的特征拼成 (date × symbol) 的面板矩阵，横截面排名一次向量化算完。
比逐日逐票循环快约 40 倍，而且更不容易写出前视偏差 (lookahead bias)。

⚠️ 前视偏差是量化回测最常见也最致命的 bug
------------------------------------------
它的表现是回测曲线漂亮得不真实，实盘一上就亏。
本模块的防护：
  1. momentum 用 shift(21) / shift(252)，t 日只能看到 t-21 日及更早的数据
  2. 所有滚动窗口都是 rolling()，pandas 默认右对齐，不会用到未来数据
  3. tests/test_logic.py 里有断言逐一验证这一点

任何时候你改了这个文件，必须重跑 tests/ 确认断言全过。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.config import Config


# =============================================================================
# 技术指标
# =============================================================================

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    """Average True Range —— 衡量单位时间的真实波动幅度，用于定止损和仓位。"""
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


# =============================================================================
# 引擎
# =============================================================================

class SignalEngine:
    """
    输入: {symbol: OHLCV DataFrame}
    输出: 每日的 (a) 市场状态 (b) 目标敞口 (c) 合格买入候选 (d) 持仓卖出判定
    """

    FEATURES = ("close", "high", "low", "momentum", "pct52", "ma_stack",
                "atr", "dollar_vol", "extension", "ema50", "ema20", "vol_ratio")

    def __init__(self, data: dict[str, pd.DataFrame], cfg: Config):
        self.cfg = cfg
        self.data = data
        if cfg.regime_symbol not in data:
            raise ValueError(
                f"基准 {cfg.regime_symbol} 不在数据中 —— 没有它就无法做择时过滤，"
                f"而择时过滤是这套系统最重要的风控。"
            )
        self._build()

    # -------------------------------------------------------------------------
    def _build(self) -> None:
        cfg = self.cfg
        cols: dict[str, dict[str, pd.Series]] = {k: {} for k in self.FEATURES}

        for sym, df in self.data.items():
            c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
            e10, e20, e50 = ema(c, 10), ema(c, 20), ema(c, 50)
            a = atr(h, l, c, cfg.atr_period)

            cols["close"][sym] = c
            cols["high"][sym] = h
            cols["low"][sym] = l
            # --- 12-1 动量：跳过最近一个月，避开短期反转 ---
            cols["momentum"][sym] = c.shift(cfg.mom_skip) / c.shift(cfg.mom_lookback) - 1.0
            cols["pct52"][sym] = c / c.rolling(252, min_periods=120).max()
            cols["ma_stack"][sym] = (c > e10) & (e10 > e20) & (e20 > e50)
            cols["atr"][sym] = a
            cols["dollar_vol"][sym] = (c * v).rolling(20).mean()
            cols["extension"][sym] = (c - e20) / a.replace(0, np.nan)
            cols["ema50"][sym] = e50
            cols["ema20"][sym] = e20
            cols["vol_ratio"][sym] = v / v.rolling(20).mean()

        self.P = {k: pd.DataFrame(v).sort_index() for k, v in cols.items()}
        self.dates: pd.DatetimeIndex = self.P["close"].index

        # --- 横截面动量分 (0-100 百分位)。这就是各种付费工具所谓 "评分" 的真实面目 ---
        mom = self.P["momentum"].drop(columns=[cfg.regime_symbol], errors="ignore")
        self.mom_score = mom.rank(axis=1, pct=True) * 100.0

        # --- 市场状态：SPY vs 200日均线 ---
        bench = self.data[cfg.regime_symbol]["close"].reindex(self.dates).ffill()
        self.bench = bench
        self.bench_ma = bench.rolling(cfg.regime_ma).mean()
        self.regime = (bench > self.bench_ma * (1 - cfg.regime_buffer)).fillna(False)

        # --- 波动率缩放系数：市场越动荡，总仓位越低 ---
        rv = bench.pct_change().rolling(cfg.vol_lookback).std() * np.sqrt(252)
        self.realized_vol = rv
        self.scalar = (cfg.vol_target / rv).clip(0.0, cfg.max_exposure).fillna(0.0)

        # --- 买入资格掩码 ---
        mask = ((self.P["pct52"] >= cfg.min_pct_52w_high) &
                (self.P["dollar_vol"] >= cfg.min_dollar_volume))
        if cfg.require_ma_stack:
            mask &= self.P["ma_stack"]
        self.eligible = mask.drop(columns=[cfg.regime_symbol], errors="ignore").fillna(False)

    # -------------------------------------------------------------------------
    # 市场状态
    # -------------------------------------------------------------------------
    def regime_on(self, date: pd.Timestamp) -> bool:
        return bool(self.regime.get(date, False))

    def target_exposure(self, date: pd.Timestamp) -> float:
        """目标总敞口 (0-1)。RISK-OFF 时为 0。"""
        if not self.regime_on(date):
            return 0.0
        return float(self.scalar.get(date, 0.0))

    def market_context(self, date: pd.Timestamp) -> dict:
        """给报告用的市场状态描述。"""
        px = float(self.bench.get(date, np.nan))
        ma = float(self.bench_ma.get(date, np.nan))
        rv = float(self.realized_vol.get(date, np.nan))
        return {
            "date": str(pd.Timestamp(date).date()),
            "regime_on": self.regime_on(date),
            "spy_price": round(px, 2) if np.isfinite(px) else None,
            "spy_ma200": round(ma, 2) if np.isfinite(ma) else None,
            "spy_vs_ma": f"{px/ma - 1:+.1%}" if np.isfinite(px) and np.isfinite(ma) and ma else None,
            "realized_vol_63d": f"{rv:.1%}" if np.isfinite(rv) else None,
            "target_exposure": f"{self.target_exposure(date):.0%}",
            "n_eligible": int(self.eligible.loc[date].sum()) if date in self.eligible.index else 0,
        }

    # -------------------------------------------------------------------------
    # 买入候选
    # -------------------------------------------------------------------------
    def candidates(self, date: pd.Timestamp, exclude: set[str] | None = None) -> pd.DataFrame:
        """当日全部合格候选，按动量分降序。exclude 通常传当前持仓。"""
        cfg = self.cfg
        exclude = exclude or set()
        if date not in self.dates:
            return pd.DataFrame()

        elig = self.eligible.loc[date]
        score = self.mom_score.loc[date]
        ext = self.P["extension"].loc[date]

        sel = elig & (score >= cfg.min_mom_score) & (ext < cfg.max_extension_atr)
        syms = [s for s in sel.index[sel.fillna(False)] if s not in exclude]
        if not syms:
            return pd.DataFrame()

        out = pd.DataFrame({
            "symbol": syms,
            "close": self.P["close"].loc[date, syms].values,
            "atr": self.P["atr"].loc[date, syms].values,
            "mom_score": score[syms].values,
            "pct52": self.P["pct52"].loc[date, syms].values,
            "extension": ext[syms].values,
            "vol_ratio": self.P["vol_ratio"].loc[date, syms].values,
            "ema50": self.P["ema50"].loc[date, syms].values,
        })
        out = out[np.isfinite(out["atr"]) & (out["atr"] > 0) & np.isfinite(out["close"])]
        return out.sort_values("mom_score", ascending=False).reset_index(drop=True)

    # -------------------------------------------------------------------------
    # 卖出判定
    # -------------------------------------------------------------------------
    def exit_check(self, date: pd.Timestamp, symbol: str, stop_price: float) -> tuple[bool, str]:
        """
        判断某个持仓今天是否该卖。返回 (是否卖出, 原因)。
        优先级：市场状态 > 止损 > 趋势破坏 > 动量衰减
        """
        cfg = self.cfg

        if not self.regime_on(date):
            return True, "大盘跌破200日均线 — 系统性离场"

        if symbol not in self.P["close"].columns or date not in self.dates:
            return False, "无数据 — 无法评估，请人工检查"

        px = float(self.P["close"].at[date, symbol])
        if not np.isfinite(px):
            return False, "无数据 — 无法评估，请人工检查"

        if px <= stop_price:
            return True, f"触发移动止损 (${stop_price:.2f})"

        if cfg.use_ma50_exit:
            e50 = self.P["ema50"].at[date, symbol]
            if np.isfinite(e50) and px < float(e50):
                return True, f"收盘跌破50日EMA (${float(e50):.2f}) — 中期趋势破坏"

        if date in self.mom_score.index and symbol in self.mom_score.columns:
            sc = self.mom_score.at[date, symbol]
            if np.isfinite(sc) and float(sc) < cfg.exit_rank_score:
                return True, f"动量分跌至 {float(sc):.0f} — 已出强势区"

        return False, "持有"

    # -------------------------------------------------------------------------
    # 辅助
    # -------------------------------------------------------------------------
    def price(self, date: pd.Timestamp, symbol: str) -> float:
        try:
            v = float(self.P["close"].at[date, symbol])
            return v if np.isfinite(v) else np.nan
        except (KeyError, IndexError):
            return np.nan

    def atr_value(self, date: pd.Timestamp, symbol: str) -> float:
        try:
            v = float(self.P["atr"].at[date, symbol])
            return v if np.isfinite(v) else np.nan
        except (KeyError, IndexError):
            return np.nan

    def score(self, date: pd.Timestamp, symbol: str) -> float:
        try:
            v = float(self.mom_score.at[date, symbol])
            return v if np.isfinite(v) else np.nan
        except (KeyError, IndexError):
            return np.nan

    def is_rebalance_day(self, date: pd.Timestamp) -> bool:
        """是否为买入信号生成日 (卖出信号每天都算，不受此限制)。"""
        return pd.Timestamp(date).weekday() == self.cfg.rebalance_weekday


# =============================================================================
# 仓位计算
# =============================================================================

def position_size(equity: float, price: float, atr_val: float, cfg: Config,
                  exposure_scalar: float = 1.0, available_cash: float | None = None) -> dict:
    """
    风险预算法定仓位。三重约束取最小值：
      ① 风险约束：打到止损只亏总资金的 risk_per_trade
      ② 权重约束：单票不超过 max_position_weight
      ③ 现金约束：不能超过可用现金

    返回 {shares, stop_price, stop_distance, risk_amount, position_value, binding_constraint}
    """
    stop_distance = cfg.stop_atr_mult * atr_val
    if stop_distance <= 0 or price <= 0:
        return {"shares": 0, "reason": "ATR或价格无效"}

    by_risk = equity * cfg.risk_per_trade / stop_distance
    by_weight = equity * cfg.max_position_weight * exposure_scalar / price
    by_cash = (available_cash / price) if available_cash is not None else float("inf")

    shares = int(min(by_risk, by_weight, by_cash))
    binding = min(
        [(by_risk, "风险预算"), (by_weight, "单票权重上限"), (by_cash, "可用现金")],
        key=lambda t: t[0],
    )[1]

    return {
        "shares": shares,
        "stop_price": round(price - stop_distance, 2),
        "stop_distance": round(stop_distance, 2),
        "risk_amount": round(shares * stop_distance, 2),
        "position_value": round(shares * price, 2),
        "binding_constraint": binding,
    }
