#!/usr/bin/env python3
"""Storage cleanup is bounded, toolchain-aware, and safe around live worker output."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


with patch.dict(os.environ, {"TAUCETI_CLEAN_ROUND_TMP": "0"}):
    check("temporary cleanup is disabled outside the Compose opt-in", tc.snapshot_round_temp() is None)

with patch.dict(os.environ, {"TAUCETI_CLEAN_ROUND_TMP": "1"}), patch.object(tc.storage, "Path") as path:
    path.return_value.is_file.return_value = False
    try:
        tc.snapshot_round_temp()
    except tc.Die:
        check("temporary cleanup refuses a non-Docker host", True)
    else:
        check("temporary cleanup refuses a non-Docker host", False)


def check_round_temp_cleanup(outcome):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "tmp"
        root.mkdir()
        existing = root / "existing" / "keep"
        outside = Path(td) / "persistent-checkout" / "keep"
        write_allocated(existing)
        write_allocated(outside)
        created = root / "review" / "source.lean"
        events = []

        def spawn(_tail, **kwargs):
            check("round receives its private temporary directory", kwargs["temporary"] == root)
            events.append("spawn")
            write_allocated(root / "spawn-output")
            return SimpleNamespace(pid=123456, returncode=-15, wait=wait)

        def wait(_timeout):
            events.append("wait")
            write_allocated(created)
            write_allocated(outside.parent / "concurrent-recovery")
            write_allocated(root / "existing" / "new-but-shared")
            (root / "outside-link").symlink_to(outside.parent, target_is_directory=True)
            (created.parent / "nested-link").symlink_to(outside.parent, target_is_directory=True)
            check(f"{outcome}: temporary files survive while the round is running", created.exists())
            if outcome == "timeout":
                raise subprocess.TimeoutExpired("round", 1)
            if outcome == "interrupt":
                raise KeyboardInterrupt
            return 1 if outcome == "failure" else 0

        def reap(_pgid):
            events.append("reap")
            check(f"{outcome}: process teardown precedes file cleanup", created.exists())

        group_state = "sent" if outcome == "survivor" else "denied" if outcome == "denied" else "gone"
        with (
            patch.object(tc.storage, "_round_temp_dir", return_value=root),
            patch.object(tc.round, "spawn_round", side_effect=spawn),
            patch.object(tc.round, "kill_round_group", side_effect=lambda _p: events.append("kill")),
            patch.object(tc.round, "reap_round_group", side_effect=reap),
            patch.object(tc.round, "signal_group", return_value=group_state),
        ):
            try:
                rc = tc.run_round_subprocess([], timeout=1)
            except KeyboardInterrupt:
                check("interruption is propagated after cleanup", outcome == "interrupt")
            else:
                expected = 124 if outcome == "timeout" else 1 if outcome == "failure" else 0
                check(f"{outcome}: round exit status is preserved", rc == expected)

        if outcome in ("survivor", "denied"):
            check(f"{outcome}: possibly active temporary files are preserved", created.exists())
        else:
            check(f"{outcome}: completed temporary trees are removed", not created.parent.exists())
            check(f"{outcome}: top-level temporary files are removed", not (root / "spawn-output").exists())
            check(f"{outcome}: new symlinks are unlinked", not (root / "outside-link").is_symlink())
        check(f"{outcome}: pre-existing entries and their contents are preserved", existing.exists())
        check(
            f"{outcome}: new files within pre-existing entries are preserved",
            (root / "existing" / "new-but-shared").exists(),
        )
        check(f"{outcome}: symlink targets are preserved", outside.exists())
        check(f"{outcome}: concurrent recovery output is preserved", (outside.parent / "concurrent-recovery").exists())
        expected_events = (
            ["spawn", "wait", "kill", "reap"] if outcome in ("timeout", "interrupt") else ["spawn", "wait", "reap"]
        )
        check(f"{outcome}: cleanup follows round teardown", events == expected_events)


for outcome in ("success", "failure", "timeout", "interrupt", "survivor", "denied"):
    check_round_temp_cleanup(outcome)


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
        cfg = SimpleNamespace(logdir=logdir, data_home=home)
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
        cfg = SimpleNamespace(logdir=logdir, data_home=home)
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
        data_home=root / "home",
    )
    toolchain = cfg.checkout / "lean-toolchain"
    toolchain.parent.mkdir(parents=True)
    toolchain.write_text("leanprover/lean4:v4.old\n")
    download = cfg.data_home / ".cache" / "mathlib" / "old-download.tar"
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


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cfg = SimpleNamespace(checkout=root / "checkout", state=root / "state", data_home=root / "home")
    cached = cfg.checkout / ".lake" / "cache" / "artifact"
    private_cache = cfg.data_home / ".cache" / "lake" / "artifact"
    mathlib = cfg.data_home / ".cache" / "mathlib" / "download"
    for path in (cached, private_cache, mathlib):
        write_allocated(path)
    gib = 1024**3
    with (
        patch.dict(os.environ, {"TAUCETI_MIN_FREE_GIB": "5"}),
        patch.object(tc.storage.shutil, "disk_usage", return_value=SimpleNamespace(free=6 * gib)),
        patch.object(tc.storage.subprocess, "run") as run,
    ):
        check("ample free space preserves warm builds", not tc.storage.maintain_host_lake_storage(cfg, phase="test"))
        run.assert_not_called()
    with (
        patch.dict(os.environ, {"TAUCETI_MIN_FREE_GIB": "5"}),
        patch.object(
            tc.storage.shutil, "disk_usage", side_effect=[SimpleNamespace(free=4 * gib), SimpleNamespace(free=9 * gib)]
        ),
        patch.object(tc.storage.subprocess, "run") as run,
    ):
        check("low free space cleans Lake outputs", tc.storage.maintain_host_lake_storage(cfg, phase="test"))
        run.assert_called_once_with(["lake", "clean"], cwd=cfg.checkout, check=True)
    check("low-space cleanup deletes both private artifact stores", not cached.exists() and not private_cache.exists())
    check("Mathlib downloads remain available for restoration", mathlib.exists())
    with patch.object(tc.storage.shutil, "disk_usage", return_value=SimpleNamespace(free=4 * gib)):
        try:
            tc.storage.verify_host_storage_headroom(cfg.checkout, {"TAUCETI_MIN_FREE_GIB": "5"}, phase="test")
        except tc.agents.BuildCacheUnavailable:
            check("inference is withheld when restoration leaves too little space", True)
        else:
            check("inference is withheld when restoration leaves too little space", False)
    (cfg.checkout / "lean-toolchain").write_text("leanprover/lean4:v4.new\n")
    with (
        patch("tauceti_worker.constants.PRUNE_OBSOLETE_LEAN_TOOLCHAINS", True),
        patch.dict(os.environ, {"ELAN_HOME": str(root / "elan")}),
        patch.object(
            tc.storage.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="leanprover/lean4:v4.old\nleanprover/lean4:v4.new\ncustom\n"),
        ) as run,
    ):
        tc.storage.prune_obsolete_host_lean_toolchains(cfg)
        check(
            "only the obsolete official toolchain is pruned",
            run.call_count == 2
            and run.call_args.args[0] == ["elan", "toolchain", "uninstall", "leanprover/lean4:v4.old"],
        )
        write_allocated(cfg.state / "recovery" / "unfinished")
        run.reset_mock()
        tc.storage.prune_obsolete_host_lean_toolchains(cfg)
        check("retained recovery work protects older toolchains", not run.called)


journal_policy = (REPO / "deploy" / "tauceti-journald.conf").read_text()
check("journald policy caps host logs", "SystemMaxUse=250M" in journal_policy)
check("journald policy reserves recovery headroom", "SystemKeepFree=2G" in journal_policy)
prune_script = (REPO / "scripts" / "docker-storage-prune").read_text()
check("Docker cleanup never prunes volumes", "volume prune" not in prune_script and "--volumes" not in prune_script)
check("Docker cleanup requires a running worker", "ps --status running --quiet tauceti" in prune_script)
check("Docker cleanup verifies the deployed image", '[[ "$running_image" != "$tagged_image" ]]' in prune_script)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
