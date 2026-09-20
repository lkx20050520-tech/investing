"""
回测引擎
========

⚠️ 回测结果不是对未来的承诺
-----------------------------
回测告诉你的是"这套规则在过去这段数据上会怎样"，仅此而已。
它会系统性高估实际表现，原因包括：
  - 幸存者偏差 (universe.py 里有说明)
  - 滑点和手动执行延迟被低估
  - 参数是在同一批数据上看着结果调的 (即使你很克制)

看回测的正确姿势：
  ✅ 看最大回撤 —— 你能不能扛住
  ✅ 看参数敏感性 —— 结果是否平滑 (--sweep)
  ✅ 看熊市段表现 —— 2018Q4 / 2020Q1 / 2022 全年
  ✅ 看卖出原因分布 —— 风控是否在起作用
  ❌ 不要看 CAGR 然后计算"三年后我有多少钱"
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from core.config import Config, DEFAULT
from core.signals import SignalEngine, position_size


@dataclass
class _Pos:
    symbol: str
    shares: float
    entry_price: float
    entry_date: pd.Timestamp
    stop: float
    peak: float


@dataclass
class Backtest:
    cfg: Config
    engine: SignalEngine
    equity_curve: pd.Series | None = field(default=None)
    trades: pd.DataFrame | None = field(default=None)
    detail: pd.DataFrame | None = field(default=None)

    # -------------------------------------------------------------------------
    def run(self, start: str, end: str | None = None) -> "Backtest":
        cfg, eng = self.cfg, self.engine
        dates = eng.dates[eng.dates >= pd.Timestamp(start)]
        if end:
            dates = dates[dates <= pd.Timestamp(end)]
        if len(dates) == 0:
            raise ValueError(f"{start} 之后没有数据。检查数据起始日期。")

        close, atr_p, ema50 = eng.P["close"], eng.P["atr"], eng.P["ema50"]
        cost_bps = (cfg.slippage_bps + cfg.commission_bps) / 1e4

        cash = cfg.equity
        positions: dict[str, _Pos] = {}
        rows, trade_log = [], []

        for date in dates:
            regime = eng.regime_on(date)
            scalar = float(eng.scalar.get(date, 0.0))
            px_row, atr_row, ema_row = close.loc[date], atr_p.loc[date], ema50.loc[date]
            score_row = eng.mom_score.loc[date] if date in eng.mom_score.index else None

            # --- 1. 更新移动止损 (只升不降) ---
            for pos in positions.values():
                px, a = px_row.get(pos.symbol), atr_row.get(pos.symbol)
                if pd.notna(px):
                    pos.peak = max(pos.peak, float(px))
                if pd.notna(a) and a > 0:
                    pos.stop = max(pos.stop, pos.peak - cfg.trail_atr_mult * float(a))

            # --- 2. 卖出检查 (每日，不等调仓日) ---
            for sym, pos in list(positions.items()):
                px = px_row.get(sym)
                if pd.isna(px):
                    continue
                px = float(px)

                reason = None
                if not regime:
                    reason = "REGIME_OFF"
                elif px <= pos.stop:
                    reason = "TRAILING_STOP"
                elif cfg.use_ma50_exit and pd.notna(ema_row.get(sym)) and px < float(ema_row[sym]):
                    reason = "BELOW_MA50"
                elif (score_row is not None and sym in score_row.index
                      and pd.notna(score_row[sym]) and float(score_row[sym]) < cfg.exit_rank_score):
                    reason = "RANK_DECAY"

                if reason:
                    fill = px * (1 - cost_bps)
                    cash += pos.shares * fill
                    trade_log.append({
                        "symbol": sym, "entry_date": pos.entry_date, "exit_date": date,
                        "entry": pos.entry_price, "exit": fill,
                        "pnl": pos.shares * (fill - pos.entry_price),
                        "pnl_pct": fill / pos.entry_price - 1,
                        "days": (date - pos.entry_date).days, "reason": reason,
                    })
                    del positions[sym]

            # --- 3. 买入 (仅调仓日，且 RISK-ON) ---
            if eng.is_rebalance_day(date) and regime and scalar > 0.05:
                mkt_val = sum(p.shares * float(px_row.get(p.symbol, p.entry_price))
                              for p in positions.values())
                total_eq = cash + mkt_val
                gross_cap = total_eq * scalar          # 组合总敞口上限
                slots = cfg.max_positions - len(positions)

                if slots > 0 and mkt_val < gross_cap:
                    for _, r in eng.candidates(date, exclude=set(positions)).head(slots).iterrows():
                        price = float(r["close"])
                        room = max(gross_cap - mkt_val, 0.0)
                        sz = position_size(total_eq, price, float(r["atr"]), cfg,
                                           exposure_scalar=scalar,
                                           available_cash=min(cash * 0.98, room))
                        shares = sz["shares"]
                        fill = price * (1 + cost_bps)
                        if shares <= 0 or shares * fill < 200:
                            continue

                        cash -= shares * fill
                        mkt_val += shares * price
                        positions[r["symbol"]] = _Pos(
                            symbol=r["symbol"], shares=shares, entry_price=fill,
                            entry_date=date, stop=fill - sz["stop_distance"], peak=fill)

            # --- 4. 记录 ---
            mv = sum(p.shares * float(px_row.get(p.symbol, p.entry_price))
                     for p in positions.values())
            eq = cash + mv
            rows.append({"date": date, "equity": eq, "cash": cash, "n_pos": len(positions),
                         "regime": regime, "scalar": scalar, "gross": mv / eq if eq else 0.0})

        self.detail = pd.DataFrame(rows).set_index("date")
        self.equity_curve = self.detail["equity"]
        self.trades = pd.DataFrame(trade_log)
        return self

    # -------------------------------------------------------------------------
    def stats(self) -> dict:
        eq, d = self.equity_curve, self.detail
        rets = eq.pct_change().dropna()
        years = len(eq) / 252
        total = eq.iloc[-1] / eq.iloc[0] - 1
        cagr = (1 + total) ** (1 / years) - 1 if years > 0 else 0.0
        dd = eq / eq.cummax() - 1
        vol = rets.std() * np.sqrt(252)
        down = rets[rets < 0].std() * np.sqrt(252)

        t = self.trades
        has = t is not None and len(t) > 0
        w = t[t.pnl > 0] if has else pd.DataFrame()
        l = t[t.pnl <= 0] if has else pd.DataFrame()
        pf = (w.pnl.sum() / abs(l.pnl.sum())) if len(l) and l.pnl.sum() else np.nan

        return {
            "总收益": f"{total:.1%}",
            "年化收益 CAGR": f"{cagr:.1%}",
            "年化波动": f"{vol:.1%}",
            "最大回撤": f"{dd.min():.1%}",
            "Sharpe": f"{cagr/vol:.2f}" if vol else "n/a",
            "Sortino": f"{cagr/down:.2f}" if down else "n/a",
            "Calmar": f"{cagr/abs(dd.min()):.2f}" if dd.min() < 0 else "n/a",
            "平均仓位": f"{d['gross'].mean():.0%}",
            "空仓天数占比": f"{(d['n_pos']==0).mean():.0%}",
            "RISK-ON天数占比": f"{d['regime'].mean():.0%}",
            "交易次数": int(len(t)) if has else 0,
            "胜率": f"{len(w)/len(t):.1%}" if has else "n/a",
            "平均盈利": f"{w.pnl_pct.mean():.1%}" if len(w) else "n/a",
            "平均亏损": f"{l.pnl_pct.mean():.1%}" if len(l) else "n/a",
            "盈亏比": (f"{abs(w.pnl_pct.mean()/l.pnl_pct.mean()):.2f}"
                     if len(w) and len(l) and l.pnl_pct.mean() else "n/a"),
            "Profit Factor": f"{pf:.2f}" if np.isfinite(pf) else "n/a",
            "平均持仓天数": f"{t.days.mean():.0f}" if has else "n/a",
        }

    def yearly(self) -> pd.DataFrame:
        eq = self.equity_curve
        yr_end = eq.resample("YE").last()
        yr_ret = pd.concat([eq.iloc[:1], yr_end]).pct_change().dropna()
        yr_dd = eq.groupby(eq.index.year).apply(lambda s: (s / s.cummax() - 1).min())
        out = pd.DataFrame({"收益": yr_ret.values}, index=yr_ret.index.year)
        out["年内最大回撤"] = yr_dd.reindex(out.index).values
        return out

    def drawdown_periods(self, top_n: int = 5) -> pd.DataFrame:
        """最大的几段回撤。看这个比看 CAGR 重要得多。"""
        eq = self.equity_curve
        dd = eq / eq.cummax() - 1
        in_dd = dd < -0.001
        periods, start = [], None
        for dt, flag in in_dd.items():
            if flag and start is None:
                start = dt
            elif not flag and start is not None:
                seg = dd.loc[start:dt]
                periods.append({"开始": start.date(), "结束": dt.date(),
                                "最大回撤": seg.min(), "天数": (dt - start).days})
                start = None
        if start is not None:
            seg = dd.loc[start:]
            periods.append({"开始": start.date(), "结束": "未恢复",
                            "最大回撤": seg.min(), "天数": (eq.index[-1] - start).days})
        if not periods:
            return pd.DataFrame()
        return (pd.DataFrame(periods).sort_values("最大回撤").head(top_n)
                .assign(最大回撤=lambda d: d["最大回撤"].map("{:.1%}".format)))


# =============================================================================
# 参数敏感性扫描
# =============================================================================

def sweep(data: dict, base: Config, start: str, param: str, values: list) -> pd.DataFrame:
    """
    扫描单个参数。**结果必须平滑。**
    如果 CAGR 只在某一个值上高、邻近值就崩，说明你在拟合噪音，不是找到规律。
    """
    rows = []
    for v in values:
        cfg = replace(base, **{param: v})
        bt = Backtest(cfg, SignalEngine(data, cfg)).run(start=start)
        s = bt.stats()
        rows.append({param: v, "CAGR": s["年化收益 CAGR"], "MaxDD": s["最大回撤"],
                     "Sharpe": s["Sharpe"], "Calmar": s["Calmar"],
                     "交易数": s["交易次数"], "平均仓位": s["平均仓位"]})
    return pd.DataFrame(rows)


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    from core.data import load
    from core.universe import get_universe

    ap = argparse.ArgumentParser(description="动量轮动系统 — 回测")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--universe", default="stocks", choices=["stocks", "etf", "both"])
    ap.add_argument("--equity", type=float, default=None)
    ap.add_argument("--sweep", action="store_true", help="跑参数敏感性扫描")
    ap.add_argument("--stress", action="store_true", help="单独跑历史压力测试段")
    args = ap.parse_args()

    cfg = DEFAULT if args.equity is None else replace(DEFAULT, equity=args.equity)
    syms = get_universe(args.universe)
    data = load(syms, start="2014-01-01")

    eng = SignalEngine(data, cfg)
    bt = Backtest(cfg, eng).run(start=args.start, end=args.end)

    print("\n" + "=" * 60)
    print(f"回测结果  {args.start} ~ {args.end or '今'}  |  池: {args.universe} ({len(data)}只)")
    print("=" * 60)
    for k, v in bt.stats().items():
        print(f"{k:.<24} {v}")

    print("\n--- 分年度 ---")
    print(bt.yearly().apply(lambda c: c.map("{:.1%}".format)).to_string())

    print("\n--- 最大的几段回撤 (这才是你要能扛住的东西) ---")
    dp = bt.drawdown_periods()
    print(dp.to_string(index=False) if len(dp) else "无")

    if bt.trades is not None and len(bt.trades):
        print("\n--- 卖出原因分布 ---")
        print(bt.trades.reason.value_counts().to_string())

    bench = data[cfg.regime_symbol]["close"].loc[args.start:]
    print(f"\n--- 基准 {cfg.regime_symbol} 同期 ---")
    print(f"总收益.................. {bench.iloc[-1]/bench.iloc[0]-1:.1%}")
    print(f"最大回撤................ {(bench/bench.cummax()-1).min():.1%}")

    if args.stress:
        print("\n" + "=" * 60)
        print("压力测试 — 这三段决定你能不能活下来")
        print("=" * 60)
        for label, s, e in [("2018Q4 急跌", "2018-09-01", "2019-01-31"),
                            ("2020Q1 疫情崩盘+V型反转", "2020-02-01", "2020-06-30"),
                            ("2022 趋势熊市", "2022-01-01", "2022-12-31")]:
            try:
                sub = Backtest(cfg, eng).run(start=s, end=e)
                b = data[cfg.regime_symbol]["close"].loc[s:e]
                st = sub.stats()
                print(f"\n[{label}]")
                print(f"  策略: 收益 {st['总收益']:>7}  回撤 {st['最大回撤']:>7}  "
                      f"平均仓位 {st['平均仓位']:>5}")
                print(f"  {cfg.regime_symbol}: 收益 {b.iloc[-1]/b.iloc[0]-1:>7.1%}  "
                      f"回撤 {(b/b.cummax()-1).min():>7.1%}")
            except Exception as ex:
                print(f"\n[{label}] 跳过: {ex}")

    if args.sweep:
        print("\n" + "=" * 60)
        print("参数敏感性 — 结果应当平滑。只在单点好 = 过拟合，别用。")
        print("=" * 60)
        for p, vals in [
            ("regime_ma", [100, 150, 200, 250]),
            ("min_mom_score", [80, 85, 90, 95]),
            ("stop_atr_mult", [2.0, 2.5, 3.0, 3.5]),
            ("trail_atr_mult", [2.5, 3.0, 3.5, 4.0, 5.0]),
            ("max_positions", [5, 8, 10, 12]),
            ("vol_target", [0.10, 0.15, 0.20, 0.25]),
        ]:
            print(f"\n[{p}]")
            print(sweep(data, cfg, args.start, p, vals).to_string(index=False))


if __name__ == "__main__":
    main()
