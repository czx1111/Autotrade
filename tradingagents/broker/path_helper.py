"""Broker installation path auto-detection and resolution utilities.

For 同花顺 (THS) and miniQMT, the user only needs to provide the
**installation directory** (e.g. ``D:\\ths\\同花顺``).  This module resolves
that to the exact executable / userdata path the broker SDK expects.

It also provides ``auto_detect_*`` helpers that scan common install locations
so the user can skip manual path entry entirely.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

__all__ = [
    "resolve_ths_xiadan",
    "auto_detect_ths_install",
    "resolve_qmt_userdata",
    "auto_detect_qmt_install",
]

# ── 同花顺 (THS) helpers ──

_THS_XIADAN = "xiadan.exe"

# Common install paths checked by auto_detect_ths_install.
# Patterns with {*} are globbed one level deep.
_THS_COMMON_PATHS = [
    # User-specific directories
    os.path.expanduser("~\\ths\\同花顺"),
    os.path.expanduser("~\\同花顺软件\\同花顺"),
    # Common drive letters
    "C:\\同花顺软件\\同花顺",
    "C:\\ths\\同花顺",
    "D:\\同花顺软件\\同花顺",
    "D:\\ths\\同花顺",
    "D:\\同花顺",
    "E:\\同花顺软件\\同花顺",
    "E:\\ths\\同花顺",
    "E:\\同花顺",
    # Program Files variants (less common for THS but worth checking)
    os.environ.get("ProgramFiles", "C:\\Program Files") + "\\同花顺\\同花顺",
    os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)") + "\\同花顺\\同花顺",
]


def resolve_ths_xiadan(path: str | None) -> str | None:
    """Resolve a user-provided path to the THS xiadan.exe executable.

    Accepts three forms:
    1. **Directory** — appends ``xiadan.exe`` (e.g. ``D:\\ths\\同花顺`` →
       ``D:\\ths\\同花顺\\xiadan.exe``).
    2. **Direct exe path** — returned as-is (e.g. ``D:\\ths\\同花顺\\xiadan.exe``).
    3. **None** — returns None (caller decides what to do).

    Returns the resolved absolute path if the file exists, otherwise returns
    the *best-effort* path so callers can raise a helpful error.
    """
    if not path:
        return None
    path = os.path.abspath(path)
    # If already pointing at xiadan.exe
    if os.path.isfile(path) and path.lower().endswith(".exe"):
        return path
    # Treat as directory, look for xiadan.exe inside
    if os.path.isdir(path):
        candidate = os.path.join(path, _THS_XIADAN)
        if os.path.isfile(candidate):
            return candidate
        # Also check common sub-folders some installers use
        for subdir in ("", "vipdoc", "T0002"):
            nested = os.path.join(path, subdir, _THS_XIADAN)
            if os.path.isfile(nested):
                return nested
        # Return the expected path even if not found yet — THS might not be
        # fully installed yet, but the user pointed at the right directory.
        return candidate
    # Neither file nor dir — return as-is and let easytrader raise the error.
    return path


def auto_detect_ths_install() -> list[str]:
    """Scan common THS installation directories and return paths that
    contain ``xiadan.exe``.

    Returns a list of **directories** (not the exe path) that look valid,
    ordered by likelihood.
    """
    found: list[str] = []
    for candidate in _THS_COMMON_PATHS:
        if not candidate:
            continue
        candidate = os.path.abspath(candidate)
        if os.path.isfile(os.path.join(candidate, _THS_XIADAN)):
            found.append(candidate)
    # Also do a shallow search under known parent directories
    for parent in ("C:\\", "D:\\", "E:\\"):
        try:
            for entry in os.scandir(parent):
                if not entry.is_dir():
                    continue
                name_lower = entry.name.lower()
                # Match directories like "ths", "同花顺软件", "同花顺"
                if any(kw in name_lower for kw in ("ths", "同花顺")):
                    sub = os.path.join(entry.path, "同花顺")
                    if os.path.isfile(os.path.join(sub, _THS_XIADAN)):
                        if sub not in found:
                            found.append(sub)
        except (PermissionError, OSError):
            continue
    return found


# ── miniQMT helpers ──

_QMT_UD_SUFFIX = "userdata_mini"

# Common install patterns.
_QMT_COMMON_PATHS: list[str] = []
# We'll build these at module level from the drives we can see.
for _drive in ("C", "D", "E", "F"):
    _QMT_COMMON_PATHS.extend([
        f"{_drive}:\\国金证券QMT交易端",
        f"{_drive}:\\国金证券QMT交易端\\userdata_mini",
        f"{_drive}:\\QMT交易端",
        f"{_drive}:\\QMT交易端\\userdata_mini",
        f"{_drive}:\\miniQMT",
        f"{_drive}:\\miniQMT\\userdata_mini",
    ])


def resolve_qmt_userdata(path: str | None) -> str | None:
    """Resolve a user-provided path to the miniQMT ``userdata_mini`` directory.

    Accepts:
    1. **QMT install directory** — appends ``userdata_mini`` (e.g.
       ``D:\\国金证券QMT交易端`` → ``D:\\国金证券QMT交易端\\userdata_mini``).
    2. **Direct userdata_mini path** — returned as-is.
    3. **None** — returns None.

    Returns the resolved path if the directory exists, or a best-effort path
    for error reporting.
    """
    if not path:
        return None
    path = os.path.abspath(path)
    # If already pointing at userdata_mini and it exists
    if os.path.isdir(path) and path.lower().endswith(_QMT_UD_SUFFIX.lower()):
        return path
    # Treat as install directory, look for userdata_mini inside
    if os.path.isdir(path):
        candidate = os.path.join(path, _QMT_UD_SUFFIX)
        if os.path.isdir(candidate):
            return candidate
        return candidate
    # Not a directory — return as-is for the error message.
    return path


def auto_detect_qmt_install() -> list[str]:
    """Scan common QMT installation directories and return paths that
    contain ``userdata_mini``.

    Returns a list of **QMT install directories** (parent of userdata_mini),
    ordered by likelihood.
    """
    found: list[str] = []
    for candidate in _QMT_COMMON_PATHS:
        if not candidate:
            continue
        candidate = os.path.abspath(candidate)
        if candidate.lower().endswith(_QMT_UD_SUFFIX.lower()):
            parent = os.path.dirname(candidate)
            if os.path.isdir(candidate) and parent not in found:
                found.append(parent)
        elif os.path.isdir(os.path.join(candidate, _QMT_UD_SUFFIX)):
            if candidate not in found:
                found.append(candidate)
    return found
