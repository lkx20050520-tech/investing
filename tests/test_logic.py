"""
逻辑验证 (合成数据，不需要网络)
================================

    python -m tests.test_logic

这些断言是防止你改坏系统的最后一道防线。
任何时候改了 core/ 下的代码，先跑这个。

重点验证：
  1. 前视偏差 —— 最致命的 bug，回测漂亮实盘亏钱的头号原因
  2. 择时过滤是否真的在熊市降低了仓位
  3. 风险约束是否被遵守 (无杠杆、持仓数上限、单笔风险)
  4. 移动止损只升不降
  5. 台账读写和盈亏计算
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backtest import Backtest, sweep
from core.config import Config
from core.portfolio import (Position, close_position, load_positions,
                            open_position, save_positions, update_trailing_stops)
from core.signals import SignalEngine, atr, ema, position_size

PASS, FAIL = "✓", "✗"
_results: list[tuple[bool, str]] = []


def check(cond: bool, msg: str) -> None:
    _results.append((bool(cond), msg))
    print(f"  {PASS if cond else FAIL} {msg}")


# =============================================================================
# 合成数据
# =============================================================================

def make_market(n_days: int = 2800, n_stock: int = 60, seed: int = 42):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2013-01-02", periods=n_days)

    # 市场因子：植入两段熊市，用于检验择时过滤。
    # 基础漂移取 0.0007/日，使合成市场长期年化约 +8%，接近美股长期股权风险溢价。
    # 这一点很重要：如果测试环境里市场 11 年累计是负的，
    # 那么"做多动量策略表现不好"就只是反映了环境设定，测不出系统本身的问题。
    drift = np.full(n_days, 0.0007)
    vol = np.full(n_days, 0.009)
    bear_windows = [(900, 1050), (1900, 2150)]
    crisis = np.zeros(n_days)          # 危机指示：1 = 处于熊市段
    for a, b in bear_windows:
        drift[a:b] = -0.0022
        vol[a:b] = 0.026
        crisis[a:b] = 1.0
    mkt = rng.normal(drift, vol)

    # 相关性聚集 (correlation clustering)：
    # 崩盘时个股相关性飙升至接近 1，"分散化"恰恰在最需要它的时候失效。
    # 这是 Ang & Chen (2002) 的经典实证发现，不加这一条的合成数据
    # 会系统性低估择时过滤的价值 —— 因为个股跌的时候大盘看着没事。
    beta_mult = 1.0 + 0.9 * crisis     # 熊市中 beta 放大到 1.9 倍
    trend_mult = 1.0 - 0.7 * crisis    # 熊市中个股自身趋势被压制

    def ohlcv(beta, alpha, idio):
        trend = alpha * np.sin(np.linspace(0, rng.uniform(1.5, 4.0) * np.pi, n_days)
                               + rng.uniform(0, 6))
        r = beta * beta_mult * mkt + trend * trend_mult + rng.normal(0, idio, n_days)
        c = 50 * np.exp(np.cumsum(r))
        h = c * (1 + np.abs(rng.normal(0, 0.008, n_days)))
        l = c * (1 - np.abs(rng.normal(0, 0.008, n_days)))
        v = rng.lognormal(15, 0.4, n_days)
        return pd.DataFrame({"open": c, "high": np.maximum(h, c),
                             "low": np.minimum(l, c), "close": c, "volume": v},
                            index=dates)

    data = {}
    spy = 200 * np.exp(np.cumsum(mkt))
    data["SPY"] = pd.DataFrame({"open": spy, "high": spy * 1.004, "low": spy * 0.996,
                                "close": spy, "volume": np.full(n_days, 8e7)}, index=dates)
    for i in range(n_stock):
        data[f"S{i:02d}"] = ohlcv(rng.uniform(0.6, 1.5), rng.uniform(0.0002, 0.0012),
                                  rng.uniform(0.012, 0.025))
    return data, dates, bear_windows


# =============================================================================
# 测试
# =============================================================================

def test_indicators():
    print("\n[1] 技术指标")
    s = pd.Series([10, 11, 12, 11, 13, 14, 13, 15], dtype=float)
    e = ema(s, 3)
    check(len(e) == len(s) and e.notna().all(), "EMA 输出长度和非空正确")
    check(abs(e.iloc[0] - 10.0) < 1e-9, "EMA 第一个值等于首个数据点")

    h = pd.Series([11, 12, 13, 12, 14], dtype=float)
    l = pd.Series([9, 10, 11, 10, 12], dtype=float)
    c = pd.Series([10, 11, 12, 11, 13], dtype=float)
    a = atr(h, l, c, 3)
    check((a.dropna() > 0).all(), "ATR 恒为正")


def test_no_lookahead(data):
    print("\n[2] 前视偏差 (最重要的一项)")
    cfg = Config()
    eng = SignalEngine(data, cfg)

    sym = "S00"
    raw = data[sym]["close"]
    manual = raw.shift(cfg.mom_skip) / raw.shift(cfg.mom_lookback) - 1
    got = eng.P["momentum"][sym]
    check(np.allclose(manual.dropna().values, got.dropna().values, atol=1e-10),
          "momentum 只用 t-21 及更早的数据，无未来信息")

    # 截断数据重算，结果必须与完整数据在同一天上完全一致
    cut = eng.dates[-200]
    truncated = {k: v.loc[:cut] for k, v in data.items()}
    eng2 = SignalEngine(truncated, cfg)
    a = eng.mom_score.loc[cut].dropna()
    b = eng2.mom_score.loc[cut].dropna()
    common = a.index.intersection(b.index)
    check(np.allclose(a[common].values, b[common].values, atol=1e-9),
          "截断未来数据后，历史某日的动量分完全不变")

    check(bool(eng.regime.loc[cut]) == bool(eng2.regime.loc[cut]),
          "截断未来数据后，历史某日的市场状态判定不变")

    # 52周高点不能包含未来
    pct52 = eng.P["pct52"][sym]
    check((pct52.dropna() <= 1.0 + 1e-9).all(), "距52周高点比值 ≤ 1 (未用未来最高价)")


def test_regime_filter(data, bear_windows, dates):
    print("\n[3] 择时过滤 (回撤控制的核心)")
    cfg = Config()
    eng = SignalEngine(data, cfg)
    bt = Backtest(cfg, eng).run(start="2015-01-02")

    for a, b in bear_windows:
        d0, d1 = dates[a], dates[b]
        seg = bt.detail.loc[d0:d1]
        spy = data["SPY"]["close"].loc[d0:d1]
        if len(seg) < 20:
            continue
        strat_ret = seg.equity.iloc[-1] / seg.equity.iloc[0] - 1
        spy_ret = spy.iloc[-1] / spy.iloc[0] - 1
        check(strat_ret > spy_ret,
              f"熊市段 {d0.date()}~{d1.date()}: 策略 {strat_ret:+.1%} 优于 SPY {spy_ret:+.1%}")
        check(seg.gross.mean() < 0.35,
              f"熊市段平均仓位 {seg.gross.mean():.0%} < 35% (过滤器生效)")

    # 关闭择时过滤应该明显更差。
    # 注意：关闭的正确方式是 regime_buffer=1.0 (判定变成 price > ma*0，恒为真)，
    # 而不是把 regime_ma 调小 —— 那样均线趋近价格本身，反而变成恒为假 = 永远空仓。
    no_filter = replace(cfg, regime_buffer=1.0)
    eng_nf = SignalEngine(data, no_filter)
    # 前 regime_ma 天均线还没算出来，这段是 NaN→False，属于正常热身期，不计入
    warm = eng_nf.regime.iloc[cfg.regime_ma:]
    check(warm.mean() > 0.99, "对照组的择时过滤确实已被关闭 (热身期后 RISK-ON 恒为真)")

    bt2 = Backtest(no_filter, eng_nf).run(start="2015-01-02")

    # 择时过滤的职责是"防崩盘"，所以要在崩盘窗口内比，而不是比全样本最大回撤。
    # 全样本最大回撤可能来自震荡市里的慢性失血 —— 那是过滤器的成本，不是它的失败。
    for a, b in bear_windows:
        d0, d1 = dates[a], dates[b]
        e1, e2 = bt.equity_curve.loc[d0:d1], bt2.equity_curve.loc[d0:d1]
        if len(e1) < 20 or len(e2) < 20:
            continue
        dd1 = (e1 / e1.cummax() - 1).min()
        dd2 = (e2 / e2.cummax() - 1).min()
        check(dd1 >= dd2 - 0.005,
              f"崩盘段 {d0.date()}~{d1.date()} 回撤: 有过滤 {dd1:.1%} 不劣于无过滤 {dd2:.1%}")


def test_regime_filter_cost(data, bear_windows, dates):
    """
    择时过滤不是免费的午餐 —— 这个测试把它的成本显式记录下来。

    过滤器让你在崩盘中活下来，代价是在震荡市里反复被扫出场、
    以及在市场刚反转时因为均线滞后而错过第一段涨幅。

    这不是 bug，是这类系统的固有性质。你必须提前知道，
    否则实盘经历一段横盘失血时会以为系统坏了而关掉它 —— 那通常正好在反转前夜。
    """
    print("\n[3b] 择时过滤的成本 (固有代价，非缺陷)")
    cfg = Config()
    bt = Backtest(cfg, SignalEngine(data, cfg)).run(start="2015-01-02")
    nf = replace(cfg, regime_buffer=1.0)
    bt2 = Backtest(nf, SignalEngine(data, nf)).run(start="2015-01-02")

    flat_ratio = (bt.detail.n_pos == 0).mean()
    check(flat_ratio > 0.15, f"有过滤时空仓天数占比 {flat_ratio:.0%} —— 大量时间在场外等待")

    eq = bt.equity_curve
    dd = eq / eq.cummax() - 1
    trough = dd.idxmin()
    peak = eq.loc[:trough].idxmax()
    days = (trough - peak).days
    print(f"      最大回撤 {dd.min():.1%} 历时 {days} 天 "
          f"({peak.date()} → {trough.date()})")
    print(f"      该段 RISK-ON 占比 {bt.detail.loc[peak:trough].regime.mean():.0%}，"
          f"平均仓位 {bt.detail.loc[peak:trough].gross.mean():.0%}")
    if days > 365:
        print(f"      ⚠️ 这是慢性失血型回撤，不是崩盘 —— 择时过滤防不住这个。")
        print(f"         实盘遇到时最容易动摇。记住：这是已知代价，不是系统失效。")

    check(len(bt.trades) < len(bt2.trades),
          f"有过滤时交易更少 ({len(bt.trades)} vs {len(bt2.trades)}) —— 过滤掉了熊市中的无效交易")


def test_risk_constraints(data):
    print("\n[4] 风险约束")
    cfg = Config()
    eng = SignalEngine(data, cfg)
    bt = Backtest(cfg, eng).run(start="2015-01-02")
    d = bt.detail

    check((d.equity > 0).all(), "净值始终为正 (无破产)")
    check(d.gross.max() <= 1.02, f"总敞口从未超过 100% (峰值 {d.gross.max():.1%}，现金账户无杠杆)")
    check(d.n_pos.max() <= cfg.max_positions,
          f"持仓数从未超过上限 (峰值 {int(d.n_pos.max())}/{cfg.max_positions})")
    check((d.cash >= -1e-6).all(), "现金从未为负")

    # 单笔仓位计算
    sz = position_size(equity=10_000, price=100.0, atr_val=2.0, cfg=cfg)
    expected_risk = 10_000 * cfg.risk_per_trade
    check(abs(sz["risk_amount"] - expected_risk) / expected_risk < 0.02,
          f"单笔风险 ${sz['risk_amount']:.0f} ≈ 风险预算 ${expected_risk:.0f}")
    check(sz["position_value"] <= 10_000 * cfg.max_position_weight + 1,
          f"单票市值 ${sz['position_value']:.0f} ≤ 权重上限 ${10_000*cfg.max_position_weight:.0f}")

    # 低波动股票应受权重约束而非风险约束
    sz2 = position_size(equity=10_000, price=100.0, atr_val=0.3, cfg=cfg)
    check(sz2["binding_constraint"] == "单票权重上限",
          "低波动标的的仓位受权重上限约束 (防止单票过重)")

    # 现金不足时受现金约束
    sz3 = position_size(equity=10_000, price=100.0, atr_val=2.0, cfg=cfg, available_cash=500)
    check(sz3["shares"] == 5, f"现金仅 $500 时只买 5 股 (实际 {sz3['shares']})")


def test_exit_logic(data):
    print("\n[5] 卖出逻辑")
    cfg = Config()
    eng = SignalEngine(data, cfg)
    bt = Backtest(cfg, eng).run(start="2015-01-02")
    t = bt.trades

    check(len(t) > 50, f"产生了足够多的交易供统计 ({len(t)} 笔)")
    reasons = set(t.reason.unique())
    check("TRAILING_STOP" in reasons, "移动止损被触发过")
    check("REGIME_OFF" in reasons, "大盘转熊的系统性清仓被触发过")

    wins = t[t.pnl > 0]
    losses = t[t.pnl <= 0]
    payoff = abs(wins.pnl_pct.mean() / losses.pnl_pct.mean())
    check(payoff > 1.2,
          f"盈亏比 {payoff:.2f} > 1.2 (趋势跟踪的必要特征：赢大亏小)")
    check(len(wins) / len(t) < 0.55,
          f"胜率 {len(wins)/len(t):.1%} < 55% (趋势跟踪本就多数交易是亏的)")

    # 单笔亏损不应远超风险预算 (允许跳空击穿)
    worst_pct = t.pnl_pct.min()
    check(worst_pct > -0.40, f"最差单笔 {worst_pct:.1%} 未失控 (>-40%)")


def test_trailing_stop():
    print("\n[6] 移动止损")
    pos = {"X": Position(symbol="X", shares=100, entry_price=100.0,
                         entry_date="2024-01-01", stop_price=95.0,
                         peak_price=100.0, initial_stop=95.0)}

    update_trailing_stops(pos, {"X": 110.0}, {"X": 2.0}, trail_mult=3.5)
    check(pos["X"].peak_price == 110.0, "价格新高后 peak 被更新")
    check(pos["X"].stop_price == 103.0, f"止损上移至 110-3.5×2=103 (实际 {pos['X'].stop_price})")

    update_trailing_stops(pos, {"X": 105.0}, {"X": 2.0}, trail_mult=3.5)
    check(pos["X"].stop_price == 103.0, "价格回落时止损不下移 (只升不降)")
    check(pos["X"].peak_price == 110.0, "价格回落时 peak 保持不变")

    update_trailing_stops(pos, {"X": float("nan")}, {"X": 2.0}, trail_mult=3.5)
    check(pos["X"].stop_price == 103.0, "价格为 NaN 时安全跳过，不破坏状态")


def test_portfolio_ledger():
    print("\n[7] 持仓台账")
    with tempfile.TemporaryDirectory() as td:
        pfile = Path(td) / "positions.json"
        tlog = Path(td) / "trades.csv"

        pos: dict[str, Position] = {}
        open_position(pos, "AAPL", 10, 190.0, 180.0, on_date="2024-01-02")
        save_positions(pos, pfile)

        loaded = load_positions(pfile)
        check(len(loaded) == 1 and loaded["AAPL"].shares == 10, "台账写入后能正确读回")
        check(loaded["AAPL"].peak_price == 190.0, "新开仓的 peak 初始化为入场价")

        try:
            open_position(loaded, "AAPL", 5, 195.0, 185.0)
            check(False, "重复开仓应当报错")
        except ValueError:
            check(True, "重复开仓被正确拒绝 (本系统不加仓)")

        rec = close_position(loaded, "AAPL", 210.0, "测试", on_date="2024-02-01",
                             log_path=tlog)
        check(abs(rec["pnl"] - 200.0) < 1e-6, f"盈亏计算正确: (210-190)×10=200 (实际 {rec['pnl']})")
        check(abs(rec["pnl_pct"] - 0.10526) < 1e-4, "盈亏百分比计算正确")
        check(rec["holding_days"] == 30, f"持仓天数正确 (实际 {rec['holding_days']})")
        check(len(loaded) == 0, "平仓后持仓被移除")
        check(tlog.exists(), "交易流水已落盘")

        df = pd.read_csv(tlog)
        check(len(df) == 1 and df.iloc[0]["symbol"] == "AAPL", "流水内容正确")


def test_param_sensitivity(data):
    print("\n[8] 参数敏感性 (过拟合检测)")
    cfg = Config()
    res = sweep(data, cfg, "2015-01-02", "regime_ma", [100, 150, 200, 250])
    dds = [float(x.strip("%")) for x in res["MaxDD"]]
    check(len(res) == 4, "参数扫描正常运行")
    check(max(dds) - min(dds) < 40,
          f"regime_ma 在 100-250 之间回撤差异 {max(dds)-min(dds):.0f}pp，未出现悬崖式突变")
    print("      " + res.to_string(index=False).replace("\n", "\n      "))


def test_report_shape(data):
    print("\n[9] 报告生成")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from run_daily import build_report

    cfg = Config(equity=10_000)
    eng = SignalEngine(data, cfg)

    pos: dict[str, Position] = {}
    d = eng.dates[-1]
    cands = eng.candidates(d)
    if len(cands):
        r = cands.iloc[0]
        open_position(pos, r["symbol"], 10, float(r["close"]),
                      float(r["close"]) * 0.9, on_date="2023-01-01")

    rep = build_report(eng, pos, cfg, force_buy_check=True)
    for k in ("market", "account", "sells", "holds", "buys", "warnings", "html_path"):
        check(k in rep, f"报告包含 '{k}' 字段")
    check(Path(rep["html_path"]).exists(), "HTML 报告文件已生成")
    check(len(rep["sells"]) + len(rep["holds"]) == len(pos), "买卖分类覆盖全部持仓")
    for b in rep["buys"]:
        check(b["risk_pct"] <= cfg.risk_per_trade * 1.05,
              f"买入 {b['symbol']} 风险 {b['risk_pct']:.2%} 未超预算")
        break


# =============================================================================
def main() -> int:
    print("=" * 66)
    print("逻辑验证 (合成数据)")
    print("=" * 66)

    data, dates, bears = make_market()
    print(f"\n合成 {len(data)} 只标的 × {len(dates)} 个交易日，含 {len(bears)} 段熊市")

    test_indicators()
    test_no_lookahead(data)
    test_regime_filter(data, bears, dates)
    test_regime_filter_cost(data, bears, dates)
    test_risk_constraints(data)
    test_exit_logic(data)
    test_trailing_stop()
    test_portfolio_ledger()
    test_param_sensitivity(data)
    test_report_shape(data)

    n_pass = sum(1 for ok, _ in _results if ok)
    n_fail = len(_results) - n_pass
    print("\n" + "=" * 66)
    print(f"结果: {n_pass} 通过, {n_fail} 失败")
    print("=" * 66)
    if n_fail:
        print("\n失败项:")
        for ok, msg in _results:
            if not ok:
                print(f"  {FAIL} {msg}")
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
