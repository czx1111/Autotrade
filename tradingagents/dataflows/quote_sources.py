"""多源行情：腾讯 / 新浪 / 东财(akshare)，单票实时报价与日K，带故障转移。

公开免费接口（无需 key）：

- 腾讯  实时 ``qt.gtimg.cn/q=sh600519``（GBK）；日K(前复权)
        ``web.ifzq.gtimg.cn/appstock/app/fqkline/get``
- 新浪  实时 ``hq.sinajs.cn/list=sh600519``（GBK，需 Referer）；日K(不复权)
        ``quotes.sina.cn/cn/api/jsonp_v2.php/.../CN_MarketDataService.getKLineData``
- 东财  经由 akshare（``stock_zh_a_hist``），本项目数据层主用源。

设计约定：

- 入参/出参统一用 bare code（"600519"）；返回标准化 dict / DataFrame；
- 解析函数是纯函数（str → 数据），网络函数分离，便于单测与故障转移；
- :func:`get_quote` / :func:`get_kline` 按供应商顺序逐个尝试，任一成功即返回，
  并带进程内 TTL 缓存（盯盘高频轮询不重复打网络）。

注：阿里无公开免费 A 股行情 API，故多源覆盖为 腾讯+新浪+东财 三家。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_QUOTE_VENDORS = ("tencent", "sina")
DEFAULT_KLINE_VENDORS = ("em", "tencent", "pytdx", "sina")

_TIMEOUT = 5
QUOTE_TTL = 45.0        # 实时报价缓存秒数
KLINE_TTL = 1800.0      # 日K缓存秒数（30 分钟）

_quote_cache: dict[str, tuple[float, dict]] = {}
_kline_cache: dict[tuple[str, int], tuple[float, pd.DataFrame]] = {}


# ── 代码 → 交易所前缀 ─────────────────────────────────────────────────────


def exchange_prefix(code: str) -> str | None:
    """bare code → ``"sh"`` / ``"sz"`` / ``"bj"``；无法识别返回 None。"""
    code = str(code).strip()
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("0", "2", "3")):
        return "sz"
    if code.startswith(("4", "8")):
        return "bj"
    return None


def to_tencent_symbol(code: str) -> str:
    prefix = exchange_prefix(code)
    if prefix is None:
        raise ValueError(f"cannot resolve exchange for code {code!r}")
    return f"{prefix}{code}"


# ── HTTP ──────────────────────────────────────────────────────────────────


def _http_get(url: str, headers: dict | None = None) -> str:
    import requests

    resp = requests.get(url, headers=headers or {}, timeout=_TIMEOUT)
    resp.raise_for_status()
    if resp.encoding is None or resp.encoding.lower() not in ("gbk", "gb2312"):
        resp.encoding = "gbk"
    return resp.text


# ── 腾讯：实时报价 ────────────────────────────────────────────────────────


def parse_tencent_quote(text: str) -> dict | None:
    """解析 ``v_sh600519="1~名称~代码~现价~昨收~今开~..."``。

    字段索引（腾讯约定，防御式取值）：1 名称 / 3 现价 / 4 昨收 / 5 今开 /
    33 最高 / 34 最低 / 36 成交量(手) / 37 成交额(万) / 38 换手率 / 39 PE /
    44 总市值(亿) / 46 市净率。
    涨跌幅自行由现价/昨收计算，避免不同端字段漂移。
    """
    if "~" not in text:
        return None
    payload = text.split("=", 1)[-1].strip().strip(';"').strip()
    fields = payload.split("~")
    if len(fields) < 40:
        return None

    def _f(i: int) -> float:
        try:
            return float(fields[i])
        except (ValueError, IndexError):
            return 0.0

    name = fields[1]
    price = _f(3)
    prev_close = _f(4)
    if not name or price <= 0:
        return None
    pct = (price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
    result = {
        "name": name,
        "price": price,
        "prev_close": prev_close,
        "open": _f(5),
        "high": _f(33),
        "low": _f(34),
        "volume": _f(36) * 100,        # 手 → 股
        "amount": _f(37) * 1e4,        # 万 → 元
        "turnover": _f(38),
        "pe": _f(39),
        "pct": round(pct, 2),
        "source": "tencent",
    }
    # 补全字段（腾讯 88 字段版本）
    if len(fields) > 44:
        mktcap = _f(44)
        if mktcap > 0:
            result["mktcap"] = mktcap * 1e8    # 亿 → 元
    if len(fields) > 46:
        pb = _f(46)
        if pb > 0:
            result["pb"] = pb
    return result


def fetch_tencent_quote(code: str) -> dict:
    sym = to_tencent_symbol(code)
    text = _http_get(f"https://qt.gtimg.cn/q={sym}")
    quote = parse_tencent_quote(text)
    if quote is None:
        raise ValueError(f"tencent quote unparsable for {code}")
    return quote


# ── 新浪：实时报价 ────────────────────────────────────────────────────────


def parse_sina_quote(text: str) -> dict | None:
    """解析 ``var hq_str_sh600519="名称,今开,昨收,现价,最高,最低,...,成交量(股),成交额(元),...";``"""
    match = re.search(r'"([^"]+)"', text)
    if not match:
        return None
    fields = match.group(1).split(",")
    if len(fields) < 10:
        return None

    def _f(i: int) -> float:
        try:
            return float(fields[i])
        except (ValueError, IndexError):
            return 0.0

    name = fields[0]
    price = _f(3)
    prev_close = _f(2)
    if not name or price <= 0:
        return None
    pct = (price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
    return {
        "name": name,
        "price": price,
        "prev_close": prev_close,
        "open": _f(1),
        "high": _f(4),
        "low": _f(5),
        "volume": _f(8),               # 股
        "amount": _f(9),               # 元
        "turnover": 0.0,
        "pe": 0.0,
        "pct": round(pct, 2),
        "source": "sina",
    }


def fetch_sina_quote(code: str) -> dict:
    sym = to_tencent_symbol(code)
    text = _http_get(
        f"https://hq.sinajs.cn/list={sym}",
        headers={"Referer": "https://finance.sina.com.cn"},
    )
    quote = parse_sina_quote(text)
    if quote is None:
        raise ValueError(f"sina quote unparsable for {code}")
    return quote


# ── 腾讯：日K ─────────────────────────────────────────────────────────────


def parse_tencent_kline(payload: dict, code: str) -> pd.DataFrame:
    """解析腾讯 fqkline JSON → 标准化 DataFrame（date/open/close/high/low/volume[股]）。"""
    sym = to_tencent_symbol(code)
    node = payload.get("data", {}).get(sym, {})
    rows = node.get("qfqday") or node.get("day") or []
    records = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        records.append({
            "date": str(row[0]),
            "open": float(row[1]),
            "close": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "volume": float(row[5]) * 100,   # 手 → 股
        })
    if not records:
        raise ValueError(f"tencent kline empty for {code}")
    return pd.DataFrame(records)


def fetch_tencent_kline(code: str, days: int = 250) -> pd.DataFrame:
    sym = to_tencent_symbol(code)
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={sym},day,,,{days},qfq"
    )
    payload = json.loads(_http_get(url))
    return parse_tencent_kline(payload, code)


# ── 新浪：日K ─────────────────────────────────────────────────────────────


def parse_sina_kline(text: str) -> pd.DataFrame:
    """解析新浪 JSONP：``var _=[{"day":"2026-03-01","open":...},...]``（不复权）。"""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("sina kline payload has no JSON array")
    rows = json.loads(text[start:end + 1])
    records = []
    for row in rows:
        if not isinstance(row, dict) or "day" not in row:
            continue
        records.append({
            "date": str(row["day"]),
            "open": float(row.get("open", 0)),
            "close": float(row.get("close", 0)),
            "high": float(row.get("high", 0)),
            "low": float(row.get("low", 0)),
            "volume": float(row.get("volume", 0)),   # 股
        })
    if not records:
        raise ValueError("sina kline empty")
    return pd.DataFrame(records)


def fetch_sina_kline(code: str, days: int = 250) -> pd.DataFrame:
    sym = to_tencent_symbol(code)
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService"
        f".getKLineData?symbol={sym}&scale=240&ma=no&datalen={days}"
    )
    return parse_sina_kline(_http_get(url, headers={"Referer": "https://finance.sina.com.cn"}))


# ── 东财：日K（经 akshare，前复权） ──────────────────────────────────────


def fetch_em_kline(code: str, days: int = 250) -> pd.DataFrame:
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover
        raise ValueError("akshare is not installed") from exc

    from datetime import timedelta

    end = datetime.now()
    start = end - timedelta(days=days)
    df = ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
        adjust="qfq",
    )
    if df is None or df.empty:
        raise ValueError(f"em kline empty for {code}")
    col_map = {"日期": "date", "开盘": "open", "收盘": "close",
               "最高": "high", "最低": "low", "成交量": "volume"}
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    df["date"] = df["date"].astype(str)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")  # 东财为手
    df["volume"] = df["volume"] * 100                            # 手 → 股
    keep = [c for c in ("date", "open", "close", "high", "low", "volume") if c in df.columns]
    return df[keep].reset_index(drop=True)


# ── pytdx：日K（通达信，含成交额） ─────────────────────────────────────────


def fetch_pytdx_kline(code: str, days: int = 250) -> pd.DataFrame:
    """通达信日K（不含前复权，但含成交额 amount）。"""
    from . import pytdx_source

    df = pytdx_source.fetch_kline(code, days)
    if df is None or df.empty:
        raise ValueError(f"pytdx kline empty for {code}")
    return df


# ── 故障转移入口（带 TTL 缓存） ──────────────────────────────────────────


def _resolve_fetcher(vendor: str, kind: str):
    """按供应商名运行时查找 fetcher（便于测试 monkeypatch 模块属性）。"""
    name = {"quote": {"tencent": "fetch_tencent_quote", "sina": "fetch_sina_quote"},
            "kline": {"em": "fetch_em_kline", "tencent": "fetch_tencent_kline",
                      "pytdx": "fetch_pytdx_kline", "sina": "fetch_sina_kline"}}[kind].get(vendor)
    return getattr(globals().get(name), "__call__", None) if name else None


def get_quote(code: str, vendors: tuple[str, ...] = DEFAULT_QUOTE_VENDORS) -> dict | None:
    """多源实时报价：按顺序尝试，任一成功即返回；全失败返回 None。"""
    cached = _quote_cache.get(code)
    now = time.time()
    if cached and now - cached[0] < QUOTE_TTL:
        return cached[1]

    errors = []
    for vendor in vendors:
        fetcher = _resolve_fetcher(vendor, "quote")
        if fetcher is None:
            continue
        try:
            quote = fetcher(code)
            if quote.get("price", 0) > 0:
                _quote_cache[code] = (now, quote)
                return quote
        except Exception as exc:
            errors.append(f"{vendor}: {exc}")
            logger.debug("quote vendor %s failed for %s: %s", vendor, code, exc)
    if errors:
        logger.warning("all quote vendors failed for %s: %s", code, "; ".join(errors))
    return None


def get_kline(
    code: str,
    days: int = 250,
    vendors: tuple[str, ...] = DEFAULT_KLINE_VENDORS,
) -> pd.DataFrame:
    """多源日K：东财(前复权) → 腾讯(前复权) → 新浪(不复权)。全失败返回空表。"""
    key = (code, days)
    cached = _kline_cache.get(key)
    now = time.time()
    if cached is not None and now - cached[0] < KLINE_TTL and not cached[1].empty:
        return cached[1]

    errors = []
    for vendor in vendors:
        fetcher = _resolve_fetcher(vendor, "kline")
        if fetcher is None:
            continue
        try:
            df = fetcher(code, days)
            if df is not None and not df.empty:
                _kline_cache[key] = (now, df)
                return df
        except Exception as exc:
            errors.append(f"{vendor}: {exc}")
            logger.debug("kline vendor %s failed for %s: %s", vendor, code, exc)
    if errors:
        logger.warning("all kline vendors failed for %s: %s", code, "; ".join(errors))
    return pd.DataFrame()
