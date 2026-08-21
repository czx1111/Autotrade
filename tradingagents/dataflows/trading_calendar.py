"""A-share trading calendar: exchange holiday awareness with local caching.

The scheduler and intraday gates historically assumed Mon–Fri == trading day
(see scheduler.py's "known simplification"). National holidays (国庆 / 春节 /
清明 …) silently violated that: analysis ran, LLM budget burned, and paper
orders filled at stale Friday prices.

Data source: akshare ``tool_trade_date_hist_sina()`` (Sina's trade-date table,
includes the current year's holidays). The table is small (~30 years of dates,
< 300 KB as JSON) and changes at most a few times a year, so:

- cache to ``<data_cache_dir>/trading_days.json``;
- refresh at most once a week (or when today is missing from the table —
  Sina publishes next year's calendar around year end);
- any failure (network down, akshare missing) falls back to the last good
  cache, and to weekday-only heuristics as a last resort — never crash the
  trading loop because a calendar endpoint is flaky.

All functions are process-level cached (module globals): the calendar is
loaded at most once per process per day.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# Refresh the on-disk cache at most this often (calendar changes rarely).
_REFRESH_DAYS = 7
_CACHE_FILENAME = "trading_days.json"

_lock = threading.Lock()
# date-str set + the date it was last verified, module-level cache.
_trading_days: set[str] | None = None
_loaded_at: date | None = None


def _cache_path() -> str:
    from .config import get_config

    cache_dir = os.path.join(get_config()["data_cache_dir"])
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, _CACHE_FILENAME)


def _fetch_trading_days() -> set[str] | None:
    """Pull the trade-date table from akshare (Sina). None on any failure."""
    try:
        import akshare as ak
    except ImportError:
        logger.debug("akshare not installed — trading calendar unavailable")
        return None
    try:
        df = ak.tool_trade_date_hist_sina()
    except Exception as exc:  # noqa: BLE001 — calendar endpoint must never crash the loop
        logger.warning("trade-date fetch failed (%s) — falling back to cache/weekday", exc)
        return None
    if df is None or df.empty or "trade_date" not in df.columns:
        logger.warning("trade-date table empty/unexpected shape — falling back")
        return None
    # trade_date may be datetime64 or object(date); normalize to ISO date str.
    return {str(d)[:10] for d in df["trade_date"].tolist()}


def _load_calendar(force_refresh: bool = False) -> set[str]:
    """Return the cached trading-day set, refreshing when stale. Never raises."""
    global _trading_days, _loaded_at

    with _lock:
        today = date.today()
        if (
            not force_refresh
            and _trading_days is not None
            and _loaded_at == today
        ):
            return _trading_days

        path = _cache_path()
        cached: set[str] | None = None
        cache_age_days: float | None = None
        if os.path.exists(path):
            try:
                raw = json.loads(open(path, encoding="utf-8").read())
                cached = set(raw.get("days", []))
                cache_age_days = (datetime.now() - datetime.fromtimestamp(
                    os.path.getmtime(path))).days
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.warning("trading-days cache unreadable (%s) — refetching", exc)

        # 是否需要刷新：
        # - 无缓存 / 缓存过期（>= 7 天）；
        # - 缓存覆盖不到今天（年末新浪补下一年日历时）：today > 缓存最大日期。
        #   注意"今天不在缓存里"本身不是刷新条件——今天可能是节假日。
        covers_today = bool(cached) and today.isoformat() <= max(cached)
        need_refresh = (
            cached is None
            or not covers_today
            or cache_age_days is None
            or cache_age_days >= _REFRESH_DAYS
        )

        days = None
        if need_refresh:
            days = _fetch_trading_days()
            if days:
                try:
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump(
                            {"days": sorted(days), "updated_at": today.isoformat()},
                            fh,
                        )
                except OSError as exc:
                    logger.warning("trading-days cache write failed: %s", exc)
        if days is None:
            days = cached or set()
        if not days:
            logger.warning(
                "no trading calendar available — weekday heuristic only "
                "(holidays will NOT be filtered)"
            )
        _trading_days = days
        _loaded_at = today
        return _trading_days


def is_trading_day(d: date | datetime | str | None = None) -> bool:
    """True when ``d`` is an A-share trading day.

    Resolution order: exchange calendar → weekday heuristic (Mon–Fri) when the
    calendar is unavailable. Never raises.
    """
    if d is None:
        d = datetime.now()
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    if isinstance(d, datetime):
        d = d.date()

    days = _load_calendar()
    if days:
        return d.isoformat() in days
    return d.weekday() < 5


def is_trading_time_today(now: datetime | None = None) -> bool:
    """Session gate = trading phase × trading-day calendar.

    Composes the pure time-of-day check (rules.is_trading_time) with the
    holiday calendar, so holiday runs are blocked at every entry point
    (scheduler jobs, intraday scans, monitor sweeps) instead of only one.
    """
    from ..rules import is_trading_time

    now = now or datetime.now()
    return is_trading_time(now) and is_trading_day(now)


def next_trading_day(d: date | datetime | None = None, max_lookahead: int = 30) -> date | None:
    """Next exchange trading day strictly after ``d``; None past lookahead."""
    if d is None:
        d = date.today()
    if isinstance(d, datetime):
        d = d.date()
    for offset in range(1, max_lookahead + 1):
        cand = d + timedelta(days=offset)
        if is_trading_day(cand):
            return cand
    return None


def reset_cache_for_tests() -> None:
    """Clear module-level caches (test isolation only)."""
    global _trading_days, _loaded_at
    with _lock:
        _trading_days = None
        _loaded_at = None
