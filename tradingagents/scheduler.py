"""Trading-day scheduler for the A-share live loop.

The scheduler turns a "run one analysis" callable into a recurring daily
workflow that matches the A-share session (Asia/Shanghai time):

    phase        default time        what it triggers
    -----------  ------------------  -------------------------------------------
    pre_market   09:00 (Mon–Fri)     scan the watchlist, reset risk state, T+1
    intraday     09:30–11:30 /       re-run analysis + execution at a fixed
                 13:00–15:00         interval (``intraday_scan_interval_min``)
    post_market  15:30 (Mon–Fri)     settle, T+1 rollover, end-of-day review

``apscheduler`` is imported lazily so a one-shot analysis never pays its cost,
and every job body is wrapped so an exception in one run is logged, not raised
into the scheduler thread.

Holidays: production entry points (``run_auto.py``) inject the exchange
calendar via ``is_trading_day`` (see :mod:`tradingagents.dataflows.trading_calendar`,
Sina trade-date table with local caching). The default only filters weekends
and exists for isolated/standalone use where no calendar is wanted.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime

from .rules import is_trading_time, trading_phase

logger = logging.getLogger(__name__)

_ZH_TZ = "Asia/Shanghai"


def _default_is_trading_day(dt: datetime) -> bool:
    """Weekday-only filter (the scheduler cannot know exchange holidays)."""
    return dt.weekday() < 5


class TradingScheduler:
    """Recurring A-share trading workflow on top of APScheduler."""

    def __init__(
        self,
        run_phase,
        config: dict | None = None,
        is_trading_day=None,
        timezone: str = _ZH_TZ,
    ):
        """``run_phase(phase)`` is called with ``"pre_market"`` / ``"intraday"`` /
        ``"post_market"``; it is where the caller wires graph analysis, execution,
        and settlement. ``is_trading_day(datetime)`` filters holidays.
        """
        self.run_phase = run_phase
        self.is_trading_day = is_trading_day or _default_is_trading_day
        self.timezone = timezone

        config = config or {}
        schedule = config.get("trading_schedule", {})
        self.pre_market = schedule.get("pre_market", "09:00")
        self.post_market = schedule.get("post_market", "15:30")
        self.intraday_min = int(schedule.get("intraday_scan_interval_min", 60))
        self.monitor_min = int(schedule.get("monitor_interval_min", 5))

        self._scheduler = None

    # ── scheduling setup ──

    def _get_scheduler(self):
        if self._scheduler is None:
            try:
                from apscheduler.schedulers.background import BackgroundScheduler
            except ImportError as exc:
                raise RuntimeError(
                    "apscheduler is required for the live trading loop. "
                    "Install it with: pip install apscheduler"
                ) from exc
            self._scheduler = BackgroundScheduler(timezone=self.timezone)
        return self._scheduler

    def _job(self, phase: str) -> None:
        now = datetime.now()
        if not self.is_trading_day(now):
            logger.info("Skipping %s: not a trading day (%s)", phase, now.date())
            return
        try:
            self.run_phase(phase)
        except Exception:
            logger.exception("%s job failed", phase)

    def start(self) -> None:
        scheduler = self._get_scheduler()

        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        pre_h, pre_m = _parse_hhmm(self.pre_market)
        post_h, post_m = _parse_hhmm(self.post_market)

        scheduler.add_job(
            self._job, CronTrigger(
                day_of_week="mon-fri", hour=pre_h, minute=pre_m, timezone=self.timezone,
            ),
            args=["pre_market"], id="pre_market", name="盘前扫描",
            replace_existing=True,
        )
        scheduler.add_job(
            self._job, CronTrigger(
                day_of_week="mon-fri", hour=post_h, minute=post_m, timezone=self.timezone,
            ),
            args=["post_market"], id="post_market", name="盘后复盘",
            replace_existing=True,
        )
        scheduler.add_job(
            self._job,
            IntervalTrigger(minutes=max(1, self.intraday_min), timezone=self.timezone),
            args=["intraday"], id="intraday", name="盘中扫描",
            replace_existing=True,
        )
        if self.monitor_min > 0:
            scheduler.add_job(
                self._job,
                IntervalTrigger(minutes=self.monitor_min, timezone=self.timezone),
                args=["monitor"], id="monitor", name="盯盘巡检",
                replace_existing=True,
            )

        if not scheduler.running:
            scheduler.start()
        logger.info(
            "Trading scheduler started: pre_market=%s, intraday every %d min, "
            "monitor every %d min, post_market=%s",
            self.pre_market, self.intraday_min, self.monitor_min, self.post_market,
        )

    def stop(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Trading scheduler stopped")

    # ── introspection ──

    def list_jobs(self):
        scheduler = self._get_scheduler()
        return scheduler.get_jobs()


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    """Parse ``"HH:MM"`` into ``(hour, minute)`` ints."""
    hour, minute = hhmm.strip().split(":")
    return int(hour), int(minute)


# ── convenience: should the loop act right now? ──


def should_trade_now(now: datetime | None = None) -> bool:
    """True when the market is in an order-accepting phase.

    Wraps :func:`tradingagents.rules.is_trading_time` so the intraday interval
    job can cheaply decide whether to run a full analysis or skip a tick.
    """
    return is_trading_time(now)
