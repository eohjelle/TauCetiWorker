#!/usr/bin/env python3
"""Storage cleanup is bounded, toolchain-aware, and safe around live worker output."""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


def write_allocated(path: Path, text: str = "log") -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path.stat().st_blocks * 512


saved_active = os.environ.get("TAUCETI_LOG_FILE")
try:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        logdir = root / "logs"
        active = logdir / "work-active.log"
        expired = logdir / "agent-expired.log"
        recent = logdir / "agent-recent.log"
        write_allocated(active)
        write_allocated(expired)
        write_allocated(recent)
        now = 2_000_000_000.0
        os.utime(active, (now - 30 * 86400, now - 30 * 86400))
        os.utime(expired, (now - 15 * 86400, now - 15 * 86400))
        os.utime(recent, (now - 13 * 86400, now - 13 * 86400))
        os.environ["TAUCETI_LOG_FILE"] = str(active)
        cfg = SimpleNamespace(logdir=logdir)
        removed = tc.maintain_worker_logs(cfg, phase="test", now=now, max_bytes=1024**3)
        check("14-day retention removes completed expired logs", removed == 1 and not expired.exists())
        check("active session log is protected regardless of age", active.exists())
        check("recent completed log is retained", recent.exists())

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        logdir = root / "logs"
        active = logdir / "work-active.log"
        oldest = logdir / "agent-oldest.log"
        newest = logdir / "agent-newest.log"
        active_bytes = write_allocated(active)
        write_allocated(oldest)
        newest_bytes = write_allocated(newest)
        now = 2_000_000_000.0
        os.utime(oldest, (now - 100, now - 100))
        os.utime(newest, (now - 10, now - 10))
        os.environ["TAUCETI_LOG_FILE"] = str(active)
        cfg = SimpleNamespace(logdir=logdir)
        tc.maintain_worker_logs(
            cfg,
            phase="test",
            now=now,
            retention_days=14,
            max_bytes=active_bytes + newest_bytes,
        )
        check("volume cap removes the oldest completed log first", not oldest.exists())
        check("volume cap preserves newer completed output when sufficient", newest.exists())
        check("volume cap still preserves the active session", active.exists())
finally:
    if saved_active is None:
        os.environ.pop("TAUCETI_LOG_FILE", None)
    else:
        os.environ["TAUCETI_LOG_FILE"] = saved_active


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cfg = SimpleNamespace(
        checkout=root / "checkout",
        state=root / "state",
        home=root / "home",
    )
    toolchain = cfg.checkout / "lean-toolchain"
    toolchain.parent.mkdir(parents=True)
    toolchain.write_text("leanprover/lean4:v4.old\n")
    download = cfg.home / ".cache" / "mathlib" / "old-download.tar"
    build = cfg.checkout / ".lake" / "build" / "TauCeti.olean"
    write_allocated(download, "compressed artifact")
    write_allocated(build, "warm output")

    check("first observed toolchain only establishes the cache marker", not tc.maintain_mathlib_download_cache(cfg))
    check("first observation preserves an existing Mathlib download", download.exists())
    check("unchanged toolchain preserves the Mathlib download cache", not tc.maintain_mathlib_download_cache(cfg))
    toolchain.write_text("leanprover/lean4:v4.new\n")
    check("toolchain change clears the Mathlib download cache", tc.maintain_mathlib_download_cache(cfg))
    check("toolchain cleanup removes compressed downloads", not download.exists())
    check("toolchain cleanup preserves expanded Lake builds", build.exists())
    marker = cfg.state / "cache" / "mathlib-cache-toolchain"
    check("toolchain marker advances only after cleanup", marker.read_text().strip() == "leanprover/lean4:v4.new")


journal_policy = (REPO / "deploy" / "tauceti-journald.conf").read_text()
check("journald policy caps host logs", "SystemMaxUse=250M" in journal_policy)
check("journald policy reserves recovery headroom", "SystemKeepFree=2G" in journal_policy)
prune_script = (REPO / "scripts" / "docker-storage-prune").read_text()
check("Docker cleanup never prunes volumes", "volume prune" not in prune_script and "--volumes" not in prune_script)
check("Docker cleanup requires a running worker", "ps --status running --quiet tauceti" in prune_script)
check("Docker cleanup verifies the deployed image", '[[ "$running_image" != "$tagged_image" ]]' in prune_script)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
