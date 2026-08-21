"""xiadan.exe 进程守护：掉线自动拉起。

同花顺 universal 通道依赖 xiadan.exe 进程存活。进程崩溃/被误关时，
easytrader 的所有调用都会失败，止损与下单保护随之失效（P0-1 的告警
只负责「通知」，本模块负责「恢复」）。

恢复可行性：xiadan.exe 自身保存登录会话，独立拉起即可恢复交易窗口
（无需 hexin.exe 主程序在场），这正是本模块的价值。

行为::

    process_alive()   psutil 优先、tasklist 兜底；检测本身失败时按
                      「存活」处理（宁可漏拉起，不可误拉起）
    ensure_running()  进程在 → True；不在 → 冷却期内跳过，冷却期外
                      拉起并等待「窗口就绪」（成功 warning / 失败
                      critical 钉钉）

两个实测验过的坑都做了处理：

- 进程在 ≠ 窗口在：交易窗口约 6s 才出现，且被强杀后立刻重启会得到
  「无窗口僵尸进程」（占用单实例锁、不显示界面、数分钟后自行退出）。
  因此就绪判定看窗口而非进程；僵尸实例会被主动清理，冷却后重试。
- 冷却（默认 60s）防崩溃循环；easytrader 重连由 EasytraderBroker
  负责（会话过期时通知人工登录）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_PROCESS_NAME = "xiadan.exe"


class XiadanGuard:
    """单账号的 xiadan.exe 守护器。

    ``alive_fn`` / ``window_fn`` / ``launch_fn`` / ``kill_fn`` /
    ``sleep_fn`` / ``now_fn`` 均可注入，便于离线单元测试；默认实现
    面向真实 Windows 环境。
    """

    def __init__(
        self,
        exe_path: str,
        *,
        account_name: str = "easytrader",
        restart_cooldown_sec: float = 180.0,
        startup_timeout_sec: int = 30,
        settle_sec: float = 5.0,
        now_fn=None,
        sleep_fn=None,
        alive_fn=None,
        window_fn=None,
        launch_fn=None,
        kill_fn=None,
    ):
        self.exe_path = str(exe_path)
        self.account_name = account_name
        self.restart_cooldown_sec = max(0.0, float(restart_cooldown_sec))
        self.startup_timeout_sec = max(1, int(startup_timeout_sec))
        # 进程在 ≠ 窗口就绪：拉起后再等 settle 秒，否则 easytrader
        # connect 抢跑会拿不到 top_window（实测验过）。
        self.settle_sec = max(0.0, float(settle_sec))
        self._now_fn = now_fn or datetime.now
        self._sleep_fn = sleep_fn or time.sleep
        self._alive_fn = alive_fn
        self._window_fn = window_fn
        self._launch_fn = launch_fn
        self._kill_fn = kill_fn
        self._last_restart_ts: float | None = None

    # ── 进程检测 ──

    def process_alive(self) -> bool:
        """xiadan.exe 是否存活（按进程名匹配，不区分安装实例）。"""
        if self._alive_fn is not None:
            return bool(self._alive_fn())
        return _default_process_alive()

    # ── 守护入口 ──

    def ensure_running(self, context: str = "") -> bool:
        """进程不在时拉起；返回拉起后是否存活。

        冷却期内（上次拉起后 ``restart_cooldown_sec`` 秒内）不重复拉起，
        避免「启动即崩」的循环里每分钟刷一个进程 + 一条告警。
        """
        if self.process_alive():
            return True

        now_ts = self._now_fn().timestamp()
        if (
            self._last_restart_ts is not None
            and now_ts - self._last_restart_ts < self.restart_cooldown_sec
        ):
            logger.warning(
                "[%s] xiadan.exe down; relaunch suppressed by cooldown "
                "(last attempt %.0fs ago, %s)",
                self.account_name, now_ts - self._last_restart_ts, context,
            )
            return False

        self._last_restart_ts = now_ts
        logger.warning(
            "[%s] xiadan.exe not running — relaunching from %s (%s)",
            self.account_name, self.exe_path, context or "guard",
        )
        self._launch()
        if self._wait_until_ready():
            logger.info(
                "[%s] xiadan.exe relaunched and window ready",
                self.account_name,
            )
            self._notify(
                "xiadan.exe 已自动拉起",
                f"**账号**：{self.account_name}\n\n"
                f"**事件**：交易客户端进程掉线，已自动重新拉起（{context or '守护'}）。\n\n"
                "正在重连交易会话；若会话已过期会再收到一条需要人工登录的告警。",
                level="warning",
                key=f"xiadan-relaunch:{self.account_name}:{int(now_ts)}",
            )
            return True

        logger.error(
            "[%s] xiadan.exe relaunch failed: window not ready after %ds",
            self.account_name, self.startup_timeout_sec,
        )
        self._notify(
            "xiadan.exe 自动拉起失败",
            f"**账号**：{self.account_name}\n\n"
            f"**事件**：客户端进程掉线后自动拉起失败（等待 "
            f"{self.startup_timeout_sec}s 交易窗口未就绪；无窗口的僵尸实例"
            "已被清理）。\n\n"
            f"**路径**：{self.exe_path}\n\n"
            "**动作**：请检查同花顺安装目录是否变动、手动启动一次 xiadan.exe；"
            f"冷却 {self.restart_cooldown_sec:.0f}s 后会再次自动尝试。",
            level="critical",
            key=f"xiadan-relaunch-fail:{self.account_name}:{int(now_ts)}",
        )
        return False

    # ── 内部 ──

    def _launch(self) -> None:
        if self._launch_fn is not None:
            self._launch_fn(self.exe_path)
            return
        # GUI 程序：以安装目录为工作目录启动，进程独立于守护进程存活。
        subprocess.Popen(
            [self.exe_path], cwd=str(Path(self.exe_path).parent),
        )

    def _wait_until_ready(self) -> bool:
        """等待交易窗口就绪：进程在且能找到主窗口（进程在 ≠ 窗口在）。"""
        for _ in range(self.startup_timeout_sec):
            if self.process_alive() and self._has_main_window():
                if self.settle_sec > 0:
                    self._sleep_fn(self.settle_sec)   # 等窗口内容初始化完成
                return True
            self._sleep_fn(1)
        # 超时仍有进程无窗口 = 僵尸（占单实例锁、无界面）——清掉，
        # 本轮按失败处理，冷却期过后下一次拉起才有机会出正常窗口。
        if self.process_alive():
            self._kill_zombie()
        return False

    def _has_main_window(self) -> bool:
        """是否存在交易主窗口。

        与 easytrader connect 同款探测（win32 backend + top_window）：
        这里能找到窗口，easytrader 那边才连得上。无 pywinauto 时退化为
        进程级检测（此时僵尸判定不可用，但不至于误杀/误判就绪）。
        """
        if self._window_fn is not None:
            return bool(self._window_fn())
        try:
            import pywinauto
        except ImportError:
            return True
        try:
            app = pywinauto.Application().connect(path=self.exe_path, timeout=2)
            app.top_window()
            return True
        except Exception:  # noqa: BLE001 — 进程不在/无窗口都按「未就绪」
            return False

    def _kill_zombie(self) -> None:
        """清理无窗口的僵尸进程（强杀后立刻重启容易出现）。"""
        if self._kill_fn is not None:
            self._kill_fn()
            return
        try:
            import psutil
        except ImportError:
            return
        target = os.path.normcase(os.path.abspath(self.exe_path))
        for proc in psutil.process_iter(["name", "exe"]):
            name = (proc.info.get("name") or "").lower()
            proc_exe = proc.info.get("exe")
            # 按精确路径匹配，只清自己账号的 xiadan，绝不误杀别的安装实例
            if name != _PROCESS_NAME or not proc_exe:
                continue
            if os.path.normcase(os.path.abspath(proc_exe)) != target:
                continue
            try:
                proc.terminate()
                logger.warning(
                    "[%s] killed zombie xiadan.exe (pid %d, no window)",
                    self.account_name, proc.pid,
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _notify(self, title: str, text: str, *, level: str, key: str) -> None:
        try:
            from ..notifier import notify

            notify(title, text, level=level, key=key)
        except Exception:  # noqa: BLE001 — 通知失败不影响守护主流程
            logger.debug("guard notify failed", exc_info=True)


def _default_process_alive() -> bool:
    """真实环境检测：psutil 优先，tasklist 兜底；检测失败按存活处理。"""
    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name == _PROCESS_NAME:
                return True
        return False
    except ImportError:
        pass
    except Exception:  # psutil 本身异常（权限等）→ 按存活处理
        logger.debug("psutil scan failed", exc_info=True)
        return True

    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {_PROCESS_NAME}"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return _PROCESS_NAME in out.lower()
    except (OSError, subprocess.SubprocessError):
        logger.debug("tasklist check failed", exc_info=True)
        return True
