"""TradingAgents Web UI 数据层。

集中封装 UI 需要的行情数据访问（akshare）与纯数据处理函数：

- akshare 调用全部走 :mod:`streamlit` 的 ``st.cache_data`` 缓存（全市场
  快照约 5000 行，缓存后搜索/列表/行情卡片共用一份）；
- 纯函数（重命名、均线、过滤、格式化）与 IO 分离，便于单元测试。

UI 之外不要 import 本模块（依赖 streamlit）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

try:
    import streamlit as st
except ImportError as exc:  # pragma: no cover - streamlit 是 ui 可选依赖
    raise ImportError(
        "streamlit is not installed. Install UI deps with: "
        'pip install "tradingagents[ui]"'
    ) from exc

# akshare 东财快照列 → UI 通用列名
SPOT_COL_MAP = {
    "代码": "code",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "pct",
    "涨跌额": "chg",
    "成交量": "volume",
    "成交额": "amount",
    "换手率": "turnover",
    "市盈率-动态": "pe",
    "市净率": "pb",
    "总市值": "mktcap",
    "60日涨跌幅": "pct60d",
    "年初至今涨跌幅": "pctYtd",
}

# K 线历史列 → 通用列名
KLINE_COL_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct",
    "换手率": "turnover",
}

MA_WINDOWS = (5, 10, 20, 60)


# ── 纯函数（可单测） ───────────────────────────────────────────────────────


def rename_columns(df: pd.DataFrame, col_map: dict[str, str]) -> pd.DataFrame:
    """按映射重命名存在的列（akshare 版本差异容忍：缺列跳过）。"""
    return df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})


def add_ma(df: pd.DataFrame, windows: tuple[int, ...] = MA_WINDOWS) -> pd.DataFrame:
    """在 K 线 DataFrame 上追加 MA5/MA10/... 列（基于 close）。"""
    out = df.copy()
    for w in windows:
        out[f"ma{w}"] = out["close"].rolling(window=w).mean()
    return out


def search_stocks(spot: pd.DataFrame, query: str) -> pd.DataFrame:
    """按代码前缀或名称包含过滤全市场快照。query 去空格；空则原样返回。"""
    q = (query or "").strip()
    if not q:
        return spot
    mask = spot["code"].astype(str).str.startswith(q) | spot["name"].astype(str).str.contains(q, na=False)
    return spot[mask]


def filter_market(
    spot: pd.DataFrame,
    *,
    pct_min: float | None = None,
    pct_max: float | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    exclude_st: bool = True,
) -> pd.DataFrame:
    """选股过滤器：涨跌幅/价格区间，默认剔除 ST 与退市。"""
    out = spot.copy()
    if exclude_st:
        name = out["name"].astype(str)
        out = out[~name.str.contains("ST", na=False) & ~name.str.contains("退", na=False)]
    if pct_min is not None:
        out = out[out["pct"] >= pct_min]
    if pct_max is not None:
        out = out[out["pct"] <= pct_max]
    if price_min is not None:
        out = out[out["price"] >= price_min]
    if price_max is not None:
        out = out[out["price"] <= price_max]
    return out


def fmt_amount(value: float) -> str:
    """成交额/市值格式化：亿/万。"""
    if value != value:  # NaN
        return "-"
    if abs(value) >= 1e8:
        return f"{value / 1e8:.2f}亿"
    if abs(value) >= 1e4:
        return f"{value / 1e4:.1f}万"
    return f"{value:.0f}"


def kline_range(days: int, today: datetime | None = None) -> tuple[str, str]:
    """K 线取数区间：今天往前 ``days`` 个自然日 → (start, end)，yyyy-mm-dd。"""
    end = today or datetime.now()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# ── akshare 数据访问（st.cache_data 缓存） ────────────────────────────────


def _import_ak():
    try:
        import akshare as ak

        return ak
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("akshare is not installed. Run: pip install akshare") from exc


# 防止 fallback 递归（akshare → em 直连 → 再失败不重试）
_EM_SPOT_FALLBACK_LOCK = False


def _load_market_spot_em_single_page() -> pd.DataFrame:
    """东财单页快照降级：直接 HTTP 请求东财 clist API（一页拉全部）。

    当 akshare 的 ``stock_zh_a_spot_em`` 因分页限流断连时，用单页 6000 条
    请求兜底。字段编号对齐东财 ``f`` 约定，返回标准列名 DataFrame。
    """
    import requests

    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=6000&po=1&np=1"
        "&fields=f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f20,f21,f23"
        "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
        ",m:0+t:81+s:2049,m:0+t:82+s:2048"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("diff") or []
    except Exception as exc:
        logger.warning("load_market_spot em fallback failed: %s", exc)
        return pd.DataFrame()

    if not data:
        return pd.DataFrame()

    rows = []
    for item in data:
        rows.append({
            "code": str(item.get("f12", "")),
            "name": str(item.get("f14", "")),
            "price": item.get("f2"),
            "pct": item.get("f3"),
            "chg": item.get("f4"),
            "volume": item.get("f5"),
            "amount": item.get("f6"),
            "turnover": item.get("f8"),
            "pe": item.get("f9"),
            "pb": item.get("f23") if item.get("f23") != "-" else None,
            "mktcap": item.get("f20"),
        })
    return pd.DataFrame(rows)


def _load_market_spot_sina() -> pd.DataFrame:
    """新浪全市场快照降级：多线程并发拉取全部 A 股（沪深主板+创业板+科创板+北交所）。

    新浪 API 每页最多 100 条，约 55 页。使用 8 线程并发，6 秒内完成。
    新浪返回字段包括 per(PE)、pb(PB)、mktcap(总市值)、turnoverratio(换手率)，
    均完整解析，返回标准列名 DataFrame。
    """
    import json
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import requests

    nodes = [
        ("hs_a", "沪深A股"),   # 沪深主板+创业板+科创板
    ]

    def _fetch_page(node: str, page: int) -> list[dict]:
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api"
            f"/json_v2.php/Market_Center.getHQNodeData"
            f"?page={page}&num=100&node={node}&sort=changepercent&asc=0&_s_a_auto="
        )
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            if resp.text.strip() in ("", "null", "[]"):
                return []
            items = json.loads(resp.text)
            return items or []
        except Exception as exc:
            logger.debug("load_market_spot sina page %d failed: %s", page, exc)
            return []

    rows = []
    for node, _label in nodes:
        # 先拉第一页确定总页数
        first = _fetch_page(node, 1)
        if not first:
            continue
        rows.extend(first)
        # 并发拉剩余页（第 2~56 页）
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_fetch_page, node, p) for p in range(2, 57)]
            for f in as_completed(futs):
                rows.extend(f.result())

    if not rows:
        return pd.DataFrame()

    records = []
    for item in rows:
        code = str(item.get("code", ""))
        # 新浪返回 sh600519 / sz000858 / bj920000
        if len(code) > 6:
            code = code[-6:]
        records.append({
            "code": code,
            "name": str(item.get("name", "")),
            "price": float(item.get("trade", 0) or 0),
            "pct": float(item.get("changepercent", 0) or 0),
            "chg": float(item.get("pricechange", 0) or 0),
            "volume": float(item.get("volume", 0) or 0),
            "amount": float(item.get("amount", 0) or 0),
            "turnover": float(item.get("turnoverratio", 0) or 0),
            "pe": float(item.get("per", 0) or 0) if item.get("per") else 0.0,
            "pb": float(item.get("pb", 0) or 0) if item.get("pb") else 0.0,
            "mktcap": float(item.get("mktcap", 0) or 0) * 10000 if item.get("mktcap") else 0.0,  # 新浪返回万元
        })
    return pd.DataFrame(records)


def _load_indices_tencent() -> pd.DataFrame:
    """腾讯指数快照降级：通过 ``qt.gtimg.cn`` 批量获取主要指数。"""
    import requests

    # 腾讯格式: sh000001 / sz399001 等
    idx_codes = [
        ("sh000001", "上证指数", "000001"),
        ("sz399001", "深证成指", "399001"),
        ("sz399006", "创业板指", "399006"),
        ("sh000300", "沪深300", "000300"),
        ("sh000905", "中证500", "000905"),
        ("sh000688", "科创50", "000688"),
    ]
    syms = ",".join(s[0] for s in idx_codes)
    try:
        resp = requests.get(f"https://qt.gtimg.cn/q={syms}", timeout=5)
        resp.raise_for_status()
        if resp.encoding is None or resp.encoding.lower() not in ("gbk", "gb2312"):
            resp.encoding = "gbk"
        text = resp.text
    except Exception as exc:
        logger.warning("load_indices tencent failed: %s", exc)
        return pd.DataFrame()

    rows = []
    for sym, label, code in idx_codes:
        # 每只股票一行: v_sh000001="..."
        prefix = f"v_{sym}="
        idx = text.find(prefix)
        if idx < 0:
            continue
        payload = text[idx + len(prefix):]
        end = payload.find('";')
        if end < 0:
            end = payload.find('"')
        fields = payload[:end].split("~")
        if len(fields) < 10:
            continue
        try:
            price = float(fields[3]) if fields[3] else 0.0
            prev_close = float(fields[4]) if fields[4] else 0.0
            pct = (price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
            rows.append({
                "code": code,
                "name": label,
                "price": price,
                "pct": round(pct, 2),
                "chg": price - prev_close,
                "volume": float(fields[36]) * 100 if len(fields) > 36 and fields[36] else 0.0,
                "amount": float(fields[37]) * 1e4 if len(fields) > 37 and fields[37] else 0.0,
            })
        except (ValueError, IndexError):
            continue
    return pd.DataFrame(rows)


def _load_indices_em_fallback() -> pd.DataFrame:
    """东财指数快照降级：直接 HTTP 请求东财指数 clist API。"""
    import requests

    secids = [
        ("1.000001", "上证指数"), ("0.399001", "深证成指"),
        ("0.399006", "创业板指"), ("1.000300", "沪深300"),
        ("1.000905", "中证500"), ("1.000688", "科创50"),
    ]
    rows = []
    for secid, label in secids:
        url = (
            f"https://push2.eastmoney.com/api/qt/stock/get"
            f"?secid={secid}&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f168,f169,f170"
        )
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            d = resp.json().get("data", {})
            if not d:
                continue
            rows.append({
                "code": secid.split(".")[-1],
                "name": label,
                "price": d.get("f43", 0) / 100.0 if d.get("f43") else 0,
                "pct": d.get("f170", 0) / 100.0 if d.get("f170") else 0,
                "chg": (d.get("f43", 0) - d.get("f60", 0)) / 100.0 if d.get("f43") and d.get("f60") else 0,
                "volume": d.get("f47", 0),
                "amount": d.get("f48", 0),
            })
        except Exception as exc:
            logger.debug("indices em fallback %s failed: %s", secid, exc)
            continue
    return pd.DataFrame(rows)


def _load_sector_boards_baostock() -> pd.DataFrame:
    """BaoStock 行业分类 + pytdx 全市场快照 → 行业板块涨跌幅。

    BaoStock 提供 84 个行业分类（申万行业标准），每只股票有行业标签。
    结合 pytdx 全市场实时快照，计算每个行业的平均涨跌幅、上涨/下跌家数。
    """
    # 1. 获取行业分类（缓存 30 分钟，第一次约 20 秒）
    ind_df = _load_baostock_industry_cached()
    if ind_df is None or ind_df.empty:
        return pd.DataFrame()

    # 2. 获取全市场快照（pytdx → 新浪，不走 st.cache_data 避免缓存递归）
    spot = pd.DataFrame()
    try:
        from tradingagents.dataflows import pytdx_source

        spot = pytdx_source.fetch_market_spot()
    except Exception as exc:
        logger.debug("sector_boards baostock: pytdx spot failed: %s", exc)

    if spot is None or spot.empty:
        # 降级到新浪全市场快照
        logger.info("sector_boards baostock: pytdx 不可用，降级到新浪快照")
        try:
            spot = _load_market_spot_sina()
        except Exception as exc:
            logger.warning("sector_boards baostock: 新浪快照失败: %s", exc)
            return pd.DataFrame()

    if spot is None or spot.empty:
        return pd.DataFrame()

    # 3. 合并行业分类 + 快照
    merged = spot.merge(ind_df[["code", "industry"]], on="code", how="inner")
    if merged.empty:
        return pd.DataFrame()

    # 4. 按行业聚合
    grouped = merged.groupby("industry").agg(
        pct=("pct", "mean"),
        up=("pct", lambda x: (x > 0).sum()),
        down=("pct", lambda x: (x < 0).sum()),
        count=("code", "count"),
        amount=("amount", "sum"),
    ).reset_index()

    grouped = grouped.rename(columns={"industry": "name"})
    grouped = grouped.sort_values("pct", ascending=False)
    grouped["pct"] = grouped["pct"].round(2)
    return grouped.reset_index(drop=True)


@st.cache_data(ttl=1800, show_spinner=False)
def _load_baostock_industry_cached() -> pd.DataFrame | None:
    """BaoStock 行业分类数据（30 分钟缓存）。第一次调用约 20 秒。"""
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        logger.warning("baostock login failed: %s", lg.error_msg)
        return None

    try:
        rs = bs.query_stock_industry()
        industries: list[dict] = []
        while (rs.error_code == "0") and rs.next():
            row = rs.get_row_data()
            code = row[1].replace("sh.", "").replace("sz.", "").replace("bj.", "")
            industry = row[3]
            if industry and code:
                industries.append({"code": code, "industry": industry})
    finally:
        bs.logout()

    if not industries:
        return None
    return pd.DataFrame(industries)


@st.cache_data(ttl=300, show_spinner=False)
def load_market_spot() -> pd.DataFrame:
    """全市场 A 股快照（pytdx→东财→新浪多级降级），重命名为通用列。5 分钟缓存。

    pytdx 全市场实时行情 ~1.5 秒完成 5500 只股票，是最快最稳定的数据源。
    网络异常时返回空 DataFrame（不抛异常）。
    """
    # 优先：pytdx 全市场实时快照（~1.5s）
    try:
        from tradingagents.dataflows import pytdx_source

        df = pytdx_source.fetch_market_spot()
        if df is not None and not df.empty:
            logger.info("load_market_spot: pytdx %d stocks", len(df))
            # pytdx 返回标准列名，但缺 turnover/pe/pb/mktcap/pct60d/pctYtd
            # 补全默认值
            for col in ("turnover", "pe", "pb", "mktcap", "pct60d", "pctYtd", "chg"):
                if col not in df.columns:
                    df[col] = 0.0
            # chg = price - last_close
            if "last_close" in df.columns:
                df["chg"] = df["price"] - df["last_close"]
            keep = [c for c in SPOT_COL_MAP.values() if c in df.columns]
            df = df[keep]
            for col in ("price", "pct", "chg", "volume", "amount", "turnover", "pe", "pb", "mktcap"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df[df["price"].notna() & (df["price"] > 0)]
            return df.reset_index(drop=True)
    except Exception as exc:
        logger.warning("load_market_spot: pytdx 失败: %s", exc)

    # 降级 1: 东财 (akshare)
    ak = _import_ak()
    df = pd.DataFrame()
    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as exc:
        logger.warning("load_market_spot: akshare 请求失败: %s", exc)

    if (df is None or df.empty) and not _EM_SPOT_FALLBACK_LOCK:
        df = _load_market_spot_em_single_page()

    if df is None or df.empty:
        # 降级 2: 新浪全市场并发快照
        logger.info("load_market_spot: 东财不可用，降级到新浪全市场快照")
        df = _load_market_spot_sina()

    if df is None or df.empty:
        return pd.DataFrame()

    # 新浪 fallback 已是标准列名，跳过 rename
    if "code" not in df.columns:
        df = rename_columns(df, SPOT_COL_MAP)
    keep = [c for c in SPOT_COL_MAP.values() if c in df.columns]
    df = df[keep]
    for col in ("price", "pct", "chg", "volume", "amount", "turnover", "pe", "pb", "mktcap", "pct60d", "pctYtd"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df["price"].notna() & (df["price"] > 0)]
    return df.reset_index(drop=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_indices() -> pd.DataFrame:
    """主要指数快照（pytdx→东财→腾讯多级降级）。全失败返回空 DataFrame。
    """
    wanted = {"000001", "399001", "399006", "000300", "000905", "000688"}

    # 优先：pytdx 指数实时行情
    try:
        from tradingagents.dataflows import pytdx_source

        df = pytdx_source.fetch_indices()
        if df is not None and not df.empty:
            logger.info("load_indices: pytdx %d indices", len(df))
            return df[df["code"].astype(str).isin(wanted)].reset_index(drop=True)
    except Exception as exc:
        logger.warning("load_indices: pytdx 失败: %s", exc)

    # 降级 1: 东财 (akshare)
    ak = _import_ak()
    try:
        df = ak.stock_zh_index_spot_em()
    except Exception as exc:
        logger.warning("load_indices: akshare 请求失败: %s, 降级到东财直连", exc)
        df = _load_indices_em_fallback()
    if df is not None and not df.empty:
        df = rename_columns(df, SPOT_COL_MAP)
        keep = [c for c in SPOT_COL_MAP.values() if c in df.columns]
        df = df[keep]
        return df[df["code"].astype(str).isin(wanted)].reset_index(drop=True)
    # 降级 2: 腾讯指数
    logger.info("load_indices: 东财不可用，降级到腾讯指数")
    df = _load_indices_tencent()
    if df is not None and not df.empty:
        return df[df["code"].astype(str).isin(wanted)].reset_index(drop=True)
    return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner=False)
def load_sector_boards() -> pd.DataFrame:
    """行业板块行情（东财→BaoStock 行业分类 + pytdx 快照计算），10 分钟缓存。

    东财不可用时，用 BaoStock 行业分类 + pytdx 全市场快照计算
    每个行业的平均涨跌幅、上涨/下跌家数。
    """
    # 优先：东财 akshare
    ak = _import_ak()
    try:
        df = ak.stock_board_industry_name_em()
    except Exception as exc:
        logger.warning("load_sector_boards: akshare 请求失败: %s", exc)
        df = None

    if df is not None and not df.empty:
        col_map = {
            "板块名称": "name", "最新价": "price", "涨跌幅": "pct",
            "上涨家数": "up", "下跌家数": "down", "领涨股票": "leader",
        }
        df = rename_columns(df, col_map)
        keep = [c for c in col_map.values() if c in df.columns]
        df = df[keep]
        if "pct" in df.columns:
            df["pct"] = pd.to_numeric(df["pct"], errors="coerce")
            df = df.sort_values("pct", ascending=False)
        return df.reset_index(drop=True)

    # 降级：BaoStock 行业分类 + pytdx 全市场快照
    logger.info("load_sector_boards: 东财不可用，用 BaoStock 行业分类 + pytdx 快照")
    try:
        return _load_sector_boards_baostock()
    except Exception as exc:
        logger.warning("load_sector_boards: BaoStock 行业分类失败: %s", exc)
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def load_kline(code: str, days: int = 250) -> pd.DataFrame:
    """单只股票日 K（多源故障转移：东财→腾讯→新浪），重命名 + 均线。30 分钟缓存。"""
    from ..dataflows import quote_sources

    df = pd.DataFrame()
    try:
        ak = _import_ak()
        start, end = kline_range(days)
        raw = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start.replace("-", ""), end_date=end.replace("-", ""),
            adjust="qfq",
        )
        if raw is not None and not raw.empty:
            df = rename_columns(raw, KLINE_COL_MAP)
            keep = [c for c in KLINE_COL_MAP.values() if c in df.columns]
            df = df[keep]
            for col in keep:
                if col != "date":
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df["date"] = df["date"].astype(str)
    except Exception:
        df = pd.DataFrame()

    if df.empty:
        # 东财失败 → pytdx/腾讯/新浪故障转移（quote_sources 已标准化列名）
        try:
            df = quote_sources.get_kline(code, days, vendors=("tencent", "pytdx", "sina"))
        except Exception:
            df = pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()
    df = add_ma(df)
    return df.reset_index(drop=True)


def get_quote(code: str) -> dict | None:
    """获取单只股票的最新行情。

    优先用腾讯单股实时报价（快速，~0.5s，含 PE/PB/总市值）；
    腾讯失败用 pytdx 单股报价 + 财务补全；
    全失败再从全市场快照缓存取。
    """
    # 优先：腾讯单股实时报价（~0.5s，含完整字段）
    try:
        from tradingagents.dataflows import quote_sources

        quote = quote_sources.get_quote(code)
        if quote and quote.get("price", 0) > 0:
            # 补全快照中可能有但单股报价没有的字段（pb/mktcap）
            if not quote.get("pb") or not quote.get("mktcap"):
                try:
                    spot = load_market_spot()
                    if not spot.empty:
                        row = spot[spot["code"] == code]
                        if not row.empty:
                            r = row.iloc[0]
                            if not quote.get("pb"):
                                quote["pb"] = r.get("pb") or 0
                            if not quote.get("mktcap"):
                                quote["mktcap"] = r.get("mktcap") or 0
                except Exception:
                    pass
            return quote
    except Exception as exc:
        logger.debug("get_quote: 腾讯报价失败 %s: %s", code, exc)

    # 降级 1: pytdx 单股报价 + 财务补全
    try:
        from tradingagents.dataflows import pytdx_source

        quote = pytdx_source.fetch_quote(code)
        if quote and quote.get("price", 0) > 0:
            # 用 pytdx 财务数据补全 PB/PE/总市值
            quote = pytdx_source.enrich_quote(quote)
            return quote
    except Exception as exc:
        logger.debug("get_quote: pytdx 报价失败 %s: %s", code, exc)

    # 后备：全市场快照缓存
    try:
        spot = load_market_spot()
        if not spot.empty:
            rows = spot[spot["code"] == code]
            if not rows.empty:
                return rows.iloc[0].to_dict()
    except Exception as exc:
        logger.debug("get_quote: 快照查询失败 %s: %s", code, exc)

    return None
