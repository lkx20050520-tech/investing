"""
数据获取与缓存
==============

用 yfinance 下载日线 OHLCV，缓存成 parquet。
缓存的意义：回测和参数扫描要反复读同一批数据，每次重新下载又慢又容易被限流。

⚠️ 关于 yfinance 的可靠性
--------------------------
yfinance 是非官方接口，Yahoo 随时可能改动或限流。它适合做研究和验证，
不适合作为真金白银交易系统的唯一数据源。如果这套系统你要长期跑，
考虑换成付费数据源 (Polygon.io / Tiingo / Alpaca Market Data 都有免费额度)。

数据质量检查 (本模块会自动做)：
  - 复权：使用 auto_adjust=True，价格已按拆股和分红复权
  - 缺失：连续缺口超过 5 个交易日的标的会被剔除并告警
  - 异常：单日涨跌超过 ±50% 会告警 (可能是未处理的拆股)
"""

from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from core.config import CACHE_DIR

warnings.filterwarnings("ignore")

CACHE_FILE = CACHE_DIR / "ohlcv.parquet"
META_FILE = CACHE_DIR / "meta.json"


# =============================================================================
# 下载
# =============================================================================

def _download(symbols: list[str], start: str, end: str | None) -> pd.DataFrame:
    """返回长表 (long format): [date, symbol, open, high, low, close, volume]"""
    import yfinance as yf

    print(f"[data] 下载 {len(symbols)} 只标的 ({start} 至今)...")
    raw = yf.download(
        symbols, start=start, end=end, auto_adjust=True,
        progress=False, group_by="ticker", threads=True,
    )
    if raw.empty:
        raise RuntimeError(
            "下载返回空数据。可能原因：网络被限制、Yahoo 限流、或标的代码全错。\n"
            "排查：单独跑 `python -c \"import yfinance as yf; "
            "print(yf.download('SPY', period='5d'))\"`"
        )

    frames = []
    for sym in symbols:
        try:
            df = raw[sym] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = df.dropna(subset=["Close"]).copy()
            if df.empty:
                continue
            df.columns = [c.lower() for c in df.columns]
            df["symbol"] = sym
            df = df.reset_index().rename(columns={"Date": "date", "index": "date"})
            frames.append(df[["date", "symbol", "open", "high", "low", "close", "volume"]])
        except Exception:
            continue

    if not frames:
        raise RuntimeError("所有标的都下载失败。")
    return pd.concat(frames, ignore_index=True)


# =============================================================================
# 质量检查
# =============================================================================

def _quality_check(long_df: pd.DataFrame, min_days: int = 300) -> tuple[pd.DataFrame, list[str]]:
    """剔除数据质量不合格的标的，返回 (清洗后数据, 被剔除的标的及原因)"""
    issues = []
    keep = []

    for sym, g in long_df.groupby("symbol"):
        g = g.sort_values("date")

        if len(g) < min_days:
            issues.append(f"{sym}: 历史不足 {len(g)} 天 (<{min_days})")
            continue

        # 检查异常跳空 (可能是未处理的拆股)
        ret = g["close"].pct_change()
        extreme = ret.abs() > 0.5
        if extreme.sum() > 0:
            dates = g.loc[extreme, "date"].dt.date.tolist()[:3]
            issues.append(f"{sym}: {extreme.sum()} 天涨跌超±50% (如 {dates})，疑似拆股未复权 —— 已保留但请人工核对")

        # 检查数据缺口
        gaps = g["date"].diff().dt.days
        big_gap = gaps > 10
        if big_gap.sum() > 0:
            issues.append(f"{sym}: 存在 {big_gap.sum()} 处 >10 天的数据缺口 —— 已剔除")
            continue

        # 检查非正价格
        if (g["close"] <= 0).any():
            issues.append(f"{sym}: 存在非正价格 —— 已剔除")
            continue

        keep.append(sym)

    return long_df[long_df["symbol"].isin(keep)].copy(), issues


# =============================================================================
# 对外接口
# =============================================================================

def load(symbols: list[str], start: str = "2015-01-01", end: str | None = None,
         use_cache: bool = True, max_age_hours: int = 12) -> dict[str, pd.DataFrame]:
    """
    加载数据，优先用缓存。返回 {symbol: DataFrame(index=date, cols=OHLCV)}

    max_age_hours: 缓存超过这个时长就重新下载。
                   默认 12 小时 —— 每天收盘后跑一次会自动刷新。
    """
    long_df = None

    if use_cache and CACHE_FILE.exists():
        age = datetime.now() - datetime.fromtimestamp(CACHE_FILE.stat().st_mtime)
        cached = pd.read_parquet(CACHE_FILE)
        cached_syms = set(cached["symbol"].unique())
        missing = set(symbols) - cached_syms

        if age < timedelta(hours=max_age_hours) and not missing:
            print(f"[data] 使用缓存 (更新于 {age.total_seconds()/3600:.1f} 小时前)")
            long_df = cached
        else:
            reason = "缓存过期" if age >= timedelta(hours=max_age_hours) else f"缺少 {len(missing)} 只新标的"
            print(f"[data] {reason}，重新下载")

    if long_df is None:
        long_df = _download(symbols, start, end)
        long_df, issues = _quality_check(long_df)
        if issues:
            print(f"[data] 质量检查发现 {len(issues)} 个问题：")
            for i in issues[:10]:
                print(f"       - {i}")
            if len(issues) > 10:
                print(f"       ... 另有 {len(issues)-10} 条")
        long_df.to_parquet(CACHE_FILE, index=False)
        print(f"[data] 已缓存至 {CACHE_FILE}")

    # 转成 {symbol: DataFrame}
    out = {}
    for sym, g in long_df.groupby("symbol"):
        if sym not in symbols:
            continue
        df = g.sort_values("date").set_index("date")[["open", "high", "low", "close", "volume"]]
        df.index = pd.to_datetime(df.index)
        out[sym] = df

    print(f"[data] 加载完成：{len(out)} 只标的，"
          f"{min(len(d) for d in out.values())}-{max(len(d) for d in out.values())} 个交易日")
    return out


def clear_cache() -> None:
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
        print(f"[data] 缓存已清除")
