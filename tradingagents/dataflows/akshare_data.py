"""AKShare-backed data vendor for China A-share market.

Function signatures mirror the existing yfinance / Alpha Vantage adapters so the
vendor-routing layer (``dataflows/interface.py``) can swap them in by key.
All public functions return **strings** (CSV with headers or prose), matching
the convention that agents consume them as tool-call text.

AKShare endpoints used (all free, no API key):
- stock_zh_a_hist          — daily OHLCV
- stock_zh_a_hist_min_em   — minute-level OHLCV (EastMoney)
- stock_zh_a_spot_em       — full A-share real-time snapshot
- stock_individual_info_em — company profile (name, sector, listing date)
- stock_financial_abstract_ths — quarterly financial summary (THS source)
- stock_news_em            — per-ticker news (EastMoney)
- stock_global_spot_em     — global index real-time (for macro context)
- stock_board_industry_name_em — sector board list
- stock_hsgt_north_net_flow_in_em — northbound capital flow
- stock_margin_detail_sse  / szse — margin trading (融资融券) detail
- stock_lhb_detail_em      — dragon-tiger list (龙虎榜)
- macro_china_cpi_monthly   — CPI
- macro_china_ppi_monthly   — PPI
- macro_china_pmi_yearly    — PMI (manufacturing)
- macro_china_money_supply  — M0/M1/M2
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

import pandas as pd

from .ashare_symbol_utils import normalize_ashare_symbol, parse_ashare_symbol, resolve_name
from .errors import NoMarketDataError, VendorNotConfiguredError

logger = logging.getLogger(__name__)

# ── helpers ──────────────────────────────────────────────────────────────────


def _resolve_code(symbol: str) -> str:
    """Resolve a user-supplied ticker to a bare six-digit A-share code.

    Accepts:
    - bare code ``600519``
    - suffixed ``600519.SH``
    - Chinese name ``贵州茅台``
    Raises ``NoMarketDataError`` when resolution fails.
    """
    code = normalize_ashare_symbol(symbol)
    if code:
        return code
    # try Chinese name
    if _is_plain_text(symbol) and len(symbol) > 1:
        code = resolve_name(symbol.strip())
        if code:
            return code
    raise NoMarketDataError(
        symbol, symbol,
        f"cannot resolve '{symbol}' to an A-share code (try 600519.SH or 贵州茅台)",
    )


def _is_plain_text(s: str) -> bool:
    from .ashare_symbol_utils import is_ashare_name
    return is_ashare_name(s) if isinstance(s, str) else False


def _csv_header(title: str, extra: str = "") -> str:
    return (
        f"# {title}\n"
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{extra}\n"
    )


def _df_to_csv(df: pd.DataFrame, title: str, extra: str = "") -> str:
    if df.empty:
        raise NoMarketDataError(title, title, "empty result from AKShare")
    csv_string = df.to_csv()
    return _csv_header(title, extra) + csv_string


def _import_ak():
    """Lazy import AKShare with a clear error when it is missing."""
    try:
        import akshare as ak
        return ak
    except ImportError:
        raise VendorNotConfiguredError(
            "AKShare is not installed. Run: pip install akshare"
        )


# ── stock_news_em 兼容层 ─────────────────────────────────────────────────────
#
# akshare ≤1.18.83 的 stock_news_em 末尾用 str.replace(r"\u3000", regex=True)
# 清理全角空格；pandas ≥2.x 且装有 pyarrow 时该 regex 走 pyarrow 的 RE2 引擎，
# RE2 不认 \u 转义 → ArrowInvalid: invalid escape sequence: \u。
# 这里先试原生接口（akshare 修复后自动恢复），失败则用同一 HTTP 端点的
# 自实现兜底：标签清理全部用字面量替换（regex=False），不再触发该 bug。

_EM_NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
_EM_NEWS_HEADERS = {
    "accept": "*/*",
    "accept-language": "en,zh-CN;q=0.9,zh;q=0.8",
    "referer": "https://so.eastmoney.com/news/s",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    ),
}


def _clean_em_news_text(df: pd.DataFrame) -> pd.DataFrame:
    """按字面量清理东财搜索结果里的 <em> 高亮标签与全角空白（不用 regex）。"""
    for col in ("新闻标题", "新闻内容"):
        if col not in df.columns:
            continue
        s = df[col].astype(str)
        for literal in ("(<em>", "</em>)", "<em>", "</em>", "\u3000", "\r\n"):
            s = s.str.replace(literal, "", regex=False)
        df[col] = s
    return df


def _stock_news_em_fallback(keyword: str) -> pd.DataFrame:
    """直接请求东财搜索 API（与 akshare 同端点），返回同构 DataFrame。"""
    import json as _json

    import requests

    inner_param = {
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": 10,
                "preTag": "<em>",
                "postTag": "</em>",
            }
        },
    }
    params = {
        "cb": "jQuery_callback",
        "param": _json.dumps(inner_param, ensure_ascii=False),
        "_": str(int(datetime.now().timestamp() * 1000)),
    }
    resp = requests.get(
        _EM_NEWS_URL, params=params, headers=_EM_NEWS_HEADERS, timeout=8,
    )
    resp.raise_for_status()
    text = resp.text
    # JSONP 包裹：取第一个 '(' 与最后一个 ')' 之间的 JSON 体
    start, end = text.find("("), text.rfind(")")
    if start < 0 or end <= start:
        raise ValueError("unexpected EastMoney search payload shape")
    payload = _json.loads(text[start + 1:end])
    rows = payload.get("result", {}).get("cmsArticleWebOld") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["url"] = "http://finance.eastmoney.com/a/" + df["code"].astype(str) + ".html"
    df = df.rename(columns={
        "date": "发布时间", "mediaName": "文章来源", "code": "-",
        "title": "新闻标题", "content": "新闻内容", "url": "新闻链接",
        "image": "-",
    })
    df["关键词"] = keyword
    keep = [c for c in ("关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接") if c in df.columns]
    return _clean_em_news_text(df[keep])


def _stock_news_em_safe(keyword: str) -> pd.DataFrame:
    """stock_news_em 兼容层：原生优先，ArrowInvalid 等兼容性错误时走兜底。"""
    ak = _import_ak()
    try:
        return ak.stock_news_em(symbol=keyword)
    except Exception as exc:
        # 兼容性 bug（pyarrow/regex）与瞬时错误都值得走一次兜底请求
        logger.info("stock_news_em native failed (%s) — using built-in fallback", exc)
        return _stock_news_em_fallback(keyword)


# ── 新浪个股新闻（东财搜索端点失效时的二级兜底） ─────────────────────────────
#
# 东财 search-api 的 cmsArticleWebOld 节点自 2026-08 起对全部关键词返回空
# （端点实质失效，akshare 尚未跟进）。新浪个股新闻页稳定且含日期 URL：
# https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/sh600519.phtml

_SINA_NEWS_ITEM_RE = None  # 惰性编译


def _fetch_sina_stock_news(code: str) -> pd.DataFrame:
    """抓取新浪个股新闻页，返回与东财 stock_news_em 同构的 DataFrame。"""
    import re as _re

    import requests

    from .quote_sources import exchange_prefix

    prefix = exchange_prefix(code)
    if prefix is None:
        return pd.DataFrame()
    url = (
        "https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock"
        f"/symbol/{prefix}{code}.phtml"
    )
    headers = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        ),
        "referer": "https://finance.sina.com.cn/",
    }
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    resp.encoding = "gbk"

    global _SINA_NEWS_ITEM_RE
    if _SINA_NEWS_ITEM_RE is None:
        # URL 自带发布日期（…/2026-08-19/doc-xxxx.shtml），标题在 <a> 文本里
        _SINA_NEWS_ITEM_RE = _re.compile(
            r"href=['\"](https?://finance\.sina\.com\.cn/[^'\"]*?/(\d{4}-\d{2}-\d{2})/doc-[^'\"]+\.shtml)['\"][^>]*>([^<]{6,200})</a>"
        )
    rows = []
    for link, day, title in _SINA_NEWS_ITEM_RE.findall(resp.text):
        rows.append({
            "关键词": code,
            "新闻标题": title.strip(),
            "新闻内容": "",           # 摘要需逐篇抓取，标题已够分析用
            "发布时间": day,
            "文章来源": "新浪财经",
            "新闻链接": link,
        })
    return pd.DataFrame(rows)


# ── core_stock_apis ──────────────────────────────────────────────────────────


def get_stock_data_akshare(
    symbol: Annotated[str, "A-share code (600519) or Chinese name (贵州茅台)"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Retrieve daily OHLCV data for an A-share stock via AKShare (EastMoney source)."""
    ak = _import_ak()
    code = _resolve_code(symbol)

    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="qfq",  # 前复权
        )
    except Exception as exc:
        raise NoMarketDataError(symbol, code, f"stock_zh_a_hist failed: {exc}") from exc

    if df is None or df.empty:
        raise NoMarketDataError(
            symbol, code,
            f"no daily OHLCV between {start_date} and {end_date}",
        )

    # AKShare column names are Chinese; standardise to English.
    col_map = {
        "日期": "Date", "开盘": "Open", "收盘": "Close",
        "最高": "High", "最低": "Low", "成交量": "Volume",
        "成交额": "Amount", "振幅": "Amplitude", "涨跌幅": "ChangePct",
        "涨跌额": "ChangeAmt", "换手率": "TurnoverRate",
    }
    df.rename(columns=col_map, inplace=True)

    label = code if code == symbol.strip() else f"{code} (from {symbol})"
    return _csv_header(f"Stock data for {label} from {start_date} to {end_date}") + df.to_csv()


# ── technical_indicators ──────────────────────────────────────────────────────

# stockstats-based indicator computation, identical logic to the yfinance path
# but fed from AKShare OHLCV data instead.

_IND_PARAMS = {
    "close_50_sma": (
        "50 SMA: 中期趋势指标。用途：判断趋势方向、动态支撑/阻力位。"
        "提示：滞后性较强，需配合快速指标使用。"
    ),
    "close_200_sma": (
        "200 SMA: 长期趋势基准。用途：确认整体趋势，判断金叉/死叉。"
        "提示：反应慢，适合战略趋势确认而非频繁交易。"
    ),
    "close_10_ema": (
        "10 EMA: 短期指数均线。用途：捕捉动量快速变化和潜在入场点。"
        "提示：在震荡市中噪声大，需配合长周期均线。"
    ),
    "macd": (
        "MACD: 通过EMA差值计算动量。用途：观察金叉/死叉和背离信号。"
        "提示：低波动率市场需配合其他指标确认。"
    ),
    "macds": (
        "MACD Signal: MACD线的EMA平滑。用途：与MACD线交叉作为交易信号。"
        "提示：应纳入更广泛的策略避免误判。"
    ),
    "macdh": (
        "MACD Histogram: MACD线与信号线的差值。用途：可视化动量强弱和早期背离。"
        "提示：波动较大，需配合其他过滤器。"
    ),
    "rsi": (
        "RSI: 衡量动量，标记超买/超卖。用途：70/30阈值+背离信号。"
        "提示：强趋势中RSI可能持续极端值，需结合趋势分析。"
    ),
    "boll": (
        "Bollinger Middle: 20日SMA布林带中轨。用途：价格运动动态基准。"
        "提示：结合上下轨判断突破或反转。"
    ),
    "boll_ub": (
        "Bollinger Upper Band: 2倍标准差上轨。用途：超买区域和突破信号。"
        "提示：强趋势中价格可能沿上轨运行。"
    ),
    "boll_lb": (
        "Bollinger Lower Band: 2倍标准差下轨。用途：超卖区域。"
        "提示：需附加分析避免虚假反转信号。"
    ),
    "atr": (
        "ATR: 平均真实波幅，衡量波动率。用途：设定止损位、调整仓位。"
        "提示：是反应性指标，应纳入更广的风控策略。"
    ),
    "vwma": (
        "VWMA: 成交量加权移动平均。用途：整合价格与成交量确认趋势。"
        "提示：成交量突变可能导致偏差，需结合其他成交量分析。"
    ),
    "mfi": (
        "MFI: 资金流量指数，结合价格与成交量的动量指标。"
        "用途：超买(>80)/超卖(<20)条件，确认趋势或反转强度。"
        "提示：配合RSI或MACD确认信号；价格与MFI背离提示潜在反转。"
    ),
    "kdj_k": (
        "KDJ K值: 随机指标K线。用途：超买(>80)/超卖(<20)，判断短期转折。"
        "提示：A股常用指标，需配合成交量确认。"
    ),
    "kdj_d": (
        "KDJ D值: K值的移动平均。用途：K/D交叉作为交易信号。"
        "提示：金叉(底部)/死叉(顶部)信号在A股中较有效。"
    ),
    "kdj_j": (
        "KDJ J值: 3K-2D，更敏感。用途：领先于K/D的转折信号。"
        "提示：极端值(<0或>100)往往是转折点。"
    ),
}


def get_indicators_akshare(
    symbol: Annotated[str, "A-share code or Chinese name"],
    indicator: Annotated[str, "technical indicator name"],
    curr_date: Annotated[str, "current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Compute a technical indicator for an A-share stock over a look-back window."""
    from stockstats import wrap

    if indicator not in _IND_PARAMS:
        raise ValueError(
            f"Indicator '{indicator}' not supported. "
            f"Choose from: {list(_IND_PARAMS.keys())}"
        )

    ak = _import_ak()
    code = _resolve_code(symbol)
    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = end_dt - pd.Timedelta(days=look_back_days + 30)  # extra buffer for indicator warmup

    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start_dt.strftime("%Y%m%d"),
            end_date=end_dt.strftime("%Y%m%d"),
            adjust="qfq",
        )
    except Exception as exc:
        raise NoMarketDataError(symbol, code, f"indicator data fetch failed: {exc}") from exc

    if df is None or df.empty:
        raise NoMarketDataError(symbol, code, "no data for indicator calculation")

    # Rename Chinese columns to English for stockstats.
    col_map = {"日期": "date", "开盘": "open", "收盘": "close",
               "最高": "high", "最低": "low", "成交量": "volume"}
    df.rename(columns=col_map, inplace=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    # stockstats needs Date column as well
    df["Date"] = df.index

    wrapped = wrap(df)
    _ = wrapped[indicator]  # trigger lazy calculation

    cutoff = end_dt - pd.Timedelta(days=look_back_days)
    wrapped = wrapped[wrapped.index >= cutoff]

    lines = []
    for dt, row in wrapped.iterrows():
        val = row[indicator]
        lines.append(f"{dt.strftime('%Y-%m-%d')}: {val if pd.notna(val) else 'N/A'}")

    title = f"{indicator} values from {cutoff.strftime('%Y-%m-%d')} to {curr_date}"
    return f"## {title}:\n\n" + "\n".join(lines) + f"\n\n{_IND_PARAMS.get(indicator, '')}"


# ── fundamental_data ────────────────────────────────────────────────────────


def get_fundamentals_akshare(
    ticker: Annotated[str, "A-share code or Chinese name"],
    curr_date: Annotated[str, "not used; AKShare returns latest"] = None,
) -> str:
    """Get A-share company fundamentals via AKShare (EastMoney + THS)."""
    ak = _import_ak()
    code = _resolve_code(ticker)
    parsed = parse_ashare_symbol(code)
    lines = []

    # ── company profile ──
    try:
        info = ak.stock_individual_info_em(symbol=code)
        if info is not None and not info.empty:
            label = code if code == ticker.strip() else f"{code} (from {ticker})"
            lines.append(f"# Company Fundamentals for {label}")
            lines.append(f"# Board: {_board_label(parsed)}")
            for _, row in info.iterrows():
                lines.append(f"{row.get('item', '')}: {row.get('value', '')}")
    except Exception as exc:
        logger.warning("stock_individual_info_em failed for %s: %s", code, exc)

    if lines:
        return "\n".join(lines)

    raise NoMarketDataError(ticker, code, "no fundamentals returned")


def get_balance_sheet_akshare(
    ticker: Annotated[str, "A-share code or Chinese name"],
    freq: Annotated[str, "'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "not used"] = None,
) -> str:
    """Get A-share balance sheet via AKShare (THS source)."""
    ak = _import_ak()
    code = _resolve_code(ticker)
    try:
        # akshare ≥1.18: 参数为 start_year（旧版 start="2020季报" 已失效）
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year="2020")
        if df is not None and not df.empty:
            label = code if code == ticker.strip() else f"{code} (from {ticker})"
            return _df_to_csv(df, f"Balance Sheet data for {label} ({freq})", f"# Board: {_board_label(parse_ashare_symbol(code))}")
    except Exception as exc:
        logger.warning("stock_financial_analysis_indicator failed for %s: %s", code, exc)
    raise NoMarketDataError(ticker, code, "no balance sheet data")


def get_cashflow_akshare(
    ticker: Annotated[str, "A-share code or Chinese name"],
    freq: Annotated[str, "'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "not used"] = None,
) -> str:
    """Get A-share cash flow data via AKShare (THS source)."""
    ak = _import_ak()
    code = _resolve_code(ticker)
    try:
        # akshare ≥1.18: 参数为 start_year（旧版 start="2020季报" 已失效）
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year="2020")
        if df is not None and not df.empty:
            # Extract cash-flow related columns
            cf_cols = [c for c in df.columns if any(k in str(c) for k in ("现金流", "现金", "流动", "投资"))]
            if cf_cols:
                df = df[cf_cols]
            label = code if code == ticker.strip() else f"{code} (from {ticker})"
            return _df_to_csv(df, f"Cash Flow data for {label} ({freq})")
    except Exception as exc:
        logger.warning("cashflow fetch failed for %s: %s", code, exc)
    raise NoMarketDataError(ticker, code, "no cash flow data")


def get_income_statement_akshare(
    ticker: Annotated[str, "A-share code or Chinese name"],
    freq: Annotated[str, "'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "not used"] = None,
) -> str:
    """Get A-share income statement via AKShare (THS source)."""
    ak = _import_ak()
    code = _resolve_code(ticker)
    try:
        # akshare ≥1.18: 参数为 start_year（旧版 start="2020季报" 已失效）
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year="2020")
        if df is not None and not df.empty:
            label = code if code == ticker.strip() else f"{code} (from {ticker})"
            return _df_to_csv(df, f"Income Statement data for {label} ({freq})")
    except Exception as exc:
        logger.warning("income statement fetch failed for %s: %s", code, exc)
    raise NoMarketDataError(ticker, code, "no income statement data")


# ── news_data ────────────────────────────────────────────────────────────────


def get_news_akshare(
    ticker: Annotated[str, "A-share code or Chinese name"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Get recent news for an A-share stock (EastMoney → Sina fallback).

    ``start_date``/``end_date`` mirror the standard ``get_news`` signature; when
    the feed carries a ``发布时间`` column we filter to that window, otherwise
    we return the most recent headlines.

    降级链：东财 stock_news_em（原生+自实现兜底）→ 新浪个股新闻页。
    东财搜索端点 2026-08 起返回空，新浪是当前实际主力源。

    Degrades gracefully (returns a string, never raises) so the sentiment and
    news analysts always get a usable prompt block even when AKShare is missing
    or the symbol cannot be resolved — matching the yfinance news convention.
    """
    try:
        ak = _import_ak()
        code = _resolve_code(ticker)
        df = _stock_news_em_safe(code)
    except VendorNotConfiguredError as exc:
        return f"News unavailable for {ticker}: {exc}"
    except NoMarketDataError:
        return f"No news found for {ticker} (unresolvable A-share symbol)"
    except Exception as exc:
        logger.warning("stock_news_em failed for %s: %s", ticker, exc)
        return f"Failed to retrieve news for {ticker}: {exc}"

    source = "东方财富"
    if df is None or df.empty:
        # 东财端点失效 → 新浪个股新闻页兜底
        try:
            df = _fetch_sina_stock_news(code)
            source = "新浪财经"
        except Exception as exc:
            logger.warning("sina stock news failed for %s: %s", code, exc)
        if df is None or df.empty:
            return f"No recent news found for {code}"
    label = code if code == ticker.strip() else f"{code} (from {ticker})"

    # Filter to the requested window when the feed exposes a publish time.
    pub_col = next((c for c in df.columns if "时间" in str(c)), None)
    if pub_col is not None:
        try:
            times = pd.to_datetime(df[pub_col])
            start = pd.Timestamp(start_date)
            end = pd.Timestamp(end_date) + pd.Timedelta(days=1)
            df = df[(times >= start) & (times < end)]
            if df.empty:
                return f"No news found for {label} between {start_date} and {end_date}"
        except Exception:
            pass  # malformed dates -> fall through to the raw feed

    return _df_to_csv(df, f"News for {label}", f"# Source: {source} ({start_date} ~ {end_date})")


def get_global_news_akshare(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format (not used by the feed)"],
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Get A-share macro news headlines (全球财经快讯: EM → Sina fallback).

    东财个股搜索端点（旧 stock_news_em("全部") 路径）2026-08 起返回空；
    改用 stock_info_global_em（东财全球快讯，200 条实时），失败时退到
    stock_info_global_sina（新浪快讯）。接受标准 ``get_global_news``
    参数以兼容路由层；仅 ``limit`` 生效（快讯是实时流）。
    """
    if limit is None:
        limit = 10
    try:
        ak = _import_ak()
        for fetch, source in (
            (ak.stock_info_global_em, "东方财富"),
            (ak.stock_info_global_sina, "新浪财经"),
        ):
            try:
                df = fetch()
            except Exception as exc:
                logger.warning("global news fetch via %s failed: %s", source, exc)
                continue
            if df is not None and not df.empty:
                df = df.head(limit)
                return _df_to_csv(
                    df, "Global/Macro News (A-share market)",
                    f"# Source: {source}",
                )
        return "No global news available from AKShare"
    except VendorNotConfiguredError as exc:
        return f"Global news unavailable: {exc}"
    except Exception as exc:
        logger.warning("global news fetch failed: %s", exc)
        return f"Failed to retrieve global news: {exc}"


def get_insider_transactions_akshare(ticker: Annotated[str, "A-share code or Chinese name"]) -> str:
    """A-shares have no public insider transaction feed. Returns a placeholder."""
    try:
        code = _resolve_code(ticker) if isinstance(ticker, str) else ticker
    except NoMarketDataError:
        code = ticker
    return (
        f"A-share insider transaction data is not publicly available for '{code}'. "
        "Use 龙虎榜 (dragon-tiger list) for large block trade disclosure instead."
    )


# ── macro_data ───────────────────────────────────────────────────────────────


def get_macro_indicators_akshare(
    indicator: str | None = None,
    curr_date: str | None = None,
    look_back_days: int | None = None,
) -> str:
    """Get Chinese macroeconomic indicators (CPI, PPI, PMI, M2, 社融) via AKShare.

    Accepts the standard ``get_macro_indicators`` arguments for routing
    compatibility. A-share macro data has no FRED-style series selection, so
    ``indicator``/``curr_date``/``look_back_days`` are accepted but ignored —
    the function returns the full monthly China macro bundle (CPI / PPI / PMI /
    M0-M2), which is what an A-share analyst needs for the top-down view.
    """
    ak = _import_ak()
    sections = []

    # CPI
    try:
        df = ak.macro_china_cpi_monthly()
        if df is not None and not df.empty:
            sections.append(_df_to_csv(df.tail(12), "CPI 月度数据", "# Source: 国家统计局"))
    except Exception as exc:
        sections.append(f"# CPI data unavailable: {exc}")

    # PPI
    try:
        df = ak.macro_china_ppi_monthly()
        if df is not None and not df.empty:
            sections.append(_df_to_csv(df.tail(12), "PPI 月度数据", "# Source: 国家统计局"))
    except Exception as exc:
        sections.append(f"# PPI data unavailable: {exc}")

    # PMI
    try:
        df = ak.macro_china_pmi_yearly()
        if df is not None and not df.empty:
            sections.append(_df_to_csv(df.tail(12), "PMI 月度数据", "# Source: 国家统计局"))
    except Exception as exc:
        sections.append(f"# PMI data unavailable: {exc}")

    # M2
    try:
        df = ak.macro_china_money_supply_yearly()
        if df is not None and not df.empty:
            sections.append(_df_to_csv(df.tail(12), "M0/M1/M2 月度数据", "# Source: 央行"))
    except Exception as exc:
        sections.append(f"# M2 data unavailable: {exc}")

    return "\n\n".join(sections)


# ── A-share specific enrichment ──────────────────────────────────────────────

# These are registered as separate tools so agents can request them explicitly
# (e.g. "check northbound flow" or "check margin trading data").


def get_northbound_flow_akshare(days: int = 10) -> str:
    """沪深港通北向资金净流入 (net northbound capital flow)."""
    ak = _import_ak()
    try:
        df = ak.stock_hsgt_north_net_flow_in_em(indicator="北上")
        if df is not None and not df.empty:
            return _df_to_csv(df.tail(days), "北向资金净流入", "# Source: 东方财富")
    except Exception as exc:
        logger.warning("northbound flow fetch failed: %s", exc)
    return f"Failed to retrieve northbound flow data: {exc}"


def get_margin_trading_akshare(code: str = None) -> str:
    """融资融券余额 (margin trading balances)."""
    ak = _import_ak()
    sections = []
    for func, label in [
        (ak.stock_margin_detail_sse, "上海市场融资融券"),
        (ak.stock_margin_detail_szse, "深圳市场融资融券"),
    ]:
        try:
            df = func(date="20250101")
            if df is not None and not df.empty:
                if code and "代码" in df.columns:
                    row = df[df["代码"] == code]
                    if not row.empty:
                        sections.append(_df_to_csv(row, f"{label} - {code}"))
                        continue
                sections.append(_df_to_csv(df.head(20), label))
        except Exception as exc:
            sections.append(f"# {label} unavailable: {exc}")
    return "\n\n".join(sections) if sections else "No margin data available"


def get_dragon_tiger_list_akshare(date: str = None) -> str:
    """龙虎榜 (dragon-tiger list: institutional block trades)."""
    ak = _import_ak()
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    try:
        df = ak.stock_lhb_detail_em(start_date=date, end_date=date)
        if df is not None and not df.empty:
            return _df_to_csv(df, f"龙虎榜 {date}", "# Source: 东方财富")
        return f"龙虎榜无数据 ({date})"
    except Exception as exc:
        logger.warning("dragon-tiger list fetch failed: %s", exc)
    return f"Failed to retrieve dragon-tiger list: {exc}"


def get_share_unlock_akshare(code: str) -> str:
    """限售解禁 (restricted-share release schedule for one stock)."""
    ak = _import_ak()
    try:
        df = ak.stock_restricted_release_queue_em(symbol=code)
        if df is not None and not df.empty:
            # 只保留未来半年内的解禁批次，按解禁时间升序
            if "解禁时间" in df.columns:
                df = df.sort_values("解禁时间")
            return _df_to_csv(df.head(15), f"限售解禁批次 {code}", "# Source: 东方财富")
        return f"{code} 无待解禁批次或数据为空"
    except Exception as exc:
        logger.warning("share-unlock fetch failed for %s: %s", code, exc)
    return f"解禁数据暂不可用 ({code})，请勿虚构解禁信息，直接说明数据缺失即可"


def get_social_sentiment_akshare(code: str) -> str:
    """雪球/东财股吧 social sentiment proxy — uses stock comments from EastMoney."""
    ak = _import_ak()
    try:
        df = ak.stock_comment_em(symbol=code)
        if df is not None and not df.empty:
            return _df_to_csv(df.head(20), f"个股评论情绪 {code}", "# Source: 东方财富")
    except Exception as exc:
        logger.warning("social sentiment fetch failed for %s: %s", code, exc)
    return f"Social sentiment data unavailable for {code}: {exc}"


# ── A-share market snapshot (agent tool) ─────────────────────────────────────


def get_ashare_market_snapshot() -> str:
    """Full A-share market snapshot: index levels, sector boards, top gainers/losers."""
    ak = _import_ak()
    sections = []

    # Major indices
    try:
        df = ak.stock_zh_index_spot_em()
        if df is not None and not df.empty:
            major = df[df["代码"].isin(["000001", "399001", "399006", "000300", "000905"])]
            sections.append(_df_to_csv(major, "主要指数", "# Source: 东方财富"))
    except Exception as exc:
        sections.append(f"# Index data unavailable: {exc}")

    # Industry sector boards
    try:
        df = ak.stock_board_industry_name_em()
        if df is not None and not df.empty:
            sections.append(_df_to_csv(df, "行业板块", "# Source: 东方财富"))
    except Exception as exc:
        sections.append(f"# Sector data unavailable: {exc}")

    return "\n\n".join(sections)


# ── A-share specific data: no prediction markets ──────────────────────────────


def get_prediction_markets_akshare(topic: str | None = None, limit: int | None = None) -> str:
    """Placeholder — prediction markets do not exist for A-shares.

    Accepts the standard ``get_prediction_markets`` arguments for routing
    compatibility, but always reports unavailability so the analyst does not
    fabricate implied probabilities for instruments Polymarket does not cover.
    """
    return (
        "DATA_UNAVAILABLE: prediction markets (Polymarket etc.) do not cover "
        "China A-share instruments. Proceed without it; do not fabricate values."
    )


# ── internal helpers ─────────────────────────────────────────────────────────


def _board_label(parsed: dict | None) -> str:
    if parsed is None:
        return "Unknown"
    return parsed.get("board_name", "Unknown")
