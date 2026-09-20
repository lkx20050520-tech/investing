#!/usr/bin/env python3
"""
每日运行入口
============

    python run_daily.py                 # 生成今日信号报告
    python run_daily.py --equity 12000  # 指定账户权益
    python run_daily.py --universe etf  # 用行业ETF池
    python run_daily.py --fresh         # 强制重新下载数据

什么时候跑
----------
美股收盘后。英国时间约 21:05 (夏令时) / 22:05 (冬令时)。
早跑没意义 —— 所有信号都基于收盘价。

跑完做什么
----------
1. 看报告里的卖出指令，去 Robinhood 执行
2. 看买入指令，去 Robinhood 执行，并立刻挂止损单
3. 用 record.py 记账 (这一步不能省，否则系统状态就错了)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

import numpy as np
import pandas as pd

from core.config import DEFAULT, POSITIONS_FILE
from core.data import clear_cache, load
from core.portfolio import load_positions, save_positions, update_trailing_stops
from core.report import print_report, write_html
from core.signals import SignalEngine, position_size
from core.universe import get_universe


def build_report(eng: SignalEngine, positions: dict, cfg, date=None,
                 force_buy_check: bool = False) -> dict:
    date = date or eng.dates[-1]
    warnings_: list[str] = []

    # ---------- 1. 更新移动止损 ----------
    prices = {s: eng.price(date, s) for s in positions}
    atrs = {s: eng.atr_value(date, s) for s in positions}
    raised = update_trailing_stops(positions, prices, atrs, cfg.trail_atr_mult)

    stale = [s for s, p in prices.items() if not np.isfinite(p)]
    if stale:
        warnings_.append(
            f"持仓 {', '.join(stale)} 当日无价格数据 —— 可能已退市/改代码/停牌。"
            f"请人工核查，系统无法对其做卖出判断。")

    # ---------- 2. 账户快照 ----------
    mv = sum(p.shares * prices[s] for s, p in positions.items() if np.isfinite(prices[s]))
    cost = sum(p.cost_basis for p in positions.values())
    equity = cfg.equity
    cash = max(equity - mv, 0.0)

    account = {
        "equity": equity, "market_value": round(mv, 2), "cash": round(cash, 2),
        "n_positions": len(positions),
        "unrealized_pnl": round(mv - cost, 2),
        "unrealized_pct": (mv / cost - 1) if cost else 0.0,
    }

    # ---------- 3. 卖出判定 ----------
    sells, holds = [], []
    for sym, pos in positions.items():
        px = prices[sym]
        if not np.isfinite(px):
            continue
        should_exit, reason = eng.exit_check(date, sym, pos.stop_price)
        rec = {
            "symbol": sym, "shares": pos.shares, "price": round(px, 2),
            "entry_price": round(pos.entry_price, 2),
            "stop_price": round(pos.stop_price, 2),
            "pnl": round(pos.shares * (px - pos.entry_price), 2),
            "pnl_pct": px / pos.entry_price - 1,
            "mom_score": (round(eng.score(date, sym)) if np.isfinite(eng.score(date, sym)) else "—"),
        }
        if should_exit:
            rec["reason"] = reason
            sells.append(rec)
        else:
            holds.append(rec)

    # ---------- 4. 买入判定 ----------
    buys: list[dict] = []
    no_buy_reason = ""
    regime = eng.regime_on(date)
    scalar = eng.target_exposure(date)
    is_rebal = eng.is_rebalance_day(date) or force_buy_check

    if not regime:
        no_buy_reason = "RISK-OFF：SPY 在 200 日均线之下，系统不开新仓。"
    elif not is_rebal:
        wd = ["周一", "周二", "周三", "周四", "周五"][cfg.rebalance_weekday]
        no_buy_reason = (f"今天不是调仓日（买入信号每{wd}生成）。"
                         f"想强制查看用 --force-buy。卖出信号不受此限制，每天都算。")
    elif scalar <= 0.05:
        no_buy_reason = f"波动率过高，目标敞口仅 {scalar:.0%}，暂不开新仓。"
    else:
        slots = cfg.max_positions - len(holds)
        if slots <= 0:
            no_buy_reason = f"持仓已满 ({len(holds)}/{cfg.max_positions})，无空位。"
        else:
            # 可用现金 = 现金 + 本轮卖出预计回笼
            proceeds = sum(s["shares"] * s["price"] for s in sells)
            budget = cash + proceeds
            gross_cap = equity * scalar
            held_mv = sum(h["shares"] * h["price"] for h in holds)
            room = max(gross_cap - held_mv, 0.0)
            budget = min(budget, room)

            cands = eng.candidates(date, exclude=set(positions))
            if cands.empty:
                no_buy_reason = "没有标的同时满足全部买入条件。空仓等待是合理状态。"
            else:
                spent = 0.0
                for _, r in cands.head(slots * 2).iterrows():
                    if len(buys) >= slots:
                        break
                    avail = budget - spent
                    if avail < 200:
                        break
                    sz = position_size(equity, float(r["close"]), float(r["atr"]), cfg,
                                       exposure_scalar=scalar, available_cash=avail)
                    if sz["shares"] <= 0 or sz["position_value"] < 200:
                        continue
                    spent += sz["position_value"]
                    buys.append({
                        "symbol": r["symbol"],
                        "price": round(float(r["close"]), 2),
                        "shares": sz["shares"],
                        "stop_price": sz["stop_price"],
                        "risk_amount": sz["risk_amount"],
                        "risk_pct": sz["risk_amount"] / equity if equity else 0.0,
                        "position_value": sz["position_value"],
                        "binding_constraint": sz["binding_constraint"],
                        "mom_score": round(float(r["mom_score"])),
                        "pct_52w_high": f"{float(r['pct52']):.0%}",
                        "extension": round(float(r["extension"]), 1),
                        "vol_ratio": round(float(r["vol_ratio"]), 2),
                    })
                if not buys:
                    no_buy_reason = ("有合格候选，但按当前权益和风险预算算出的仓位太小 "
                                     "(<$200)。账户资金不足以再开新仓。")

    # ---------- 5. 一致性警告 ----------
    if positions and equity <= mv * 0.5:
        warnings_.append(
            f"账户权益 ${equity:,.0f} 明显小于持仓市值 ${mv:,.0f} —— "
            f"config.py 里的 equity 可能没更新。用 --equity 传入真实值。")
    if sells and not regime:
        warnings_.append("RISK-OFF 状态下的清仓是系统性风控，不是择股判断。全部卖掉，不要挑。")

    rep = {
        "market": eng.market_context(date),
        "account": account,
        "sells": sells,
        "holds": sorted(holds, key=lambda h: -h["pnl_pct"]),
        "buys": buys,
        "no_buy_reason": no_buy_reason,
        "stops_raised": raised,
        "warnings": warnings_,
    }
    rep["html_path"] = str(write_html(rep))
    return rep


def main() -> None:
    ap = argparse.ArgumentParser(description="动量轮动系统 — 每日信号")
    ap.add_argument("--equity", type=float, default=None, help="账户总权益 (美元)")
    ap.add_argument("--universe", default="stocks", choices=["stocks", "etf", "both"])
    ap.add_argument("--fresh", action="store_true", help="清缓存，强制重新下载")
    ap.add_argument("--force-buy", action="store_true", help="非调仓日也计算买入信号")
    ap.add_argument("--date", default=None, help="回看某一天的信号 (YYYY-MM-DD)，调试用")
    args = ap.parse_args()

    cfg = DEFAULT if args.equity is None else replace(DEFAULT, equity=args.equity)

    if args.fresh:
        clear_cache()

    positions = load_positions()
    syms = get_universe(args.universe)
    # 持仓里的票必须在数据池里，否则无法评估卖出
    syms = list(dict.fromkeys(syms + list(positions)))

    data = load(syms, start="2015-01-01")
    eng = SignalEngine(data, cfg)

    date = None
    if args.date:
        want = pd.Timestamp(args.date)
        avail = eng.dates[eng.dates <= want]
        if len(avail) == 0:
            sys.exit(f"{args.date} 之前没有数据。")
        date = avail[-1]

    rep = build_report(eng, positions, cfg, date=date, force_buy_check=args.force_buy)

    # 止损可能被上移，保存回台账
    save_positions(positions)

    print_report(rep)


if __name__ == "__main__":
    main()
