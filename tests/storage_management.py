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


saved_agent_env = {name: os.environ.get(name) for name in ("TAUCETI_LOG_FILE", "CLAUDE_CONFIG_DIR", "CODEX_HOME")}
try:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "home"
        logdir = root / "logs"
        active = logdir / "work-active.log"
        expired = logdir / "agent-expired.log"
        recent = logdir / "agent-recent.log"
        claude_session = home / ".claude" / "projects" / "checkout" / "session.jsonl"
        claude_tool_result = home / ".claude" / "projects" / "checkout" / "run" / "tool-results" / "result.txt"
        claude_memory = home / ".claude" / "projects" / "checkout" / "memory" / "MEMORY.md"
        claude_creds = home / ".claude" / ".credentials.json"
        codex_session = home / ".codex" / "sessions" / "2026" / "08" / "session.jsonl"
        codex_history = home / ".codex" / "history.jsonl"
        codex_creds = home / ".codex" / "auth.json"
        write_allocated(active)
        write_allocated(expired)
        write_allocated(recent)
        for path in (
            claude_session,
            claude_tool_result,
            claude_memory,
            claude_creds,
            codex_session,
            codex_history,
            codex_creds,
        ):
            write_allocated(path)
        now = 2_000_000_000.0
        os.utime(active, (now - 30 * 86400, now - 30 * 86400))
        os.utime(expired, (now - 15 * 86400, now - 15 * 86400))
        os.utime(recent, (now - 13 * 86400, now - 13 * 86400))
        for path in (
            claude_session,
            claude_tool_result,
            claude_memory,
            claude_creds,
            codex_session,
            codex_history,
            codex_creds,
        ):
            os.utime(path, (now - 15 * 86400, now - 15 * 86400))
        os.environ["TAUCETI_LOG_FILE"] = str(active)
        os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        os.environ["CODEX_HOME"] = str(home / ".codex")
        cfg = SimpleNamespace(logdir=logdir, home=home)
        removed = tc.maintain_worker_logs(cfg, phase="test", now=now, max_bytes=1024**3)
        check(
            "14-day retention removes completed TauCeti and provider logs",
            removed == 5
            and not expired.exists()
            and not claude_session.exists()
            and not claude_tool_result.exists()
            and not codex_session.exists()
            and not codex_history.exists(),
        )
        check("active session log is protected regardless of age", active.exists())
        check("recent completed log is retained", recent.exists())
        check("Claude memory and credentials are excluded", claude_memory.exists() and claude_creds.exists())
        check("Codex credentials are excluded", codex_creds.exists())

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "home"
        logdir = root / "logs"
        active = logdir / "work-active.log"
        oldest = home / ".claude" / "projects" / "checkout" / "agent-oldest.jsonl"
        newest = logdir / "agent-newest.log"
        active_bytes = write_allocated(active)
        write_allocated(oldest)
        newest_bytes = write_allocated(newest)
        now = 2_000_000_000.0
        os.utime(oldest, (now - 100, now - 100))
        os.utime(newest, (now - 10, now - 10))
        os.environ["TAUCETI_LOG_FILE"] = str(active)
        os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        os.environ["CODEX_HOME"] = str(home / ".codex")
        cfg = SimpleNamespace(logdir=logdir, home=home)
        tc.maintain_worker_logs(
            cfg,
            phase="test",
            now=now,
            retention_days=14,
            max_bytes=active_bytes + newest_bytes,
        )
        check("combined volume cap removes the oldest provider log first", not oldest.exists())
        check("volume cap preserves newer completed output when sufficient", newest.exists())
        check("volume cap still preserves the active session", active.exists())
finally:
    for name, value in saved_agent_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


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
