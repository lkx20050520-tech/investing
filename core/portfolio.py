"""
本地持仓台账
============

手动执行模式的核心组件。

为什么需要它
------------
接券商 API 时，持仓可以随时从券商查询，本地不需要记状态。
但你在 Robinhood 手动下单，程序无从得知你实际买了没有、买了多少、什么价格。
所以必须有一个本地台账，由你在每次执行后手动确认。

⚠️ 台账不准 = 整个系统失效
---------------------------
移动止损依赖 "持仓期最高价"，动量衰减判断依赖 "我持有哪些票"。
如果你在 Robinhood 买了但没在这里记，程序永远不会提醒你卖它。
如果你卖了但没记，程序会一直用错误的持仓算敞口。

纪律要求：**在 Robinhood 执行完的当天就记账，不要拖。**

数据文件
--------
data/positions.json   当前持仓 (含每只票的持仓期最高价，用于移动止损)
data/trade_log.csv    已平仓交易流水 (用于复盘和统计真实胜率)
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import date as _date
from pathlib import Path

from core.config import POSITIONS_FILE, TRADE_LOG_FILE


@dataclass
class Position:
    symbol: str
    shares: int
    entry_price: float
    entry_date: str              # YYYY-MM-DD
    stop_price: float            # 当前止损位 (会随价格上移，只升不降)
    peak_price: float            # 持仓期最高收盘价，移动止损的基准
    initial_stop: float = 0.0    # 入场时的止损，用于事后评估风险控制是否有效
    note: str = ""

    @property
    def cost_basis(self) -> float:
        return self.shares * self.entry_price

    def unrealized(self, current_price: float) -> dict:
        pnl = self.shares * (current_price - self.entry_price)
        return {
            "pnl": round(pnl, 2),
            "pnl_pct": round(current_price / self.entry_price - 1, 4),
            "market_value": round(self.shares * current_price, 2),
            "risk_remaining": round(self.shares * (current_price - self.stop_price), 2),
        }


# =============================================================================
# 读写
# =============================================================================

def load_positions(path: Path = POSITIONS_FILE) -> dict[str, Position]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"持仓文件 {path} 格式损坏: {e}\n"
            f"这个文件是系统的状态核心，不要手动乱改。\n"
            f"如果确实需要重置，先备份再删除该文件。"
        )
    return {k: Position(**v) for k, v in raw.items()}


def save_positions(positions: dict[str, Position], path: Path = POSITIONS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: asdict(v) for k, v in positions.items()}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# =============================================================================
# 操作
# =============================================================================

def open_position(positions: dict[str, Position], symbol: str, shares: int,
                  price: float, stop: float, on_date: str | None = None,
                  note: str = "") -> Position:
    if symbol in positions:
        raise ValueError(
            f"{symbol} 已在持仓中 ({positions[symbol].shares} 股)。\n"
            f"本系统不做加仓 —— 加仓会打乱风险预算。\n"
            f"如果是要修正记录，先 close 再重新 open。"
        )
    pos = Position(
        symbol=symbol, shares=shares, entry_price=price,
        entry_date=on_date or str(_date.today()),
        stop_price=stop, peak_price=price, initial_stop=stop, note=note,
    )
    positions[symbol] = pos
    return pos


def close_position(positions: dict[str, Position], symbol: str, price: float,
                   reason: str, on_date: str | None = None,
                   log_path: Path = TRADE_LOG_FILE) -> dict:
    if symbol not in positions:
        raise ValueError(f"{symbol} 不在持仓中。当前持仓: {list(positions) or '无'}")

    pos = positions.pop(symbol)
    exit_date = on_date or str(_date.today())
    pnl = pos.shares * (price - pos.entry_price)
    holding_days = (_date.fromisoformat(exit_date) - _date.fromisoformat(pos.entry_date)).days

    record = {
        "symbol": symbol,
        "entry_date": pos.entry_date,
        "exit_date": exit_date,
        "shares": pos.shares,
        "entry_price": round(pos.entry_price, 4),
        "exit_price": round(price, 4),
        "pnl": round(pnl, 2),
        "pnl_pct": round(price / pos.entry_price - 1, 4),
        "holding_days": holding_days,
        "initial_stop": round(pos.initial_stop, 4),
        "final_stop": round(pos.stop_price, 4),
        "exit_reason": reason,
    }
    _append_trade_log(record, log_path)
    return record


def _append_trade_log(record: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(record))
        if not exists:
            w.writeheader()
        w.writerow(record)


def update_trailing_stops(positions: dict[str, Position], prices: dict[str, float],
                          atrs: dict[str, float], trail_mult: float) -> list[str]:
    """
    每日更新移动止损。止损只升不降 —— 这是趋势跟踪的铁律。
    返回被上调了止损的标的列表。
    """
    raised = []
    for sym, pos in positions.items():
        px, a = prices.get(sym), atrs.get(sym)
        if px is None or a is None or px != px or a != a:   # NaN 检查
            continue
        if px > pos.peak_price:
            pos.peak_price = float(px)
        new_stop = pos.peak_price - trail_mult * float(a)
        if new_stop > pos.stop_price:
            pos.stop_price = round(float(new_stop), 2)
            raised.append(sym)
    return raised


# =============================================================================
# 统计
# =============================================================================

def realized_stats(log_path: Path = TRADE_LOG_FILE) -> dict:
    """从真实交易流水算统计 —— 这才是你的实际表现，不是回测数字。"""
    if not log_path.exists():
        return {"n_trades": 0, "note": "还没有已平仓交易"}

    import pandas as pd
    df = pd.read_csv(log_path)
    if df.empty:
        return {"n_trades": 0, "note": "还没有已平仓交易"}

    wins, losses = df[df.pnl > 0], df[df.pnl <= 0]
    return {
        "n_trades": len(df),
        "total_pnl": round(df.pnl.sum(), 2),
        "win_rate": f"{len(wins)/len(df):.1%}",
        "avg_win_pct": f"{wins.pnl_pct.mean():.1%}" if len(wins) else "n/a",
        "avg_loss_pct": f"{losses.pnl_pct.mean():.1%}" if len(losses) else "n/a",
        "payoff_ratio": (f"{abs(wins.pnl_pct.mean()/losses.pnl_pct.mean()):.2f}"
                         if len(wins) and len(losses) and losses.pnl_pct.mean() else "n/a"),
        "profit_factor": (f"{wins.pnl.sum()/abs(losses.pnl.sum()):.2f}"
                          if len(losses) and losses.pnl.sum() else "n/a"),
        "avg_holding_days": round(df.holding_days.mean(), 1),
        "worst_trade": round(df.pnl.min(), 2),
        "best_trade": round(df.pnl.max(), 2),
        "exit_reasons": df.exit_reason.value_counts().to_dict(),
    }
