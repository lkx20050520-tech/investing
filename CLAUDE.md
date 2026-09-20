# investing — 动量轮动信号系统

规则化的趋势跟踪信号生成器：本地生成买卖信号，用户在 Robinhood 手动执行，不接券商 API。详见 README.md。

## Setup

```bash
pip install -r requirements.txt
```

## Commands

- 逻辑验证 (合成数据，不需要网络，改代码后必跑): `python -m tests.test_logic`
- 真实数据回测: `python -m core.backtest --start 2016-01-01 --stress`
- 过拟合检验 (不能跳过): `python -m core.backtest --sweep`
- 生成今日信号: `python run_daily.py --equity <权益>`
- 记账: `python record.py buy|sell|list|stats|fix ...`

## Layout

```
core/config.py      全局参数，每个都带理由注释
core/universe.py    股票池 (含幸存者偏差说明)
core/data.py        yfinance 下载 + parquet 缓存 + 质量检查
core/signals.py     信号引擎，面板向量化，含前视偏差防护
core/portfolio.py   本地持仓台账 (手动执行模式的状态核心)
core/backtest.py    回测 + 统计 + 参数扫描 + 压力测试
core/report.py      终端报告 + HTML 存档
run_daily.py        每日入口
record.py           记账 CLI
tests/test_logic.py 56 项断言
```

## 改代码时必须遵守的约束

1. **改了 `core/` 下任何文件，必须重跑 `python -m tests.test_logic`**，56 项断言要全过。
2. **不要为了让回测好看而调参数**。参数的理由都写在 `core/config.py` 的注释里，改之前先读。改完必须重跑 `--sweep` 确认仍然平滑。
3. **前视偏差是最致命的 bug**。`core/signals.py` 里的 momentum 用 `shift(21)/shift(252)`，任何改动都不能破坏这一点。`tests/test_logic.py::test_no_lookahead` 专门验证：截断未来数据后，历史信号必须完全不变。
4. **不要接券商 API 下单**。用户明确选择手动执行；`core/portfolio.py` 的本地台账是状态的唯一来源。

## 已知的设计取舍（不是 bug，不用"修"）

| 现象 | 原因 |
|---|---|
| 胜率只有 ~34% | 趋势跟踪的固有形态，靠盈亏比 1.67 覆盖 |
| 大量时间空仓 | 择时过滤在起作用，RISK-OFF 时就该空仓 |
| 最大回撤可能是长期慢性失血 | 择时过滤防崩盘，防不住震荡市 |
| 需要手动记账 | 没有券商 API，台账是唯一的状态来源 |
| 股票池有幸存者偏差 | 免费数据拿不到历史成分股，已在 README 标注 |

## 已知的方法论缺口（真实存在，未来若有网络可用时应优先验证/修正）

- `core/backtest.py` 的调仓日买入在同一天以收盘价成交，未模拟"周五出信号、周一手动下单"的执行延迟；8bp 滑点假设可能不足以覆盖这个缺口。
- `core/backtest.py --stress` 的压力测试段是从空仓现金重新起跑的，没有延续此前已建立的持仓状态，可能低估动量崩塌类场景 (如 2020Q1) 的真实回撤。

## 环境限制说明

这套系统依赖 yfinance 拉取真实行情数据。在网络受限的沙箱环境 (包括本 Claude Code 云端会话) 里，Yahoo Finance 及其他行情数据源的出站请求会被拦截，`python -m core.backtest`、`run_daily.py` 等命令无法用真实数据跑通，只能跑 `tests.test_logic` 的合成数据验证。真实数据验证需要在有正常互联网访问的环境（如用户本机）执行。
