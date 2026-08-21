"""UI 配置存储：accounts.json 读写 + 报告/日报文件列举。

与 :mod:`tradingagents.auto_trader` 共用同一份 accounts.json，
UI 改自选股 → 自动交易循环下一轮生效；反之亦然。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNTS_PATH = Path("accounts.json")


def load_accounts(path: Path | str = DEFAULT_ACCOUNTS_PATH) -> list[dict]:
    """读取账号配置；无文件时返回单账号 paper 默认（与 run_auto.py 一致）。"""
    path = Path(path)
    if not path.exists():
        return [{
            "name": "paper-default",
            "broker_settings": {"broker": "paper"},
            "watchlist": [],
            "screening_enabled": False,
        }]
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("accounts", [])
    return data


def save_accounts(accounts: list[dict], path: Path | str = DEFAULT_ACCOUNTS_PATH) -> None:
    Path(path).write_text(
        json.dumps({"accounts": accounts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def set_watchlist(accounts: list[dict], account_name: str, symbols: list[str]) -> list[dict]:
    """更新指定账号的自选股（返回新列表，不落盘）。"""
    out = []
    for account in accounts:
        if account.get("name") == account_name:
            account = dict(account)
            account["watchlist"] = symbols
        out.append(account)
    return out


def list_daily_summaries(results_dir: str | Path) -> list[Path]:
    """自动交易日报文件（results_dir/auto/*.md），新→旧。"""
    folder = Path(results_dir) / "auto"
    if not folder.exists():
        return []
    return sorted(folder.glob("*.md"), reverse=True)


def list_analysis_reports(results_dir: str | Path, limit: int = 50) -> list[Path]:
    """历史 AI 分析报告目录（results_dir/reports/<ticker>_<stamp>/），新→旧。"""
    folder = Path(results_dir) / "reports"
    if not folder.exists():
        return []
    runs = [d for d in folder.iterdir() if (d / "complete_report.md").exists()]
    return sorted(runs, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


# ── 净值历史（仪表盘收益曲线） ────────────────────────────────────────────


def _equity_file(results_dir: str | Path, account: str) -> Path:
    return Path(results_dir) / "equity" / f"{account}.json"


def load_equity_history(results_dir: str | Path, account: str) -> list[dict]:
    """读取账号净值序列：[{date, total_asset, cash, market_value}]，旧→新。"""
    path = _equity_file(results_dir, account)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def append_equity_point(results_dir: str | Path, account: str, point: dict) -> None:
    """追加一个净值点（同日覆盖）。由盘后复盘 / 手动复盘调用。"""
    path = _equity_file(results_dir, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    history = load_equity_history(results_dir, account)
    history = [p for p in history if p.get("date") != point.get("date")]
    history.append(point)
    history.sort(key=lambda p: p.get("date", ""))
    path.write_text(json.dumps(history[-400:], ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ── UI 设置（分析师选择 / 分析模式，Studio 与 Config 页共用） ────────────


def _settings_file(results_dir: str | Path) -> Path:
    return Path(results_dir) / "ui_settings.json"


def load_ui_settings(results_dir: str | Path) -> dict:
    path = _settings_file(results_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_ui_settings(results_dir: str | Path, settings: dict) -> None:
    path = _settings_file(results_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2),
                    encoding="utf-8")
