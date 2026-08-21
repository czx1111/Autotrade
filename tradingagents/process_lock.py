"""Per-account process locks: prevent double-running trading daemons.

Running two ``run_auto.py`` instances against the same account doubles the
risk budget (each process keeps its own in-memory order counters), splits
T+1/position views, and races on the approval-store JSON. OS-level file
locks (``filelock``) auto-release when the holder dies, so a crashed daemon
never leaves a stale lock behind.

Scope: only long-lived trading loops take the lock. Read-only UI access
(positions page, reports) stays lock-free.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_LOCK_DIR = os.path.join(os.path.expanduser("~"), ".tradingagents", "locks")


def _lock_path(account_name: str) -> str:
    safe = re.sub(r"[^\w-]+", "_", account_name.strip()) or "default"
    os.makedirs(_LOCK_DIR, exist_ok=True)
    return os.path.join(_LOCK_DIR, f"{safe}.lock")


class AccountProcessLock:
    """Advisory exclusive lock for one account's trading loop.

    Usage::

        lock = AccountProcessLock("平安证券")
        if not lock.acquire():
            raise SystemExit(lock.conflict_message)
        try:
            ...  # daemon loop
        finally:
            lock.release()
    """

    def __init__(self, account_name: str, timeout: float = 1.0):
        self.account = account_name
        self.timeout = timeout
        self._lock = None
        self.conflict_message = ""

    def acquire(self) -> bool:
        """Try to take the lock. False = another live daemon holds it."""
        from filelock import FileLock, Timeout

        lock = FileLock(_lock_path(self.account), timeout=self.timeout)
        try:
            lock.acquire()
        except Timeout:
            self.conflict_message = (
                f"账号 {self.account!r} 已有另一个交易进程在运行"
                f"（锁文件 {lock.lock_file}）。同一账号禁止双开："
                "风控预算与审批状态会互相覆盖。"
            )
            logger.error(self.conflict_message)
            return False
        except OSError as exc:
            # 锁文件不可写（磁盘满/权限）：放行但大声记录——
            # 宁可重复告警也不能让交易循环整体停摆。
            logger.warning("process lock unavailable for %s (%s) — continuing without", self.account, exc)
            return True
        self._lock = lock
        return True

    def release(self) -> None:
        if self._lock is not None:
            try:
                self._lock.release()
            except OSError as exc:
                logger.warning("lock release failed for %s: %s", self.account, exc)
            self._lock = None
