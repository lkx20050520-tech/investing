"""
Telegram 推送
=============

收盘后跑完 `run_daily.py`，把报告推到手机上。完全可选：
不配置 Telegram 环境变量的话，`run_daily.py` 行为不变，什么都不会发。

配置
----
在项目根目录建一个 `.env` 文件 (已在 .gitignore 里，不会被提交)：

    TELEGRAM_BOT_TOKEN=123456:ABC-...
    TELEGRAM_CHAT_ID=123456789

拿到这两个值的方法见 README「自动化调度」一节。
"""

from __future__ import annotations

import os
from pathlib import Path

from core.config import ROOT

ENV_FILE = ROOT / ".env"


def _load_dotenv() -> None:
    """极简 .env 解析：KEY=VALUE，不覆盖已有环境变量。"""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _credentials() -> tuple[str, str] | None:
    _load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    return token, chat_id


def _summary_text(rep: dict) -> str:
    ctx, a = rep["market"], rep["account"]
    lines = [
        f"📅 每日信号 {ctx['date']}",
        f"{'🟢 RISK-ON' if ctx['regime_on'] else '🔴 RISK-OFF 全部离场'}  "
        f"SPY vs 200MA {ctx['spy_vs_ma'] or 'n/a'}",
        f"权益 ${a['equity']:,.0f}  持仓 {a['n_positions']} 只  可用现金 ${a['cash']:,.2f}",
        "",
        f"🔴 卖出 {len(rep['sells'])} 笔",
    ]
    for s in rep["sells"]:
        lines.append(f"  {s['symbol']} × {s['shares']} — {s['reason']}")
    lines.append(f"🟢 买入 {len(rep['buys'])} 笔")
    for b in rep["buys"]:
        lines.append(f"  {b['symbol']} × {b['shares']} @ ~${b['price']:.2f}  止损 ${b['stop_price']:.2f}")
    if not rep["buys"]:
        lines.append(f"  {rep['no_buy_reason']}")
    if rep["warnings"]:
        lines.append("")
        lines.append("⚠️ 警告")
        lines.extend(f"  {w}" for w in rep["warnings"])
    return "\n".join(lines)


def send_telegram_report(rep: dict) -> bool:
    """把报告摘要 + HTML 存档文件发到 Telegram。失败只打印警告，不抛异常——
    通知渠道挂了不该影响信号已经生成、持仓已经保存这个事实。"""
    creds = _credentials()
    if creds is None:
        return False
    token, chat_id = creds

    import requests

    base = f"https://api.telegram.org/bot{token}"
    try:
        requests.post(f"{base}/sendMessage", data={
            "chat_id": chat_id, "text": _summary_text(rep),
        }, timeout=15).raise_for_status()

        html_path = Path(rep["html_path"])
        with html_path.open("rb") as f:
            requests.post(f"{base}/sendDocument", data={"chat_id": chat_id},
                          files={"document": (html_path.name, f, "text/html")},
                          timeout=30).raise_for_status()
        print("[notify] 已推送到 Telegram")
        return True
    except Exception as e:
        print(f"[notify] Telegram 推送失败 (不影响信号和持仓记录): {e}")
        return False
