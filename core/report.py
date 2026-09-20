"""
每日信号报告
============

生成两份输出：
  1. 终端文本 —— 你每天看的
  2. HTML 文件 —— 存档，手机上也能打开对着操作

报告的设计原则：**能直接照着在 Robinhood 下单，不需要再算任何东西。**
每一条买入指令都包含：股数、限价参考、止损位、这笔最多亏多少钱。
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.config import REPORT_DIR


# =============================================================================
# 终端输出
# =============================================================================

def _bar(title: str, char: str = "=") -> str:
    return f"\n{char * 66}\n{title}\n{char * 66}"


def print_report(rep: dict) -> None:
    ctx = rep["market"]

    print(_bar(f"每日信号  {ctx['date']}"))

    # --- 市场状态 ---
    state = "🟢 RISK-ON 可建仓" if ctx["regime_on"] else "🔴 RISK-OFF 全部离场"
    print(f"\n市场状态      {state}")
    print(f"SPY           ${ctx['spy_price']}  (200日均线 ${ctx['spy_ma200']}, {ctx['spy_vs_ma']})")
    print(f"63日实现波动   {ctx['realized_vol_63d']}")
    print(f"目标总敞口     {ctx['target_exposure']}   ← 波动率缩放后的上限")
    print(f"合格候选池     {ctx['n_eligible']} 只")

    # --- 账户 ---
    a = rep["account"]
    print(f"\n账户权益      ${a['equity']:,.2f}")
    print(f"持仓市值      ${a['market_value']:,.2f}  ({a['n_positions']} 只)")
    print(f"可用现金      ${a['cash']:,.2f}")
    if a["n_positions"]:
        print(f"未实现盈亏     ${a['unrealized_pnl']:+,.2f} ({a['unrealized_pct']:+.1%})")

    # --- 卖出 ---
    print(_bar(f"🔴 卖出指令  ({len(rep['sells'])} 笔)  ← 优先执行，不要拖", "-"))
    if not rep["sells"]:
        print("\n无。所有持仓继续持有。")
    else:
        for s in rep["sells"]:
            print(f"\n  卖出 {s['shares']} 股 {s['symbol']}")
            print(f"    参考价    ${s['price']:.2f}")
            print(f"    原因      {s['reason']}")
            print(f"    成本      ${s['entry_price']:.2f}  →  盈亏 {s['pnl_pct']:+.1%} (${s['pnl']:+,.2f})")

    # --- 持有 ---
    if rep["holds"]:
        print(_bar(f"⚪ 继续持有  ({len(rep['holds'])} 笔)", "-"))
        print(f"\n  {'标的':<8}{'股数':>7}{'现价':>10}{'成本':>10}{'盈亏':>9}"
              f"{'止损位':>10}{'动量分':>8}")
        print("  " + "-" * 62)
        for h in rep["holds"]:
            print(f"  {h['symbol']:<8}{h['shares']:>7}{h['price']:>10.2f}"
                  f"{h['entry_price']:>10.2f}{h['pnl_pct']:>8.1%}"
                  f"{h['stop_price']:>10.2f}{h['mom_score']:>8}")
        if rep["stops_raised"]:
            print(f"\n  ↑ 止损已上移: {', '.join(rep['stops_raised'])}")
            print(f"    如果你在 Robinhood 挂了止损单，记得同步修改")

    # --- 买入 ---
    print(_bar(f"🟢 买入指令  ({len(rep['buys'])} 笔)", "-"))
    if not rep["buys"]:
        print(f"\n无。{rep['no_buy_reason']}")
    else:
        for b in rep["buys"]:
            print(f"\n  买入 {b['shares']} 股 {b['symbol']}   约 ${b['position_value']:,.2f}")
            print(f"    参考价    ${b['price']:.2f}")
            print(f"    止损位    ${b['stop_price']:.2f}   ← 下单后立刻在 Robinhood 挂 Stop Loss 单")
            print(f"    最大亏损   ${b['risk_amount']:,.2f}  (占权益 {b['risk_pct']:.2%})")
            print(f"    动量分 {b['mom_score']}  |  距52周高点 {b['pct_52w_high']}  "
                  f"|  扩张度 {b['extension']} ATR  |  量比 {b['vol_ratio']}")
            print(f"    仓位受限于: {b['binding_constraint']}")

    # --- 执行清单 ---
    print(_bar("执行清单", "-"))
    n = 1
    if rep["sells"]:
        print(f"\n  {n}. 在 Robinhood 卖出上面 {len(rep['sells'])} 笔")
        n += 1
        print(f"  {n}. 记账: " + "  ".join(
            f"python record.py sell {s['symbol']} <成交价>" for s in rep["sells"][:2]))
        n += 1
    if rep["buys"]:
        print(f"\n  {n}. 在 Robinhood 买入上面 {len(rep['buys'])} 笔")
        n += 1
        print(f"  {n}. 每笔买入后立刻挂止损单 (Stop Loss，价格见上)")
        n += 1
        print(f"  {n}. 记账: " + "  ".join(
            f"python record.py buy {b['symbol']} {b['shares']} <成交价>" for b in rep["buys"][:2]))
        n += 1
    if not rep["sells"] and not rep["buys"]:
        print(f"\n  今天没有操作。如果止损位有上移，去同步一下挂单即可。")

    if rep["warnings"]:
        print(_bar("⚠️  警告", "-"))
        for w in rep["warnings"]:
            print(f"  - {w}")

    print(f"\n报告已存档: {rep['html_path']}\n")


# =============================================================================
# HTML 输出
# =============================================================================

_CSS = """
:root{--bg:#ffffff;--fg:#1a1a1a;--muted:#6b7280;--line:#e5e7eb;--card:#f9fafb;
--buy:#047857;--sell:#b91c1c;--hold:#6b7280;--warn:#b45309;--on:#047857;--off:#b91c1c}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f1115;--fg:#e8e8ea;
--muted:#9ca3af;--line:#262a33;--card:#161a21;--buy:#34d399;--sell:#f87171;--hold:#9ca3af;
--warn:#fbbf24;--on:#34d399;--off:#f87171}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 4px}
.sub{color:var(--muted);font-size:.875rem;margin-bottom:20px}
.badge{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:600;font-size:.875rem}
.on{background:color-mix(in srgb,var(--on) 15%,transparent);color:var(--on)}
.off{background:color-mix(in srgb,var(--off) 15%,transparent);color:var(--off)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.stat .k{color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.04em}
.stat .v{font-size:1.25rem;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
h2{font-size:1.05rem;margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.order{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--line);
border-radius:8px;padding:12px 14px;margin-bottom:10px}
.order.b{border-left-color:var(--buy)}.order.s{border-left-color:var(--sell)}
.order .t{font-weight:600;font-size:1.05rem;margin-bottom:6px}
.order .t .sym{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.row{display:flex;justify-content:space-between;gap:12px;padding:3px 0;font-size:.875rem}
.row .l{color:var(--muted)}
.row .r{font-variant-numeric:tabular-nums;text-align:right}
table{width:100%;border-collapse:collapse;font-size:.875rem}
th,td{padding:8px 6px;text-align:right;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left;font-family:ui-monospace,Menlo,monospace}
th{color:var(--muted);font-weight:500;font-size:.75rem;text-transform:uppercase;letter-spacing:.04em}
.pos{color:var(--buy)}.neg{color:var(--sell)}
.empty{color:var(--muted);padding:14px;background:var(--card);border-radius:8px;font-size:.9rem}
.warn{background:color-mix(in srgb,var(--warn) 12%,transparent);border:1px solid var(--warn);
border-radius:8px;padding:12px;margin:16px 0;font-size:.875rem}
ol{padding-left:22px}li{margin:6px 0}
code{font-family:ui-monospace,Menlo,monospace;font-size:.85em;background:var(--card);
padding:2px 6px;border-radius:4px;border:1px solid var(--line)}
.foot{margin-top:32px;padding-top:16px;border-top:1px solid var(--line);
color:var(--muted);font-size:.8rem}
"""


def _esc(x) -> str:
    return html.escape(str(x))


def write_html(rep: dict) -> Path:
    ctx, a = rep["market"], rep["account"]
    on = ctx["regime_on"]

    def stat(k, v):
        return f'<div class="stat"><div class="k">{_esc(k)}</div><div class="v">{_esc(v)}</div></div>'

    def row(l, r, cls=""):
        return f'<div class="row"><span class="l">{_esc(l)}</span><span class="r {cls}">{_esc(r)}</span></div>'

    parts = [f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日信号 {_esc(ctx['date'])}</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>动量轮动 · 每日信号</h1>
<div class="sub">{_esc(ctx['date'])} 收盘 · 生成于 {datetime.now():%Y-%m-%d %H:%M}</div>
<div><span class="badge {'on' if on else 'off'}">
{'RISK-ON 可建仓' if on else 'RISK-OFF 全部离场'}</span></div>
<div class="grid">
{stat('SPY', f"${ctx['spy_price']}")}
{stat('vs 200日均线', ctx['spy_vs_ma'])}
{stat('目标总敞口', ctx['target_exposure'])}
{stat('账户权益', f"${a['equity']:,.0f}")}
{stat('持仓', f"{a['n_positions']} 只")}
{stat('可用现金', f"${a['cash']:,.0f}")}
</div>"""]

    # 卖出
    parts.append(f"<h2>卖出 · {len(rep['sells'])} 笔</h2>")
    if rep["sells"]:
        for s in rep["sells"]:
            cls = "pos" if s["pnl"] >= 0 else "neg"
            parts.append(f"""<div class="order s"><div class="t">
卖出 {s['shares']} 股 <span class="sym">{_esc(s['symbol'])}</span></div>
{row('参考价', f"${s['price']:.2f}")}
{row('原因', s['reason'])}
{row('成本价', f"${s['entry_price']:.2f}")}
{row('盈亏', f"{s['pnl_pct']:+.1%}  (${s['pnl']:+,.2f})", cls)}
</div>""")
    else:
        parts.append('<div class="empty">无卖出信号，所有持仓继续持有。</div>')

    # 持有
    if rep["holds"]:
        parts.append(f"<h2>持有 · {len(rep['holds'])} 笔</h2><table><thead><tr>"
                     "<th>标的</th><th>股数</th><th>现价</th><th>成本</th><th>盈亏</th>"
                     "<th>止损位</th><th>动量分</th></tr></thead><tbody>")
        for h in rep["holds"]:
            cls = "pos" if h["pnl_pct"] >= 0 else "neg"
            parts.append(f"<tr><td>{_esc(h['symbol'])}</td><td>{h['shares']}</td>"
                         f"<td>${h['price']:.2f}</td><td>${h['entry_price']:.2f}</td>"
                         f"<td class='{cls}'>{h['pnl_pct']:+.1%}</td>"
                         f"<td>${h['stop_price']:.2f}</td><td>{h['mom_score']}</td></tr>")
        parts.append("</tbody></table>")
        if rep["stops_raised"]:
            parts.append(f'<div class="warn">止损位已上移：<b>{_esc(", ".join(rep["stops_raised"]))}</b>'
                         f'。如果你在 Robinhood 挂了止损单，记得同步修改。</div>')

    # 买入
    parts.append(f"<h2>买入 · {len(rep['buys'])} 笔</h2>")
    if rep["buys"]:
        for b in rep["buys"]:
            parts.append(f"""<div class="order b"><div class="t">
买入 {b['shares']} 股 <span class="sym">{_esc(b['symbol'])}</span> · 约 ${b['position_value']:,.2f}</div>
{row('参考价', f"${b['price']:.2f}")}
{row('止损位 (下单后立刻挂)', f"${b['stop_price']:.2f}")}
{row('最大亏损', f"${b['risk_amount']:,.2f} (权益的 {b['risk_pct']:.2%})")}
{row('动量分', b['mom_score'])}
{row('距52周高点', b['pct_52w_high'])}
{row('扩张度', f"{b['extension']} ATR")}
{row('量比', b['vol_ratio'])}
{row('仓位受限于', b['binding_constraint'])}
</div>""")
    else:
        parts.append(f'<div class="empty">{_esc(rep["no_buy_reason"])}</div>')

    # 执行清单
    steps = []
    if rep["sells"]:
        steps.append(f"在 Robinhood 卖出上面 {len(rep['sells'])} 笔")
        steps.append("记账：<code>python record.py sell &lt;标的&gt; &lt;成交价&gt;</code>")
    if rep["buys"]:
        steps.append(f"在 Robinhood 买入上面 {len(rep['buys'])} 笔")
        steps.append("每笔买入后<b>立刻挂止损单</b>（Stop Loss，价格见上）")
        steps.append("记账：<code>python record.py buy &lt;标的&gt; &lt;股数&gt; &lt;成交价&gt;</code>")
    if not steps:
        steps.append("今天没有买卖操作。如果止损位有上移，去 Robinhood 同步挂单即可。")
    parts.append("<h2>执行清单</h2><ol>" + "".join(f"<li>{s}</li>" for s in steps) + "</ol>")

    if rep["warnings"]:
        parts.append('<div class="warn"><b>警告</b><ul>' +
                     "".join(f"<li>{_esc(w)}</li>" for w in rep["warnings"]) + "</ul></div>")

    parts.append("""<div class="foot">
本报告由规则系统自动生成，不构成投资建议。信号价格为前一交易日收盘价，
实际成交价会有差异。手动执行存在延迟，实际表现会低于回测。
</div></div></body></html>""")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"signal_{ctx['date']}.html"
    path.write_text("".join(parts), encoding="utf-8")
    return path
