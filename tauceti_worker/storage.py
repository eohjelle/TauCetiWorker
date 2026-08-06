"""Bounded, round-safe storage maintenance for dedicated TauCeti workers."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .config import Config, log
from .constants import WORKER_LOG_MAX_BYTES, WORKER_LOG_RETENTION_DAYS


def _configured_positive_int(name: str, default: int, *, unit: str) -> int:
    """Read a positive whole-number setting, retaining a safe default on invalid input."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        log(f"warning: ignoring invalid {name}={raw!r}; expected a positive whole number of {unit}")
        return default
    return value


def _allocated_tree_bytes(root: Path) -> int:
    """Return allocated bytes under ``root``, counting hard-linked inodes only once."""
    total = 0
    seen: set[tuple[int, int]] = set()
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            stat = (Path(dirpath) / filename).lstat()
            inode = (stat.st_dev, stat.st_ino)
            if inode in seen:
                continue
            seen.add(inode)
            total += stat.st_blocks * 512
    return total


def _active_log_path() -> Path | None:
    raw = os.environ.get("TAUCETI_LOG_FILE")
    if not raw:
        return None
    try:
        return Path(raw).resolve(strict=False)
    except OSError:
        return Path(raw).absolute()


def maintain_worker_logs(
    cfg: Config,
    *,
    phase: str,
    now: float | None = None,
    retention_days: int | None = None,
    max_bytes: int | None = None,
) -> int:
    """Expire and cap completed logs at a safe round boundary.

    The loop driver's continuous session log is inherited by every round child through
    ``TAUCETI_LOG_FILE``.  It is never a deletion candidate, even if it alone exceeds the cap.
    Returns the number of completed files removed.
    """
    logdir = cfg.logdir
    if not logdir.is_dir():
        return 0
    if retention_days is None:
        retention_days = _configured_positive_int("TAUCETI_LOG_RETENTION_DAYS", WORKER_LOG_RETENTION_DAYS, unit="days")
    if max_bytes is None:
        default_gib = max(1, WORKER_LOG_MAX_BYTES // 1024**3)
        max_gib = _configured_positive_int("TAUCETI_LOG_MAX_GIB", default_gib, unit="GiB")
        max_bytes = max_gib * 1024**3
    active = _active_log_path()
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    completed: list[tuple[float, Path, int]] = []
    removed = 0
    removed_bytes = 0
    try:
        candidates = list(logdir.rglob("*"))
    except OSError as e:
        log(f"warning: could not inspect worker logs during {phase}: {e}")
        return 0

    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if active is not None and path.resolve(strict=False) == active:
                continue
            stat = path.stat()
            allocated = stat.st_blocks * 512
            if stat.st_mtime < cutoff:
                path.unlink()
                removed += 1
                removed_bytes += allocated
            else:
                completed.append((stat.st_mtime, path, allocated))
        except OSError as e:
            log(f"warning: could not inspect or expire worker log {path} during {phase}: {e}")

    try:
        allocated_total = _allocated_tree_bytes(logdir)
    except OSError as e:
        log(f"warning: could not measure worker logs during {phase}: {e}")
        allocated_total = 0
    if allocated_total > max_bytes:
        for _, path, allocated in sorted(completed):
            if allocated_total <= max_bytes:
                break
            try:
                path.unlink()
                allocated_total = max(0, allocated_total - allocated)
                removed += 1
                removed_bytes += allocated
            except OSError as e:
                log(f"warning: could not remove completed worker log {path} during {phase}: {e}")
        if allocated_total > max_bytes:
            log(
                "warning: worker logs remain above their storage cap after removing every completed "
                f"log ({allocated_total / 1024**3:.1f} GiB allocated; "
                f"{max_bytes / 1024**3:.1f} GiB limit); "
                "the active session log was preserved"
            )
    if removed:
        log(
            f"worker log cleanup {phase}: removed {removed} completed file(s), "
            f"reclaimed {removed_bytes / 1024**2:.1f} MiB"
        )
    return removed


def maintain_mathlib_download_cache(cfg: Config) -> bool:
    """Drop Mathlib's compressed download cache when the checkout changes Lean toolchain.

    Expanded ``.lake/build`` trees stay warm.  Only ``~/.cache/mathlib`` is invalidated, and the
    first observed toolchain merely establishes the marker so an upgrade does not masquerade as one.
    """
    toolchain_file = cfg.checkout / "lean-toolchain"
    marker = cfg.state / "cache" / "mathlib-cache-toolchain"
    try:
        requested = toolchain_file.read_text().strip()
        if not requested:
            return False
        previous = marker.read_text().strip() if marker.is_file() else None
        if previous == requested:
            return False
        cache = cfg.home / ".cache" / "mathlib"
        removed_bytes = _allocated_tree_bytes(cache) if previous and cache.is_dir() else 0
        if previous and cache.exists():
            shutil.rmtree(cache)
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(requested + "\n")
        os.replace(temporary, marker)
    except OSError as e:
        log(f"warning: could not maintain the Mathlib download cache: {e}")
        return False
    if previous:
        log(
            "Mathlib download cache cleared after Lean toolchain change "
            f"({previous} -> {requested}; reclaimed {removed_bytes / 1024**2:.1f} MiB)"
        )
        return True
    return False
