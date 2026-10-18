"""Disk usage checks for long unattended runs."""

from __future__ import annotations

import shutil
from pathlib import Path


def dir_size_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def disk_status(path: Path, warn_free_gb: float = 10.0, crit_free_gb: float = 3.0) -> dict:
    path.mkdir(parents=True, exist_ok=True)
    u = shutil.disk_usage(path)
    free_gb = u.free / 1e9
    level = "CRITICAL" if free_gb < crit_free_gb else "WARNING" if free_gb < warn_free_gb else "OK"
    return {"free_gb": round(free_gb, 2), "total_gb": round(u.total / 1e9, 1), "level": level}
