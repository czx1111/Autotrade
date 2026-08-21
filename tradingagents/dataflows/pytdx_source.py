"""pytdx 通达信数据源：全市场实时行情 + K线 + 指数 + 财务数据。

利用通达信免费行情服务器（无需 API key），提供：

- :func:`fetch_market_spot`  — 全市场 A 股实时快照（~5500 只，1.5 秒完成）
- :func:`fetch_quote`        — 单只股票实时报价
- :func:`fetch_kline`        — 日K/周K/分钟K（前复权需自行处理）
- :func:`fetch_indices`      — 主要指数实时行情
- :func:`fetch_finance`      — 财务数据（总股本、净资产、净利润 → PB/总市值/PE）
- :func:`fetch_sector_spot`  — 行业板块涨跌幅（通达信板块指数）

pytdx 连接是 TCP 长连接，本模块维护一个进程级单例连接，
自动重连 + 心跳检测。所有函数线程安全（pytdx 内部有锁）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# ── 连接管理 ──────────────────────────────────────────────────────────────

_TDX_SERVERS = [
    ("115.238.56.198", 7709),
    ("115.238.90.165", 7709),
    ("117.184.140.156", 7709),
    ("60.12.136.250", 7709),
    ("218.75.126.109", 7709),
    ("112.95.73.65", 7709),
    ("112.95.73.67", 7709),
    ("58.49.35.18", 7709),
    ("139.159.233.217", 7709),
    ("114.80.149.19", 7709),
    ("114.80.149.84", 7709),
    ("114.80.149.85", 7709),
    ("221.231.141.60", 7709),
    ("180.153.39.51", 7709),
    ("120.249.82.61", 7709),
    ("119.147.164.60", 7709),
    ("113.105.142.114", 7709),
]

_lock = threading.Lock()
_api = None
_connected_ip: str | None = None
_last_heartbeat: float = 0.0
_HEARTBEAT_INTERVAL = 30.0  # 30 秒心跳


def _ensure_connection():
    """获取或重连 pytdx 连接（线程安全）。"""
    global _api, _connected_ip, _last_heartbeat

    with _lock:
        now = time.time()

        # 已连接且未超时 → 心跳
        if _api is not None and _connected_ip:
            if now - _last_heartbeat < _HEARTBEAT_INTERVAL:
                try:
                    _api.do_heartbeat()
                    _last_heartbeat = now
                    return _api
                except Exception:
                    logger.debug("pytdx heartbeat failed, reconnecting")
                    try:
                        _api.disconnect()
                    except Exception:
                        pass
                    _api = None
                    _connected_ip = None

        # 需要连接
        from pytdx.hq import TdxHq_API

        for ip, port in _TDX_SERVERS:
            try:
                api = TdxHq_API()
                if api.connect(ip, port):
                    _api = api
                    _connected_ip = f"{ip}:{port}"
                    _last_heartbeat = now
                    logger.info("pytdx connected to %s", _connected_ip)
                    return _api
            except Exception as exc:
                logger.debug("pytdx connect %s:%s failed: %s", ip, port, exc)

        logger.warning("pytdx: all servers unreachable")
        return None


def disconnect():
    """主动断开连接（进程退出时调用）。"""
    global _api, _connected_ip
    with _lock:
        if _api:
            try:
                _api.disconnect()
            except Exception:
                pass
        _api = None
        _connected_ip = None


# ── 代码转换 ──────────────────────────────────────────────────────────────


def _to_tdx(code: str) -> tuple[int, str]:
    """bare code → (market, code)。market: 0=深圳, 1=上海。"""
    code = str(code).strip()
    if code.startswith("6"):
        return (1, code)
    if code.startswith(("0", "2", "3")):
        return (0, code)
    if code.startswith(("4", "8")):
        return (0, code)  # 北交所暂归深圳
    raise ValueError(f"cannot resolve market for code {code!r}")


# ── A 股代码列表 ──────────────────────────────────────────────────────────

_name_cache: dict[tuple[int, str], str] = {}
_name_cache_loaded = False


def _load_name_cache() -> None:
    """一次性加载全市场股票名称到进程缓存。"""
    global _name_cache_loaded
    if _name_cache_loaded and len(_name_cache) > 0:
        return

    api = _ensure_connection()
    if api is None:
        return

    cache: dict[tuple[int, str], str] = {}

    for market in (0, 1):
        try:
            count = api.get_security_count(market)
        except Exception:
            continue
        if count is None or not isinstance(count, int) or count <= 0:
            continue
        # 通达信列表排序：指数→债券→基金→A股，A股在后面
        # 需要遍历全部 count，不能只取前 30000
        for start in range(0, count, 1000):
            try:
                batch = api.get_security_list(market, start)
            except Exception:
                continue  # 跳过失败批次，不 break
            if not batch:
                continue  # 空批次跳过，不 break
            for item in batch:
                code = item.get("code", "")
                name = item.get("name", "")
                if code and name:
                    cache[(market, code)] = name

    # 只有成功加载了数据才更新缓存和标志
    if cache:
        _name_cache.clear()
        _name_cache.update(cache)
        _name_cache_loaded = True
        logger.info("pytdx name cache loaded: %d entries", len(_name_cache))
    else:
        # 加载失败不设置标志，下次调用会重试
        logger.warning("pytdx name cache load failed (empty), will retry next time")


def _lookup_name(market: int, code: str) -> str:
    """从缓存查找股票名称。"""
    if not _name_cache_loaded:
        _load_name_cache()
    return _name_cache.get((market, code), "")


def _get_a_share_codes() -> list[tuple[int, str]]:
    """获取全市场 A 股代码列表 (market, code)。"""
    api = _ensure_connection()
    if api is None:
        return []

    codes: list[tuple[int, str]] = []

    for market in (0, 1):
        try:
            count = api.get_security_count(market)
        except Exception as exc:
            logger.debug("pytdx get_security_count(%d) failed: %s", market, exc)
            # 重试一次
            api = _ensure_connection()
            if api is None:
                continue
            try:
                count = api.get_security_count(market)
            except Exception:
                continue

        # count 可能为 None（断线/服务器异常），跳过而非崩溃
        if count is None or not isinstance(count, int) or count <= 0:
            logger.warning("pytdx get_security_count(%d) returned %r, skipping", market, count)
            continue

        # 分批拉取（通达信列表排序：指数→债券→基金→A股，需遍历全部）
        for start in range(0, count, 1000):
            try:
                batch = api.get_security_list(market, start)
            except Exception:
                continue  # 跳过失败批次
            if not batch:
                continue  # 空批次跳过
            for item in batch:
                code = item.get("code", "")
                # 严格过滤 A股代码：
                # 上海：600/601/603/605/688/689
                # 深圳：000/001/002/003/300/301
                if len(code) == 6:
                    if market == 1 and code.startswith(("60", "688", "689")):
                        codes.append((market, code))
                    elif market == 0 and code.startswith(("000", "001", "002", "003", "300", "301")):
                        codes.append((market, code))

    return codes


# ── 全市场快照 ────────────────────────────────────────────────────────────


def fetch_market_spot() -> pd.DataFrame:
    """全市场 A 股实时快照（~5500 只，~1.5 秒）。

    返回标准列名 DataFrame：code/name/price/pct/open/high/low/
    volume/amount/last_close/market。
    PB/PE/总市值通过 :func:`enrich_with_finance` 按需补全（避免全市场
    拉财务数据太慢）。
    """
    api = _ensure_connection()
    if api is None:
        return pd.DataFrame()

    a_codes = _get_a_share_codes()
    if not a_codes:
        return pd.DataFrame()

    # 批量拉取行情（每批 80 只）
    all_quotes: list[dict] = []
    for i in range(0, len(a_codes), 80):
        batch = a_codes[i : i + 80]
        try:
            quotes = api.get_security_quotes(batch)
            if quotes:
                all_quotes.extend(quotes)
        except Exception:
            # 重试一次（先重连）
            time.sleep(0.1)
            api = _ensure_connection()
            if api is None:
                break
            try:
                quotes = api.get_security_quotes(batch)
                if quotes:
                    all_quotes.extend(quotes)
            except Exception:
                logger.debug("pytdx batch %d failed", i // 80)

    if not all_quotes:
        return pd.DataFrame()

    records = []
    for q in all_quotes:
        price = q.get("price", 0)
        last_close = q.get("last_close", 0)
        if last_close <= 0:
            continue
        # 非交易时段 pytdx 返回 price=0，用 last_close 作为 fallback
        # 这样 pct=0%（合理：非交易时段无涨跌）
        if price <= 0:
            price = last_close
        pct = (price / last_close - 1.0) * 100.0
        records.append(
            {
                "code": str(q.get("code", "")),
                "price": price,
                "pct": round(pct, 2),
                "open": q.get("open", 0),
                "high": q.get("high", 0),
                "low": q.get("low", 0),
                "last_close": last_close,
                "volume": q.get("vol", 0) * 100,  # 手 → 股
                "amount": q.get("amount", 0),  # 元
                "market": q.get("market", 0),
            }
        )

    df = pd.DataFrame(records)
    if df.empty:
        return df

    # 补全名称（使用进程级名称缓存）
    _load_name_cache()
    df["name"] = df.apply(
        lambda row: _name_cache.get((int(row.get("market", 0)), row["code"]), ""),
        axis=1,
    )
    return df


# ── 单股实时报价 ──────────────────────────────────────────────────────────


def fetch_quote(code: str) -> dict | None:
    """单只股票实时报价。名称从股票列表查找（pytdx 行情不含名称）。"""
    api = _ensure_connection()
    if api is None:
        return None

    market, tdx_code = _to_tdx(code)
    try:
        quotes = api.get_security_quotes([(market, tdx_code)])
        if not quotes:
            return None
        q = quotes[0]
        price = q.get("price", 0)
        last_close = q.get("last_close", 0)
        if last_close <= 0:
            return None
        # 非交易时段 price=0，用 last_close 作为 fallback
        if price <= 0:
            price = last_close
        pct = (price / last_close - 1.0) * 100.0 if last_close > 0 else 0.0

        # 从股票列表查找名称
        name = _lookup_name(market, tdx_code)

        result = {
            "name": name,
            "code": code,
            "price": price,
            "prev_close": last_close,
            "open": q.get("open", 0),
            "high": q.get("high", 0),
            "low": q.get("low", 0),
            "volume": q.get("vol", 0) * 100,  # 手 → 股
            "amount": q.get("amount", 0),  # 元
            "pct": round(pct, 2),
            "source": "pytdx",
        }
        return result
    except Exception as exc:
        logger.debug("pytdx fetch_quote %s failed: %s", code, exc)
        return None


# ── 财务数据补全 ──────────────────────────────────────────────────────────


def fetch_finance(code: str) -> dict | None:
    """获取单只股票的财务数据 → 计算 PB/总市值/PE。

    返回字段：zongguben(总股本)、jingzichan(净资产)、shuihoulirun(税后利润)、
    meigujingzichan(每股净资产)、mktcap(总市值)、pb(市净率)、pe(市盈率,年化)。
    """
    api = _ensure_connection()
    if api is None:
        return None

    market, tdx_code = _to_tdx(code)
    try:
        fin = api.get_finance_info(market, tdx_code)
        if not fin:
            return None

        zongguben = float(fin.get("zongguben", 0) or 0)  # 股
        jingzichan = float(fin.get("jingzichan", 0) or 0)  # 元
        shuihoulirun = float(fin.get("shuihoulirun", 0) or 0)  # 元
        meigujingzichan = float(fin.get("meigujingzichan", 0) or 0)  # 元/股

        # 实时价格
        quotes = api.get_security_quotes([(market, tdx_code)])
        price = quotes[0].get("price", 0) if quotes else 0

        mktcap = price * zongguben if price > 0 and zongguben > 0 else 0
        pb = price / meigujingzichan if price > 0 and meigujingzichan > 0 else 0
        eps = shuihoulirun / zongguben if zongguben > 0 else 0
        pe = price / eps if eps > 0 else 0

        return {
            "zongguben": zongguben,
            "jingzichan": jingzichan,
            "shuihoulirun": shuihoulirun,
            "meigujingzichan": meigujingzichan,
            "mktcap": mktcap,
            "pb": round(pb, 2),
            "pe": round(pe, 2),
        }
    except Exception as exc:
        logger.debug("pytdx fetch_finance %s failed: %s", code, exc)
        return None


def enrich_quote(quote: dict) -> dict:
    """给单股报价补全 PB/总市值/PE（从 pytdx 财务数据计算）。"""
    code = quote.get("code", "")
    if not code:
        return quote
    fin = fetch_finance(code)
    if fin:
        if not quote.get("pb"):
            quote["pb"] = fin.get("pb", 0)
        if not quote.get("mktcap"):
            quote["mktcap"] = fin.get("mktcap", 0)
        if not quote.get("pe"):
            quote["pe"] = fin.get("pe", 0)
    return quote


# ── K线数据 ───────────────────────────────────────────────────────────────


def fetch_kline(code: str, days: int = 250) -> pd.DataFrame:
    """日K线数据（不含前复权，需调用方注意）。

    pytdx K线不提供换手率/PE 等附加字段，但包含成交额(amount)。
    """
    api = _ensure_connection()
    if api is None:
        return pd.DataFrame()

    market, tdx_code = _to_tdx(code)
    try:
        # category: 4=日K, 5=周K, 9=日K不复权, 0=5分钟K
        # count: 返回的K线条数（从最近往前）
        data = api.get_security_bars(4, market, tdx_code, 0, days)
        if not data:
            return pd.DataFrame()

        records = []
        for row in data:
            dt = row.get("datetime", "")
            records.append(
                {
                    "date": dt[:10] if dt else "",
                    "open": float(row.get("open", 0)),
                    "close": float(row.get("close", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "volume": float(row.get("vol", 0)) * 100,  # 手 → 股
                    "amount": float(row.get("amount", 0)),  # 元
                }
            )

        df = pd.DataFrame(records)
        if df.empty:
            return df
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception as exc:
        logger.debug("pytdx fetch_kline %s failed: %s", code, exc)
        return pd.DataFrame()


# ── 指数行情 ──────────────────────────────────────────────────────────────


_INDEX_CODES = [
    (1, "000001", "上证指数", "000001"),
    (0, "399001", "深证成指", "399001"),
    (0, "399006", "创业板指", "399006"),
    (1, "000300", "沪深300", "000300"),
    (1, "000905", "中证500", "000905"),
    (1, "000688", "科创50", "000688"),
]


def fetch_indices() -> pd.DataFrame:
    """主要指数实时行情。"""
    api = _ensure_connection()
    if api is None:
        return pd.DataFrame()

    try:
        quotes = api.get_security_quotes(
            [(m, c) for m, c, _, _ in _INDEX_CODES]
        )
    except Exception:
        # 降级：用指数K线取最新收盘
        return _fetch_indices_from_bars()

    if not quotes:
        return _fetch_indices_from_bars()

    records = []
    for q, (_, _, label, code) in zip(quotes, _INDEX_CODES):
        price = q.get("price", 0)
        last_close = q.get("last_close", 0)
        if last_close <= 0:
            continue
        # 非交易时段 price=0，用 last_close 作为 fallback
        if price <= 0:
            price = last_close
        pct = (price / last_close - 1.0) * 100.0 if last_close > 0 else 0.0
        records.append(
            {
                "code": code,
                "name": label,
                "price": price,
                "pct": round(pct, 2),
                "chg": price - last_close,
                "volume": q.get("vol", 0) * 100,
                "amount": q.get("amount", 0),
            }
        )

    df = pd.DataFrame(records)
    if df.empty:
        return _fetch_indices_from_bars()
    return df


def _fetch_indices_from_bars() -> pd.DataFrame:
    """指数K线 fallback（收盘后实时行情可能返回 price=0）。"""
    api = _ensure_connection()
    if api is None:
        return pd.DataFrame()

    records = []
    for market, code, label, out_code in _INDEX_CODES:
        try:
            data = api.get_index_bars(4, market, code, 0, 2)
            if data:
                latest = data[-1]
                close = float(latest.get("close", 0))
                prev = float(data[-2].get("close", 0)) if len(data) > 1 else close
                pct = (close / prev - 1.0) * 100.0 if prev > 0 else 0.0
                records.append(
                    {
                        "code": out_code,
                        "name": label,
                        "price": close,
                        "pct": round(pct, 2),
                        "chg": close - prev,
                        "volume": float(latest.get("vol", 0)) * 100,
                        "amount": float(latest.get("amount", 0)),
                    }
                )
        except Exception:
            continue

    return pd.DataFrame(records)
