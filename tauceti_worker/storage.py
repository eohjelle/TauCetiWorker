"""Bounded, round-safe storage maintenance for dedicated TauCeti workers."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .config import Config, log
from .constants import AGENT_LOG_MAX_BYTES, AGENT_LOG_RETENTION_DAYS
from .quota import claude_dir, codex_dir


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


def _is_disposable_claude_file(path: Path) -> bool:
    """Limit cleanup to Claude's reproducible session and diagnostic output."""
    parts = path.parts
    if not parts:
        return False
    if parts[0] in {"debug", "session-env", "sessions", "shell-snapshots"}:
        return True
    return parts[0] == "projects" and (path.suffix == ".jsonl" or "tool-results" in parts)


def _is_disposable_codex_file(path: Path) -> bool:
    """Limit cleanup to Codex's reproducible session and diagnostic output."""
    parts = path.parts
    if not parts:
        return False
    if parts[0] in {"archived_sessions", "log", "logs", "sessions", "shell_snapshots"}:
        return True
    return len(parts) == 1 and path.name in {"history.jsonl", "session_index.jsonl"}


def _disposable_agent_files(cfg: Config, *, phase: str) -> list[Path]:
    """Return only files safe to discard; credentials, config, memory, and ledgers are excluded."""
    roots = (
        (cfg.logdir, lambda _path: True),
        (claude_dir(cfg.home), _is_disposable_claude_file),
        (codex_dir(cfg.home), _is_disposable_codex_file),
    )
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root, disposable in roots:
        if not root.is_dir():
            continue
        try:
            paths = list(root.rglob("*"))
        except OSError as e:
            log(f"warning: could not inspect agent logs under {root} during {phase}: {e}")
            continue
        for path in paths:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if not disposable(path.relative_to(root)):
                    continue
                resolved = path.resolve(strict=False)
                if resolved not in seen:
                    seen.add(resolved)
                    candidates.append(path)
            except OSError as e:
                log(f"warning: could not inspect agent log {path} during {phase}: {e}")
    return candidates


def maintain_worker_logs(
    cfg: Config,
    *,
    phase: str,
    now: float | None = None,
    retention_days: int | None = None,
    max_bytes: int | None = None,
) -> int:
    """Expire and cap disposable agent logs at a safe round boundary.

    The loop driver's continuous session log is inherited by every round child through
    ``TAUCETI_LOG_FILE``.  It is never a deletion candidate, even if it alone exceeds the combined
    cap. Claude/Codex credentials, configuration, memory, and quota/ledger state are not candidates.
    Returns the number of completed files removed.
    """
    if retention_days is None:
        retention_days = _configured_positive_int("TAUCETI_LOG_RETENTION_DAYS", AGENT_LOG_RETENTION_DAYS, unit="days")
    if max_bytes is None:
        default_mib = max(1, AGENT_LOG_MAX_BYTES // 1024**2)
        max_mib = _configured_positive_int("TAUCETI_LOG_MAX_MIB", default_mib, unit="MiB")
        max_bytes = max_mib * 1024**2
    active = _active_log_path()
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    retained: list[tuple[float, Path, int, bool]] = []
    removed = 0
    removed_bytes = 0
    for path in _disposable_agent_files(cfg, phase=phase):
        try:
            stat = path.stat()
            allocated = stat.st_blocks * 512
            protected = active is not None and path.resolve(strict=False) == active
            if stat.st_mtime < cutoff and not protected:
                path.unlink()
                removed += 1
                removed_bytes += allocated
            else:
                retained.append((stat.st_mtime, path, allocated, protected))
        except FileNotFoundError:
            continue
        except OSError as e:
            log(f"warning: could not inspect or expire agent log {path} during {phase}: {e}")

    allocated_total = sum(allocated for _, _, allocated, _ in retained)
    if allocated_total > max_bytes:
        for _, path, allocated, protected in sorted(retained):
            if allocated_total <= max_bytes:
                break
            if protected:
                continue
            try:
                path.unlink()
                allocated_total = max(0, allocated_total - allocated)
                removed += 1
                removed_bytes += allocated
            except FileNotFoundError:
                allocated_total = max(0, allocated_total - allocated)
            except OSError as e:
                log(f"warning: could not remove completed agent log {path} during {phase}: {e}")
        if allocated_total > max_bytes:
            log(
                "warning: agent logs remain above their storage cap after removing every completed "
                f"log ({allocated_total / 1024**2:.1f} MiB allocated; "
                f"{max_bytes / 1024**2:.1f} MiB limit); "
                "the active session log was preserved"
            )
    if removed:
        log(
            f"agent log cleanup {phase}: removed {removed} completed file(s), "
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
