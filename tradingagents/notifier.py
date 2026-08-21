"""钉钉告警通知：风控事件 → 机器人 webhook。

设计约束：

- **绝不阻塞交易主流程**：发送失败只记日志；webhook 未配置时为 no-op。
- **防刷屏**：同类事件按 ``key`` 去重（默认 30 分钟内只发一次）——
  「数据源全挂」这类事件每个扫描周期都会触发，不去重会轰炸群聊。
- **事件分级**：``info``（止损执行等常规动作）/ ``warning``（数据源故障、
  大额待审批）/ ``critical``（日亏熔断、进程异常）。

配置（.env，钉钉群机器人安全设置选"加签"时两者都填）::

    TRADINGAGENTS_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
    TRADINGAGENTS_DINGTALK_SECRET=SECxxx        # 可选
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import threading
import time
import urllib.parse

logger = logging.getLogger(__name__)

_LEVEL_PREFIX = {"info": "✅", "warning": "⚠️", "critical": "🚨"}

# 同 key 事件的最小重发间隔（秒）
DEDUP_TTL_SECONDS = 30 * 60
_HTTP_TIMEOUT = 4.0

_dedup_lock = threading.Lock()
_dedup_cache: dict[str, float] = {}


def _webhook_settings() -> tuple[str | None, str | None]:
    """(webhook, secret)，从运行配置读取；未配置返回 (None, None)。"""
    from .default_config import DEFAULT_CONFIG

    return (
        DEFAULT_CONFIG.get("dingtalk_webhook"),
        DEFAULT_CONFIG.get("dingtalk_secret"),
    )


def _signed_url(webhook: str, secret: str) -> str:
    """加签 webhook：timestamp+secret 的 HmacSHA256 → base64 → urlencode。"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={timestamp}&sign={sign}"


def _deduped(key: str | None) -> bool:
    """True = 该 key 的事件在 TTL 内已发过，本次应跳过。"""
    if not key:
        return False
    now = time.time()
    with _dedup_lock:
        last = _dedup_cache.get(key)
        if last is not None and now - last < DEDUP_TTL_SECONDS:
            return True
        _dedup_cache[key] = now
        # 顺手清理过期项，防内存缓慢增长
        if len(_dedup_cache) > 128:
            cutoff = now - DEDUP_TTL_SECONDS
            for k in [k for k, ts in _dedup_cache.items() if ts < cutoff]:
                del _dedup_cache[k]
        return False


def notify(
    title: str,
    text: str,
    level: str = "info",
    key: str | None = None,
) -> bool:
    """发送一条钉钉 markdown 消息。返回是否实际发出（False=未配置/去重/失败）。

    ``key`` 用于同类事件去重（如 ``f"quote-fail:{symbol}:{date}"`）；
    ``level`` ∈ info / warning / critical，仅影响标题前缀。

    无论 webhook 是否配置，事件都会落盘到 ``notifications.jsonl``
    （UI 告警页的事件源；webhook 未配置时是唯一的告警记录）。
    """
    if level not in _LEVEL_PREFIX:
        level = "info"
    if _deduped(key):
        logger.debug("notify deduped: %s", key)
        return False

    _log_notification(title, text, level)

    webhook, secret = _webhook_settings()
    if not webhook:
        logger.debug("dingtalk webhook not configured — notify skipped: %s", title)
        return False

    url = _signed_url(webhook, secret) if secret else webhook
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"{_LEVEL_PREFIX[level]} {title}",
            "text": f"### {_LEVEL_PREFIX[level]} {title}\n\n{text}",
        },
    }
    try:
        import requests

        resp = requests.post(url, json=payload, timeout=_HTTP_TIMEOUT)
        data = resp.json() if resp.ok else {}
        if resp.ok and data.get("errcode") == 0:
            return True
        logger.warning(
            "dingtalk notify failed: http=%s body=%s", resp.status_code, data,
        )
    except Exception as exc:  # noqa: BLE001 — 通知失败绝不影响交易流程
        logger.warning("dingtalk notify error: %s", exc)
    return False


# ── 通知落盘（UI 告警页事件源） ────────────────────────────────────────────

_NOTIF_LOG_MAX_LINES = 2000


def _notification_log_path() -> str:
    import os

    from .default_config import DEFAULT_CONFIG

    base = os.path.join(str(DEFAULT_CONFIG.get("results_dir", ".")), "monitor")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "notifications.jsonl")


def _log_notification(title: str, text: str, level: str) -> None:
    """append 一条事件到 notifications.jsonl；失败只记日志。"""
    import json
    import os

    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "title": title,
        "text": text,
    }
    try:
        path = _notification_log_path()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # 简单轮转：超过上限时保留最新一半
        if os.path.getsize(path) > 512 * 1024:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-_NOTIF_LOG_MAX_LINES // 2:])
    except OSError as exc:
        logger.debug("notification log append failed: %s", exc)


def load_notification_history(limit: int = 100) -> list[dict]:
    """读取通知/告警历史（新→旧），供 UI 展示。"""
    import json

    try:
        with open(_notification_log_path(), encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, json.JSONDecodeError):
        return []
    return list(reversed(rows[-limit:]))


def reset_dedup_for_tests() -> None:
    with _dedup_lock:
        _dedup_cache.clear()
