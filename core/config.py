"""
全局参数配置
============

所有可调参数集中在这里。改参数只改这个文件，不要散落到各处。

⚠️ 每个参数都有明确的理由。改之前先想清楚理由是什么，
   改之后必须重跑 `python -m core.backtest --sweep` 确认结果仍然平滑。
   如果某个参数只在一个特定值上表现好、邻近值就崩，那是过拟合。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
REPORT_DIR = ROOT / "reports"

for _d in (DATA_DIR, CACHE_DIR, REPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

POSITIONS_FILE = DATA_DIR / "positions.json"
TRADE_LOG_FILE = DATA_DIR / "trade_log.csv"


@dataclass(frozen=True)
class Config:
    # ========== 组合层 ==========
    equity: float = 10_000.0
    """账户总权益 (美元)。手动执行模式下这个值由你自己维护，
    每次充值/提现后改这里，或运行时用 --equity 覆盖。"""

    max_positions: int = 8
    """最大同时持仓数。
    太少 -> 单票风险过大；太多 -> 动量被稀释，且手动执行工作量爆炸。
    小账户 (<$10k) 建议降到 5，否则每笔仓位太小，手续费和滑点占比过高。"""

    # ========== 择时过滤 (系统中最重要的开关) ==========
    regime_symbol: str = "SPY"
    regime_ma: int = 200
    """SPY 跌破 200 日均线 -> 不开新仓 + 清空现有持仓。
    依据 Faber (2007)。这一条贡献了整个系统绝大部分的回撤控制能力。
    回测里去掉这条，最大回撤会从 -28% 恶化到 -50% 以上。"""

    regime_buffer: float = 0.0
    """缓冲带。0.02 表示需跌破均线 2% 才触发离场，减少假信号但反应更慢。
    默认 0 = 严格按均线。"""

    # ========== 选股信号 ==========
    mom_lookback: int = 252
    mom_skip: int = 21
    """12-1 动量：用过去 252 个交易日的收益，但跳过最近 21 日。
    跳过是为了避开短期反转效应 (Jegadeesh & Titman 1993)。
    不跳过的话你会系统性地买在短期高点上。"""

    min_mom_score: float = 90.0
    """动量分门槛 (0-100 的全市场百分位)。90 = 只买最强的 10%。"""

    min_pct_52w_high: float = 0.85
    """距 52 周高点不低于 85%。上方套牢盘少 (George & Hwang 2004)。"""

    require_ma_stack: bool = True
    """要求 价格 > EMA10 > EMA20 > EMA50，即长中短期资金同向。"""

    min_dollar_volume: float = 5e6
    """20 日平均成交额下限。流动性不够的票你进得去出不来。"""

    max_extension_atr: float = 3.0
    """扩张度上限：价格高出 EMA20 超过 3 个 ATR 就不买，等回调。
    这条是防止你在垂直拉升的末端接盘。"""

    # ========== 波动率缩放 (防动量崩塌) ==========
    vol_target: float = 0.15
    vol_lookback: int = 63
    max_exposure: float = 1.0
    """组合目标年化波动率 15%。市场实现波动率越高，总仓位自动越低。
    依据 Barroso & Santa-Clara (2015)。
    这是原始动量策略最致命缺陷 (momentum crash) 的解药：
    2009 年 3 月市场 V 型反转时，无保护的动量组合单月亏损约 40%。
    max_exposure=1.0 表示不用杠杆 (现金账户)。"""

    # ========== 风险管理 ==========
    risk_per_trade: float = 0.0075
    """单笔交易的风险预算 = 总权益的 0.75%。
    含义：如果这笔交易打到止损，你损失总资金的 0.75%。
    连续亏 10 笔 = -7.2%，这是可承受的。
    小账户可以提到 1%，但不要超过 1.5%。"""

    atr_period: int = 20
    stop_atr_mult: float = 2.5
    """初始止损 = 入场价 - 2.5 × ATR20。
    2.5 倍是给正常波动留的空间。设太窄会被日常噪音扫出去。"""

    trail_atr_mult: float = 3.5
    """移动止损 = 持仓期最高价 - 3.5 × ATR20。
    比初始止损宽，因为已经有浮盈了，要给趋势留呼吸空间。"""

    max_position_weight: float = 0.20
    """单票权重上限 20%。防止某只低波动股票按风险预算算出巨大仓位。"""

    # ========== 卖出规则 ==========
    exit_rank_score: float = 40.0
    """动量分跌破 40 -> 卖出。已经不是强势股了。"""

    use_ma50_exit: bool = True
    """收盘跌破 EMA50 -> 卖出。中期趋势破坏。"""

    # ========== 调仓节奏 ==========
    rebalance_weekday: int = 4
    """每周几生成买入信号。0=周一 ... 4=周五。
    默认周五收盘后算，下周一开盘执行。
    ⚠️ 卖出信号每天都检查，不受这个限制 —— 风控不能等。"""

    # ========== 交易成本 (仅回测用) ==========
    slippage_bps: float = 8.0
    """滑点 8 个基点 (0.08%)。
    ⚠️ 手动执行的实际滑点远大于此 —— 你看到信号到真正下单可能隔了几小时。
    回测结果要按这个折扣理解。绝对不要设成 0。"""

    commission_bps: float = 0.0
    """Robinhood 免佣。如果换券商记得改。"""


DEFAULT = Config()
