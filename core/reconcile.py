"""
台账 ↔ 券商对账
===============

`core/portfolio.py` 的台账是状态的唯一来源，但它靠你手动维护，所以会错。
漏记一笔买入 -> 系统永远不提醒你卖它；漏记一笔卖出 -> 系统用幽灵持仓算敞口。
这两种错都会让整个信号失效，而且你自己很难发现。

这个模块做的事：拿券商的持仓快照和台账比对，把差异列出来。

⚠️ 它不能替代记账
------------------
券商只知道 symbol / 股数 / 平均成本。它不知道：

  - `peak_price`  持仓期最高收盘价 —— 移动止损的基准
  - `stop_price`  当前止损位
  - `initial_stop` 入场时的止损
  - `entry_date`  真实入场日 (券商的成本价是多笔成交的平均，日期也可能被加仓摊平)

这些是系统自己算出来的内部状态 (见 `update_trailing_stops`)，券商那边根本不存在。
所以对账只能告诉你"哪里不一致"，修正仍然由你确认后执行 —— 这是有意的设计，
不是偷懒：自动覆盖台账会让一个错误的券商快照静默摧毁全部止损状态。

⚠️ 这里没有任何下单能力
-----------------------
本模块只读。`CLAUDE.md` 约束 #4 依然成立：不接券商 API 下单。

数据从哪来
----------
券商快照以 JSON 传入，本模块不发网络请求 (保持可离线测试)。
Robinhood 官方 MCP (agent.robinhood.com/mcp/trading，OAuth，只读部分) 能读出持仓和
余额，把结果存成下面的格式即可：

    {
      "as_of": "2026-09-22",
      "equity": 12480.33,
      "positions": [
        {"symbol": "NVDA", "shares": 12, "avg_cost": 178.40}
      ]
    }

字段名容忍常见别名 (quantity / average_buy_price 等)，因为不同数据源叫法不一样。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from core.portfolio import Position

# 成本价默认容差 0.5%。台账记的是你看到的成交价，券商记的是多笔成交的加权平均，
# 两者本来就不会完全相等 —— 容差太小会天天报假警。
COST_TOL = 0.005

# 权益默认容差 1%。权益每天都在波动，只在明显偏离时提醒。
EQUITY_TOL = 0.01

CRITICAL, WARNING, INFO = "critical", "warning", "info"


@dataclass(frozen=True)
class BrokerPosition:
    """券商侧的一条持仓。shares 用 float —— Robinhood 支持碎股。"""
    symbol: str
    shares: float
    avg_cost: float | None = None


@dataclass(frozen=True)
class Discrepancy:
    kind: str
    symbol: str
    severity: str
    detail: str
    suggestion: str = ""   # 修正用的命令；空表示没有可直接执行的修法


@dataclass
class ReconcileResult:
    matched: list[str] = field(default_factory=list)
    issues: list[Discrepancy] = field(default_factory=list)
    broker_equity: float | None = None
    ledger_equity: float | None = None
    as_of: str = ""

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def critical(self) -> list[Discrepancy]:
        return [d for d in self.issues if d.severity == CRITICAL]


# =============================================================================
# 解析券商快照
# =============================================================================

_SHARE_KEYS = ("shares", "quantity", "qty", "position")
_COST_KEYS = ("avg_cost", "average_buy_price", "average_cost", "avg_price", "cost_basis_per_share")
_SYMBOL_KEYS = ("symbol", "ticker", "instrument_symbol")
_EQUITY_KEYS = ("equity", "total_equity", "portfolio_value", "market_value")


def _pick(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _as_float(v, what: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} 无法解析为数字: {v!r}")


def parse_broker_snapshot(payload: dict | str | Path) -> tuple[list[BrokerPosition], float | None, str]:
    """
    把券商快照解析成 (持仓列表, 权益, 快照日期)。

    payload 可以是已解析的 dict、JSON 字符串，或文件路径。
    这是系统边界，所以这里做校验 —— 内部函数不再重复校验。
    """
    if isinstance(payload, Path):
        payload = json.loads(payload.read_text(encoding="utf-8"))
    elif isinstance(payload, str):
        payload = json.loads(payload)

    if not isinstance(payload, dict):
        raise ValueError("券商快照必须是一个 JSON 对象")

    raw = payload.get("positions")
    if raw is None:
        raise ValueError("券商快照缺少 'positions' 字段")
    if not isinstance(raw, list):
        raise ValueError("'positions' 必须是数组")

    out: list[BrokerPosition] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"positions[{i}] 不是对象")

        sym = _pick(item, _SYMBOL_KEYS)
        if not sym:
            raise ValueError(f"positions[{i}] 缺少 symbol")
        sym = str(sym).strip().upper()
        if sym in seen:
            raise ValueError(
                f"券商快照里 {sym} 出现了两次。本系统不加仓，一只票只该有一条持仓；"
                f"请先确认快照是不是把多个账户混在一起了。"
            )
        seen.add(sym)

        shares_raw = _pick(item, _SHARE_KEYS)
        if shares_raw is None:
            raise ValueError(f"{sym} 缺少股数字段 (接受 {'/'.join(_SHARE_KEYS)})")
        shares = _as_float(shares_raw, f"{sym} 的股数")

        # 券商常把已平仓的票留在列表里，股数为 0。视为不存在。
        if shares <= 0:
            continue

        cost_raw = _pick(item, _COST_KEYS)
        cost = _as_float(cost_raw, f"{sym} 的成本价") if cost_raw is not None else None
        if cost is not None and cost <= 0:
            cost = None      # 0 成本没有意义，当作缺失，跳过成本比对而不是报错

        out.append(BrokerPosition(symbol=sym, shares=shares, avg_cost=cost))

    eq_raw = _pick(payload, _EQUITY_KEYS)
    equity = _as_float(eq_raw, "账户权益") if eq_raw is not None else None
    if equity is not None and equity < 0:
        raise ValueError(f"账户权益为负 ({equity})，快照可能有问题")

    as_of = str(payload.get("as_of") or payload.get("date") or "")
    return out, equity, as_of


# =============================================================================
# 比对 (纯函数，可离线测试)
# =============================================================================

def diff_positions(
    ledger: dict[str, Position],
    broker: list[BrokerPosition],
    ledger_equity: float | None = None,
    broker_equity: float | None = None,
    as_of: str = "",
    cost_tol: float = COST_TOL,
    equity_tol: float = EQUITY_TOL,
) -> ReconcileResult:
    """
    比对台账和券商持仓，返回差异。

    不修改任何输入 —— 修正动作由调用方在用户确认后执行。
    """
    res = ReconcileResult(broker_equity=broker_equity, ledger_equity=ledger_equity,
                          as_of=as_of)
    bmap = {b.symbol: b for b in broker}

    for sym in sorted(set(ledger) | set(bmap)):
        lpos, bpos = ledger.get(sym), bmap.get(sym)

        if bpos is not None and lpos is None:
            # 最危险的一种：你在券商持有它，但系统不知道 -> 永远不会给你卖出信号，
            # 也就是说这只票完全没有止损保护。
            cost = f"${bpos.avg_cost:.2f}" if bpos.avg_cost else "未知成本"
            res.issues.append(Discrepancy(
                kind="missing_in_ledger", symbol=sym, severity=CRITICAL,
                detail=(f"券商持有 {_fmt_shares(bpos.shares)} 股 ({cost})，台账里没有。"
                        f"系统不知道这只票 -> 不会给它任何止损或卖出信号。"),
                suggestion=(f"python record.py buy {sym} {int(bpos.shares)} "
                            f"{bpos.avg_cost:.2f}" if bpos.avg_cost and
                            float(bpos.shares).is_integer() else
                            f"python record.py buy {sym} <股数> <成交价>"),
            ))
            continue

        if lpos is not None and bpos is None:
            # 台账有、券商没有：你卖了忘了记。系统会一直用这个幽灵持仓算敞口，
            # 导致真实可用资金被高估、新仓位算错。
            res.issues.append(Discrepancy(
                kind="missing_at_broker", symbol=sym, severity=CRITICAL,
                detail=(f"台账记着 {lpos.shares} 股 (成本 ${lpos.entry_price:.2f})，"
                        f"券商已无此持仓。系统在用幽灵持仓计算敞口。"),
                suggestion=f"python record.py sell {sym} <实际成交价> --reason 对账补记",
            ))
            continue

        # 两边都有，比数量和成本
        clean = True

        if not float(bpos.shares).is_integer():
            # 台账的 shares 是 int，碎股存不进去。这不是对账能修的，得先说清楚。
            res.issues.append(Discrepancy(
                kind="fractional_shares", symbol=sym, severity=WARNING,
                detail=(f"券商持有 {bpos.shares} 股（碎股），台账只支持整数股 "
                        f"(当前记 {lpos.shares})。本系统按整股设计，碎股的风险敞口算不准。"),
                suggestion="",
            ))
            clean = False
        elif int(bpos.shares) != lpos.shares:
            res.issues.append(Discrepancy(
                kind="shares_mismatch", symbol=sym, severity=CRITICAL,
                detail=(f"股数不一致：台账 {lpos.shares}，券商 {int(bpos.shares)}。"
                        f"仓位规模算错会直接影响风险预算。"),
                suggestion=f"python record.py fix {sym} --shares {int(bpos.shares)}",
            ))
            clean = False

        if bpos.avg_cost is not None:
            drift = abs(bpos.avg_cost / lpos.entry_price - 1)
            if drift > cost_tol:
                res.issues.append(Discrepancy(
                    kind="cost_mismatch", symbol=sym, severity=WARNING,
                    detail=(f"成本价偏差 {drift:.2%}：台账 ${lpos.entry_price:.2f}，"
                            f"券商 ${bpos.avg_cost:.2f}。"
                            f"注意改成本价不会重算止损 —— 移动止损基于 peak_price，"
                            f"不基于入场价。"),
                    suggestion=f"python record.py fix {sym} --entry-price {bpos.avg_cost:.2f}",
                ))
                clean = False

        if clean:
            res.matched.append(sym)

    if broker_equity is not None and ledger_equity:
        drift = abs(broker_equity / ledger_equity - 1)
        if drift > equity_tol:
            res.issues.append(Discrepancy(
                kind="equity_drift", symbol="", severity=INFO,
                detail=(f"权益偏差 {drift:.1%}：配置里 ${ledger_equity:,.2f}，"
                        f"券商 ${broker_equity:,.2f}。仓位大小按权益算，偏差会放大到每一笔。"),
                suggestion=f"python run_daily.py --equity {broker_equity:.2f}",
            ))

    return res


def _fmt_shares(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"
