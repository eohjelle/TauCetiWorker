"""Bounded, round-safe storage maintenance for dedicated TauCeti workers."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import Config, Die, NoProgress, log
from .constants import AGENT_LOG_MAX_BYTES, AGENT_LOG_RETENTION_DAYS
from .quota import claude_dir, codex_dir


def _round_temp_dir() -> Path | None:
    """Temporary storage belongs exclusively to the single worker in the Compose container."""
    if os.environ.get("TAUCETI_CLEAN_ROUND_TMP") != "1":
        return None
    if not Path("/.dockerenv").is_file():
        raise Die("TAUCETI_CLEAN_ROUND_TMP requires a dedicated Docker worker container")
    return Path(tempfile.mkdtemp(prefix="tauceti-round-", dir="/tmp"))


def snapshot_round_temp() -> tuple[Path, set[Path]] | None:
    """Record pre-existing entries so a round can discard only its own temporary output."""
    root = _round_temp_dir()
    return None if root is None else (root, set(root.iterdir()))


def clean_round_temp(snapshot: tuple[Path, set[Path]]) -> int:
    """Remove newly created entries after the round's entire process group has exited.

    Pre-existing entries are preserved, including changes inside them. Symlinks are unlinked,
    never traversed. Persistent checkouts, caches, credentials and logs live outside this directory.
    """
    root, before = snapshot
    removed = 0
    removed_bytes = 0
    for path in sorted(set(root.iterdir()) - before):
        if path.is_symlink() or not path.is_dir():
            allocated = path.lstat().st_blocks * 512
            path.unlink()
        else:
            allocated = _allocated_tree_bytes(path)
            shutil.rmtree(path)
        removed += 1
        removed_bytes += allocated
    if removed:
        log(f"round temporary cleanup: removed {removed} entries, reclaimed {removed_bytes / 1024**2:.1f} MiB")
    if not any(root.iterdir()):
        root.rmdir()
    return removed


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


def _storage_reserve() -> int:
    return _configured_positive_int("TAUCETI_MIN_FREE_GIB", 8, unit="GiB") * 1024**3


def host_storage_free(checkout: Path, env=None) -> int:
    """A thin/compressed build filesystem must also preserve its physical backing disk."""
    env = os.environ if env is None else env
    paths = [checkout]
    if backing := env.get("TAUCETI_STORAGE_GUARD_PATH"):
        paths.append(Path(backing))
    return min(shutil.disk_usage(path).free for path in paths)


def start_host_storage_maintenance(cfg: Config):
    """Standalone Docker loops run recovery housekeeping even while quota blocks rounds."""
    if "TAUCETI_MIN_FREE_GIB" not in os.environ:
        return None
    from .work_recovery import housekeep

    stopping = threading.Event()

    def run():
        while not stopping.is_set():
            try:
                result = housekeep(cfg.state.parent, seconds=2, entries=16384, bytes=1024**3)
                if result.get("removed_bytes", 0):
                    log(f"recovery storage cleanup: reclaimed {result['removed_bytes'] / 1024**2:.1f} MiB")
            except Exception as error:
                log(f"warning: recovery storage cleanup failed: {error}")
            stopping.wait(30)

    thread = threading.Thread(target=run, name="recovery-storage", daemon=True)
    thread.start()
    return stopping, thread


def clean_lake_outputs(checkout: Path) -> None:
    """Remove generated outputs without invoking Elan or downloading a missing toolchain."""
    from .retention import fresh_report, remove
    from .work_recovery import live_users

    if live_users(checkout):
        raise NoProgress("Lake cleanup incomplete: checkout still has live users")
    report = fresh_report()
    packages = checkout / ".lake" / "packages"
    roots = [checkout]
    if packages.is_dir() and not packages.is_symlink():
        roots.extend(path for path in packages.iterdir() if path.is_dir() and not path.is_symlink())
    for root in roots:
        for name in ("build", "cache"):
            remove(root / ".lake" / name, checkout, report)
    if report["errors"] or report["protected"]:
        raise NoProgress(f"Lake cleanup incomplete: {report['errors'] or report['protected']}")


def maintain_host_lake_storage(cfg: Config, *, phase: str) -> bool:
    """Clean a quiescent checkout's builds and private Lake caches below the free-space reserve."""
    if "TAUCETI_MIN_FREE_GIB" not in os.environ or not cfg.checkout.is_dir():
        return False
    before = host_storage_free(cfg.checkout)
    if before >= _storage_reserve():
        return False
    clean_lake_outputs(cfg.checkout)
    from .build_caches import lake_cache
    from .retention import fresh_report, remove

    cache = lake_cache(cfg.wid, cfg.data_home)
    report = fresh_report()
    remove(cache, cache.parent, report)
    if report["errors"] or report["protected"]:
        raise NoProgress(f"Lake cache cleanup incomplete: {report['errors'] or report['protected']}")
    after = host_storage_free(cfg.checkout)
    log(f"Lake storage cleanup {phase}: free space {before / 1024**3:.1f} -> {after / 1024**3:.1f} GiB")
    return True


@contextlib.contextmanager
def guard_host_storage(proc, checkout: Path | None, env: dict, *, interval: float = 1.0):
    """Stop an owned build/agent group below the reserve; never delete live outputs."""
    if checkout is None or "TAUCETI_MIN_FREE_GIB" not in env:
        yield
        return
    reserve = int(env["TAUCETI_MIN_FREE_GIB"]) * 1024**3
    if reserve <= 0:
        raise Die("TAUCETI_MIN_FREE_GIB must be a positive integer")
    finished = threading.Event()
    reason = []

    def monitor():
        while not finished.wait(interval):
            free = host_storage_free(checkout, env)
            if free >= reserve or proc.poll() is not None:
                continue
            reason.append(f"disk pressure: {free / 1024**3:.1f} GiB free; {reserve / 1024**3:.0f} GiB required")
            log(reason[0] + " — stopping process group; source edits are preserved")
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            if not finished.wait(2):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            return

    thread = threading.Thread(target=monitor, daemon=True, name="storage-guard")
    thread.start()
    try:
        yield
    finally:
        finished.set()
        thread.join()
    if reason:
        raise NoProgress(reason[0])


def prune_obsolete_host_lean_toolchains(cfg: Config) -> None:
    """Retire unused official installations on an explicitly dedicated worker host."""
    from .constants import PRUNE_OBSOLETE_LEAN_TOOLCHAINS

    if not PRUNE_OBSOLETE_LEAN_TOOLCHAINS:
        return
    # Idle recovery source records retain lean-toolchain and can download it again when resumed.
    # Only current worker checkouts and live toolchain users require installed copies.
    files = {cfg.checkout / "lean-toolchain", *cfg.checkout.parent.parent.glob("*/TauCeti/lean-toolchain")}
    required = {path.read_text().strip() for path in files if path.is_file()}
    if not required:
        return
    listed = subprocess.run(["elan", "toolchain", "list"], capture_output=True, text=True, check=True)
    root = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))) / "toolchains"
    # A separate recovery/verification process may still be using an older installation.
    for executable in Path("/proc").glob("[0-9]*/exe"):
        try:
            if executable.resolve(strict=True).is_relative_to(root):
                return
        except OSError:
            continue
    from .work_recovery import live_users

    for line in listed.stdout.splitlines():
        name = line.split()[0] if line.split() else ""
        installation = root / name.replace("/", "--").replace(":", "---")
        if (
            name.startswith("leanprover/lean4:")
            and name not in required
            and not installation.is_symlink()
            and not live_users(installation)
        ):
            subprocess.run(["elan", "toolchain", "uninstall", name], check=True)
            log(f"uninstalled obsolete Lean toolchain {name}")


def verify_host_storage_headroom(checkout: Path, env: dict, *, phase: str) -> None:
    """Do not launch inference when restored artifacts leave less than the configured reserve."""
    if "TAUCETI_MIN_FREE_GIB" not in env:
        return
    from .agents import BuildCacheUnavailable

    reserve = int(env["TAUCETI_MIN_FREE_GIB"]) * 1024**3
    if reserve <= 0:
        raise Die("TAUCETI_MIN_FREE_GIB must be a positive integer")
    free = host_storage_free(checkout, env)
    log(f"host storage {phase}: {free / 1024**3:.1f} GiB free (floor {reserve / 1024**3:.0f} GiB)")
    if free < reserve:
        raise BuildCacheUnavailable(
            f"only {free / 1024**3:.1f} GiB free after restoration; {reserve / 1024**3:.0f} GiB required"
        )


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
        (claude_dir(cfg.data_home), _is_disposable_claude_file),
        (codex_dir(cfg.data_home), _is_disposable_codex_file),
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
    from .retention import open_files

    opened = open_files()
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    retained: list[tuple[float, Path, int, bool]] = []
    removed = 0
    removed_bytes = 0
    for path in _disposable_agent_files(cfg, phase=phase):
        try:
            stat = path.stat()
            allocated = stat.st_blocks * 512
            protected = (active is not None and path.resolve(strict=False) == active) or (
                opened is not None and (stat.st_dev, stat.st_ino) in opened
            )
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
        cache = cfg.data_home / ".cache" / "mathlib"
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
