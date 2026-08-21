"""A-share symbol normalization, board detection, and trading-rule helpers.

China A-share symbols are six-digit codes whose board (and therefore trading
rules — price limits, lot size) is determined by the prefix:

    prefix      board                       exchange   price limit
    --------    --------------------------   --------   -----------
    600/601/    Shanghai main board          .SH        ±10% (ST ±5%)
    603/605
    688/689     STAR Market (科创板)          .SH        ±20%
    000/001/    Shenzhen main board          .SZ        ±10% (ST ±5%)
    002/003     (ex-SME, merged 2021)
    300/301/    ChiNext (创业板)              .SZ        ±20%
    302
    43/83/87/   Beijing Stock Exchange       .BJ        ±30%
    88/92       (北交所)

AKShare's EastMoney endpoints want the bare six-digit code (e.g. ``600519``);
the exchange suffix (``600519.SH`` / ``SZ`` / ``BJ``) matters only for vendors
like xtdata. Chinese-name lookup (``贵州茅台`` -> ``600519``) needs the network,
so it lives behind :func:`resolve_name` and is never called from pure
normalization paths.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Six-digit A-share code, optionally with a broker-style suffix.
_ASHARE_CODE_RE = re.compile(
    r"^(?P<code>[0-9]{6})(?:\.(?P<exchange>SH|SZ|SS|BJ|sh|sz|ss|bj))?$"
)

# Chinese-name lookup pattern (pure CJK, or CJK+digits like 中航沈飞600716).
_CJK_NAME_RE = re.compile(r"^[\u4e00-\u9fff][\u4e00-\u9fff0-9A-Za-z]{0,19}$")

# Board table by code prefix -> (board key, exchange suffix, default limit %).
# ``limit_pct`` is the non-ST daily price limit in percent.
_BOARD_BY_PREFIX = [
    ("600", "main", "SH", 10.0),
    ("601", "main", "SH", 10.0),
    ("603", "main", "SH", 10.0),
    ("605", "main", "SH", 10.0),
    ("688", "star", "SH", 20.0),
    ("689", "star", "SH", 20.0),
    ("000", "main", "SZ", 10.0),
    ("001", "main", "SZ", 10.0),
    ("002", "main", "SZ", 10.0),  # ex-SME board, merged into SZ main in 2021
    ("003", "main", "SZ", 10.0),
    ("300", "chinext", "SZ", 20.0),
    ("301", "chinext", "SZ", 20.0),
    ("302", "chinext", "SZ", 20.0),
    ("430", "bse", "BJ", 30.0),
    ("83", "bse", "BJ", 30.0),
    ("87", "bse", "BJ", 30.0),
    ("88", "bse", "BJ", 30.0),
    ("92", "bse", "BJ", 30.0),
]

_BOARD_NAMES = {
    "main": "沪深主板",
    "star": "科创板",
    "chinext": "创业板",
    "bse": "北交所",
}

# Benchmark index codes for the reflection layer (AKShare convention: bare
# six-digit codes with the exchange implied).
INDEX_ALIASES = {
    "上证指数": "000001",
    "SHINDEX": "000001",
    "SH": "000001",
    "深证成指": "399001",
    "SZINDEX": "399001",
    "创业板指": "399006",
    "CHINEXT": "399006",
    "沪深300": "000300",
    "HS300": "000300",
    "CSI300": "000300",
    "中证500": "000905",
    "CSI500": "000905",
}


def looks_like_ashare_code(raw: str) -> bool:
    """True when ``raw`` is a six-digit A-share code, with or without suffix."""
    if not isinstance(raw, str):
        return False
    return _ASHARE_CODE_RE.match(raw.strip()) is not None


def parse_ashare_symbol(raw: str) -> dict | None:
    """Parse an A-share symbol into its components.

    Returns ``None`` when ``raw`` is not an A-share code. Accepts ``600519``,
    ``600519.SH``, ``sh600519``, ``SH600519``. The returned dict carries:

    - ``code``: bare six-digit code, e.g. ``"600519"``
    - ``exchange``: ``"SH"`` / ``"SZ"`` / ``"BJ"``
    - ``suffixed``: ``"600519.SH"`` (xtdata convention)
    - ``board``: ``main`` / ``star`` / ``chinext`` / ``bse``
    - ``board_name``: Chinese board name (沪深主板/科创板/创业板/北交所)
    - ``limit_pct``: non-ST daily price limit in percent
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip().upper()

    # ``sh600519`` style prefixes -> suffix form.
    if len(s) == 8 and s[:2] in ("SH", "SZ", "BJ"):
        s = f"{s[2:]}.{s[:2]}"

    m = _ASHARE_CODE_RE.match(s)
    if not m:
        return None
    code = m.group("code")

    board, exchange, limit_pct = None, None, None
    for prefix, b, exch, limit in _BOARD_BY_PREFIX:
        if code.startswith(prefix):
            board, exchange, limit_pct = b, exch, limit
            break

    if board is None:
        # A six-digit code that matches no known board prefix — treat as
        # unknown rather than guessing rules for it.
        logger.debug("Code %s matches no known A-share board prefix", code)
        return None

    # An explicit suffix wins over the prefix-derived exchange when both
    # exist and disagree only in case; a real conflict (600xxx.SZ) keeps the
    # suffix the vendor expects but logs loudly — the prefix table is the
    # authority for board rules.
    if m.group("exchange"):
        explicit = m.group("exchange").upper()
        if explicit == "SS":  # Yahoo-style Shanghai suffix
            explicit = "SH"
        if explicit != exchange:
            logger.warning(
                "Symbol %s: suffix %s conflicts with prefix board %s; using %s",
                s, m.group("exchange"), exchange, exchange,
            )

    return {
        "code": code,
        "exchange": exchange,
        "suffixed": f"{code}.{exchange}",
        "board": board,
        "board_name": _BOARD_NAMES[board],
        "limit_pct": limit_pct,
    }


def normalize_ashare_symbol(raw: str) -> str | None:
    """Return the bare six-digit AKShare code (``600519``) or None."""
    parsed = parse_ashare_symbol(raw)
    return parsed["code"] if parsed else None


def is_ashare_name(raw: str) -> bool:
    """True when ``raw`` looks like a Chinese stock name (贵州茅台)."""
    if not isinstance(raw, str):
        return False
    return _CJK_NAME_RE.match(raw.strip()) is not None


def resolve_name(name: str) -> str | None:
    """Resolve a Chinese stock name to its six-digit code via AKShare.

    Network-backed: fetches the full A-share spot table once per process and
    caches it (the table is ~5000 rows and ~1 MB; a watchlist run resolves
    many names against one fetch). Returns ``None`` when no exact or unique
    fuzzy match exists — callers decide how loud to be about a miss.
    """
    global _NAME_CACHE, _NAME_CACHE_AT
    import time as _time

    now = _time.monotonic()
    if _NAME_CACHE is None or now - _NAME_CACHE_AT > _NAME_CACHE_TTL:
        try:
            import akshare as ak

            spot = ak.stock_zh_a_spot_em()
            _NAME_CACHE = {
                str(row["名称"]).strip(): str(row["代码"]).strip()
                for _, row in spot.iterrows()
            }
            _NAME_CACHE_AT = now
            logger.info("A-share name table loaded: %d names", len(_NAME_CACHE))
        except Exception as exc:  # network failure must not crash a run
            logger.warning("Failed to load A-share name table: %s", exc)
            if _NAME_CACHE is None:
                return None

    key = name.strip()
    if key in _NAME_CACHE:
        return _NAME_CACHE[key]

    # Fuzzy: unique prefix/substring match (e.g. 茅台 -> 贵州茅台).
    hits = [code for n, code in _NAME_CACHE.items() if key in n]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        logger.info("Name %r is ambiguous (%d matches); no code returned", key, len(hits))
    return None


_NAME_CACHE: dict | None = None
_NAME_CACHE_AT: float = -1e12
_NAME_CACHE_TTL = 6 * 3600  # listings change rarely; half-day cache is plenty


def to_vendor_symbol(code: str, vendor: str = "akshare") -> str:
    """Format a bare code for a specific vendor.

    - ``akshare`` / ``em`` (EastMoney): bare code, ``600519``
    - ``xtdata`` / ``qmt``: suffixed, ``600519.SH``
    - ``tushare``: suffixed, ``600519.SH``
    """
    parsed = parse_ashare_symbol(code)
    if parsed is None:
        return code
    if vendor in ("xtdata", "qmt", "tushare"):
        return parsed["suffixed"]
    return parsed["code"]


def price_limit_for(code: str, prev_close: float, is_st: bool = False) -> tuple[float, float]:
    """Compute the daily price-limit band (lower, upper) for one code.

    ST status tightens the ±10% main-board band to ±5%; the 20%/30% boards
    (创业板/科创板/北交所) do not narrow for ST. A-share exchanges round the
    limit price to 0.01 CNY, so the band is what the venue itself publishes.
    """
    parsed = parse_ashare_symbol(code)
    if parsed is None:
        raise ValueError(f"{code!r} is not an A-share code")
    pct = parsed["limit_pct"]
    if is_st and parsed["board"] == "main":
        pct = 5.0
    lower = round(prev_close * (1 - pct / 100), 2)
    upper = round(prev_close * (1 + pct / 100), 2)
    return lower, upper
