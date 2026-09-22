#!/usr/bin/env python3
"""
记账工具 —— 在 Robinhood 执行完之后用
=====================================

    python record.py buy  NVDA 12 178.40           # 买入 12 股 @ $178.40
    python record.py buy  NVDA 12 178.40 --stop 165.20   # 手动指定止损
    python record.py sell NVDA 191.05              # 卖出 (全部)
    python record.py list                          # 查看当前持仓
    python record.py stats                         # 真实交易统计
    python record.py fix  NVDA --shares 10         # 修正记录
    python record.py reconcile --from-json rh.json # 和券商快照对账

⚠️ 为什么这步不能省
--------------------
移动止损依赖持仓期最高价，动量衰减判断依赖"我持有什么"。
台账不准，系统就在用错误的状态给你出信号 —— 比没有系统更危险。

**在 Robinhood 执行完的当天就记，不要拖到第二天。**
"""

from __future__ import annotations

import argparse
import sys
from datetime import date as _date
from pathlib import Path

import numpy as np

from core.config import DEFAULT
from core.data import load
from core.portfolio import (close_position, load_positions, open_position,
                            realized_stats, save_positions)
from core.signals import SignalEngine
from core.universe import get_universe


def _engine_for(symbols: list[str]):
    """只为拿 ATR 和现价，数据不需要很长。"""
    syms = list(dict.fromkeys(get_universe("stocks", live_fetch=False) + symbols))
    data = load(syms, start="2023-01-01")
    return SignalEngine(data, DEFAULT)


# =============================================================================
def cmd_buy(args) -> None:
    positions = load_positions()
    sym = args.symbol.upper()

    if sym in positions:
        sys.exit(f"{sym} 已在持仓中 ({positions[sym].shares} 股)。"
                 f"本系统不加仓。要修正记录用 `record.py fix`。")

    stop = args.stop
    if stop is None:
        print(f"[计算止损] 拉取 {sym} 的 ATR...")
        try:
            eng = _engine_for([sym])
            d = eng.dates[-1]
            a = eng.atr_value(d, sym)
            if not np.isfinite(a) or a <= 0:
                sys.exit(f"无法获取 {sym} 的 ATR。请用 --stop 手动指定止损价。")
            stop = round(args.price - DEFAULT.stop_atr_mult * a, 2)
            print(f"[计算止损] ATR20 = ${a:.2f}  →  止损 = ${stop:.2f} "
                  f"(入场价 - {DEFAULT.stop_atr_mult} × ATR)")
        except Exception as e:
            sys.exit(f"计算止损失败: {e}\n请用 --stop 手动指定。")

    if stop >= args.price:
        sys.exit(f"止损价 ${stop:.2f} 不能高于等于入场价 ${args.price:.2f}。")

    pos = open_position(positions, sym, args.shares, args.price, stop,
                        on_date=args.date, note=args.note or "")
    save_positions(positions)

    risk = pos.shares * (pos.entry_price - pos.stop_price)
    print(f"\n✓ 已记录买入")
    print(f"  {sym}  {pos.shares} 股 @ ${pos.entry_price:.2f}  =  ${pos.cost_basis:,.2f}")
    print(f"  止损 ${pos.stop_price:.2f}   最大亏损 ${risk:,.2f}")
    print(f"\n→ 现在去 Robinhood 挂止损单：Stop Loss @ ${pos.stop_price:.2f}")
    print(f"  当前持仓 {len(positions)}/{DEFAULT.max_positions} 只")


def cmd_sell(args) -> None:
    positions = load_positions()
    sym = args.symbol.upper()
    if sym not in positions:
        sys.exit(f"{sym} 不在持仓中。当前持仓: {', '.join(positions) or '无'}")

    rec = close_position(positions, sym, args.price,
                         reason=args.reason or "手动记录", on_date=args.date)
    save_positions(positions)

    sign = "盈利" if rec["pnl"] >= 0 else "亏损"
    print(f"\n✓ 已记录卖出")
    print(f"  {sym}  {rec['shares']} 股 @ ${rec['exit_price']:.2f}")
    print(f"  成本 ${rec['entry_price']:.2f}  →  {sign} ${abs(rec['pnl']):,.2f} "
          f"({rec['pnl_pct']:+.1%})，持有 {rec['holding_days']} 天")
    print(f"  当前持仓 {len(positions)}/{DEFAULT.max_positions} 只")

    if rec["pnl"] < 0:
        print(f"\n  这笔亏了。按规则止损是系统在正常工作 —— "
              f"回测里胜率只有 34%，亏损交易本来就是多数。")


def cmd_list(args) -> None:
    positions = load_positions()
    if not positions:
        print("当前无持仓。")
        return

    print(f"\n当前持仓 ({len(positions)}/{DEFAULT.max_positions})\n")
    try:
        eng = _engine_for(list(positions))
        d = eng.dates[-1]
        prices = {s: eng.price(d, s) for s in positions}
        print(f"  {'标的':<8}{'股数':>7}{'成本':>10}{'现价':>10}{'盈亏':>10}"
              f"{'止损':>10}{'距止损':>9}")
        print("  " + "-" * 64)
        total_cost = total_mv = 0.0
        for s, p in positions.items():
            px = prices.get(s, np.nan)
            if np.isfinite(px):
                pnl_pct = px / p.entry_price - 1
                to_stop = px / p.stop_price - 1
                total_mv += p.shares * px
                print(f"  {s:<8}{p.shares:>7}{p.entry_price:>10.2f}{px:>10.2f}"
                      f"{pnl_pct:>9.1%}{p.stop_price:>10.2f}{to_stop:>8.1%}")
            else:
                print(f"  {s:<8}{p.shares:>7}{p.entry_price:>10.2f}{'—':>10}"
                      f"{'—':>10}{p.stop_price:>10.2f}{'—':>9}")
            total_cost += p.cost_basis
        if total_mv:
            print("  " + "-" * 64)
            print(f"  合计成本 ${total_cost:,.2f}   市值 ${total_mv:,.2f}   "
                  f"浮动盈亏 ${total_mv-total_cost:+,.2f} ({total_mv/total_cost-1:+.1%})")
    except Exception as e:
        print(f"  (拉取现价失败: {e}，只显示台账记录)\n")
        for s, p in positions.items():
            print(f"  {s:<8}{p.shares:>7} 股 @ ${p.entry_price:.2f}  "
                  f"止损 ${p.stop_price:.2f}  入场 {p.entry_date}")


def cmd_stats(args) -> None:
    st = realized_stats()
    print("\n真实交易统计 (基于你实际执行的记录，不是回测)\n")
    if st.get("n_trades", 0) == 0:
        print("  还没有已平仓交易。")
        return
    for k, v in st.items():
        if k == "exit_reasons":
            print(f"  {'卖出原因':.<20}")
            for rk, rv in v.items():
                print(f"      {rk}: {rv}")
        else:
            print(f"  {k:.<20} {v}")

    n = st["n_trades"]
    if n < 30:
        print(f"\n  ⚠️ 只有 {n} 笔交易，样本太小，这些数字没有统计意义。"
              f"至少积累 50 笔再谈胜率。")


def cmd_fix(args) -> None:
    positions = load_positions()
    sym = args.symbol.upper()
    if sym not in positions:
        sys.exit(f"{sym} 不在持仓中。")
    p = positions[sym]
    changed = []
    for f in ("shares", "entry_price", "stop_price", "peak_price", "entry_date"):
        v = getattr(args, f, None)
        if v is not None:
            old = getattr(p, f)
            setattr(p, f, v)
            changed.append(f"{f}: {old} → {v}")
    if not changed:
        sys.exit("没有指定要改的字段。可改: --shares --entry-price --stop-price "
                 "--peak-price --entry-date")
    save_positions(positions)
    print(f"✓ 已修正 {sym}")
    for c in changed:
        print(f"  {c}")


def cmd_reconcile(args) -> None:
    """
    和券商快照对账。只读、只报告 —— 不自动改台账。

    为什么不自动改：台账里的 peak_price / stop_price 是券商没有的内部状态，
    用一个可能过期或不完整的快照去覆盖，会静默摧毁全部止损状态。
    所以这里只打印该跑哪条命令，由你确认后自己执行。
    """
    from core.reconcile import diff_positions, parse_broker_snapshot

    src = args.from_json
    try:
        raw = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
    except OSError as e:
        sys.exit(f"读不到券商快照: {e}")

    try:
        broker, broker_equity, as_of = parse_broker_snapshot(raw)
    except ValueError as e:      # JSONDecodeError 也是 ValueError
        sys.exit(f"券商快照格式有问题: {e}")

    positions = load_positions()
    res = diff_positions(
        positions, broker,
        ledger_equity=args.equity if args.equity is not None else DEFAULT.equity,
        broker_equity=broker_equity,
        as_of=as_of,
    )

    stamp = f" (快照日期 {res.as_of})" if res.as_of else ""
    print(f"\n对账{stamp}：台账 {len(positions)} 只，券商 {len(broker)} 只\n")

    if res.ok:
        print(f"  ✓ 完全一致（{len(res.matched)} 只匹配）")
    else:
        sev_label = {"critical": "严重", "warning": "注意", "info": "提示"}
        for d in res.issues:
            tag = sev_label.get(d.severity, d.severity)
            head = f"{d.symbol} " if d.symbol else ""
            print(f"  [{tag}] {head}{d.detail}")
            if d.suggestion:
                print(f"         → {d.suggestion}")
            print()
        if res.matched:
            print(f"  ✓ 另有 {len(res.matched)} 只完全匹配: {', '.join(res.matched)}\n")

    n_crit = len(res.critical)
    if n_crit:
        print(f"  ⚠️ {n_crit} 项严重不一致。在修完之前，系统出的信号是基于错误状态的。")

    print("  注：peak_price / stop_price / entry_date 券商没有，无法对账 —— "
          "这几个字段只能靠系统自己维护。")

    sys.exit(1 if n_crit else 0)


# =============================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="持仓台账")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("buy", help="记录买入")
    b.add_argument("symbol"); b.add_argument("shares", type=int)
    b.add_argument("price", type=float)
    b.add_argument("--stop", type=float, default=None, help="止损价，不填则按 2.5×ATR 自动算")
    b.add_argument("--date", default=None, help="成交日 YYYY-MM-DD，默认今天")
    b.add_argument("--note", default=None)
    b.set_defaults(func=cmd_buy)

    s = sub.add_parser("sell", help="记录卖出")
    s.add_argument("symbol"); s.add_argument("price", type=float)
    s.add_argument("--reason", default=None)
    s.add_argument("--date", default=None)
    s.set_defaults(func=cmd_sell)

    sub.add_parser("list", help="查看当前持仓").set_defaults(func=cmd_list)
    sub.add_parser("stats", help="真实交易统计").set_defaults(func=cmd_stats)

    f = sub.add_parser("fix", help="修正持仓记录")
    f.add_argument("symbol")
    f.add_argument("--shares", type=int, default=None)
    f.add_argument("--entry-price", type=float, default=None, dest="entry_price")
    f.add_argument("--stop-price", type=float, default=None, dest="stop_price")
    f.add_argument("--peak-price", type=float, default=None, dest="peak_price")
    f.add_argument("--entry-date", default=None, dest="entry_date")
    f.set_defaults(func=cmd_fix)

    r = sub.add_parser("reconcile", help="和券商持仓快照对账 (只读，不改台账)")
    r.add_argument("--from-json", required=True, dest="from_json",
                   help="券商快照 JSON 文件路径，'-' 表示从 stdin 读")
    r.add_argument("--equity", type=float, default=None,
                   help="台账侧权益，默认用 config.py 里的值")
    r.set_defaults(func=cmd_reconcile)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
