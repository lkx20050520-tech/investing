"""
股票池 (Universe)
=================

⚠️ 幸存者偏差警告
------------------
下面的列表是"今天"的 S&P 500 成分股。用它回测 2015-2026 会系统性高估收益，
因为这些公司是活到今天的赢家 —— 当年倒闭、被收购、被剔除的公司不在里面。

学术上正确的做法是使用历史成分股 (point-in-time universe)，
但免费数据源基本拿不到。可选的缓解方案：
  1. 回测结果自觉打折看待 (CAGR 至少减 2-3%)
  2. 付费数据源 (Norgate Data, CRSP) 提供 point-in-time universe
  3. 用 ETF 池代替个股池 (ETF 没有退市问题，见 ETF_UNIVERSE)

对于"验证系统是否可行"这个目的，当前列表够用。
但别拿回测出来的 CAGR 当作对未来的预期。
"""

from __future__ import annotations


def fetch_sp500_from_wikipedia() -> list[str] | None:
    """从 Wikipedia 抓当前 S&P 500 成分股。失败返回 None，调用方应回退到静态列表。"""
    try:
        import pandas as pd
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url)
        syms = tables[0]["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
        return [s.strip().upper() for s in syms if s and s != "nan"]
    except Exception as e:
        print(f"[universe] Wikipedia 抓取失败 ({e})，回退到静态列表")
        return None


# ---------------------------------------------------------------------------
# 静态回退列表：大盘股为主，覆盖主要行业。约 120 只。
# 对小账户 (<$25k) 来说这个规模完全够用 —— 你最多也就持 5-8 只。
# ---------------------------------------------------------------------------
STATIC_UNIVERSE: list[str] = [
    # --- 科技 / 半导体 ---
    "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "MU", "INTC", "TSM", "ARM",
    "LRCX", "AMAT", "KLAC", "ADI", "TXN", "QCOM", "MRVL", "NXPI", "ON",
    "SNPS", "CDNS", "ANET", "SMCI",
    # --- 互联网 / 软件 ---
    "GOOGL", "META", "AMZN", "NFLX", "CRM", "ORCL", "ADBE", "NOW", "INTU",
    "PANW", "CRWD", "SNOW", "DDOG", "MDB", "TEAM", "WDAY", "UBER", "ABNB",
    "SHOP", "SQ", "PYPL", "SPOT",
    # --- 金融 ---
    "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "BLK", "SPGI", "ICE",
    "V", "MA", "AXP", "COF", "PGR", "CB", "AON", "MMC",
    # --- 能源 ---
    "XOM", "CVX", "COP", "EOG", "SLB", "PSX", "VLO", "MPC", "OXY", "WMB",
    "KMI", "OKE",
    # --- 医疗 ---
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "AMGN",
    "ISRG", "VRTX", "REGN", "GILD", "BSX", "SYK", "MDT", "ELV", "CI", "MCK",
    # --- 消费 ---
    "COST", "WMT", "HD", "LOW", "TGT", "MCD", "SBUX", "NKE", "TJX", "BKNG",
    "CMG", "PG", "KO", "PEP", "PM", "MDLZ", "CL", "MNST",
    # --- 工业 / 国防 ---
    "CAT", "DE", "GE", "HON", "UNP", "UPS", "LMT", "RTX", "NOC", "GD",
    "ETN", "EMR", "PH", "ITW", "CSX", "NSC", "WM", "PCAR",
    # --- 其他 ---
    "TSLA", "DIS", "T", "VZ", "TMUS", "CMCSA", "LIN", "APD", "SHW", "NEM",
]


# ---------------------------------------------------------------------------
# ETF 池：没有退市/幸存者偏差问题，适合做对照回测。
# 用行业 ETF 跑动量轮动是更保守、更容易复现的版本。
# ---------------------------------------------------------------------------
ETF_UNIVERSE: list[str] = [
    "XLK",  # 科技
    "XLF",  # 金融
    "XLE",  # 能源
    "XLV",  # 医疗
    "XLI",  # 工业
    "XLY",  # 可选消费
    "XLP",  # 必需消费
    "XLU",  # 公用事业
    "XLB",  # 材料
    "XLRE", # 房地产
    "XLC",  # 通信
    "SMH",  # 半导体
    "IBB",  # 生物科技
    "ITA",  # 航空国防
    "XBI",  # 生物科技 (等权)
    "KRE",  # 区域银行
    "XOP",  # 油气勘探
    "GDX",  # 金矿
    "IYT",  # 运输
    "IGV",  # 软件
]


def get_universe(kind: str = "stocks", live_fetch: bool = True) -> list[str]:
    """
    kind: "stocks" (个股) | "etf" (行业ETF) | "both"
    live_fetch: 是否尝试从 Wikipedia 抓最新 S&P 500
    """
    from core.config import DEFAULT

    if kind == "etf":
        syms = list(ETF_UNIVERSE)
    elif kind == "both":
        syms = list(dict.fromkeys(STATIC_UNIVERSE + ETF_UNIVERSE))
    else:
        syms = None
        if live_fetch:
            syms = fetch_sp500_from_wikipedia()
        if not syms:
            syms = list(STATIC_UNIVERSE)

    # 基准必须在池子里 (用于择时过滤)，但不参与选股
    if DEFAULT.regime_symbol not in syms:
        syms = [DEFAULT.regime_symbol] + syms
    return list(dict.fromkeys(syms))
