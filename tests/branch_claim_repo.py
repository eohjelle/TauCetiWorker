#!/usr/bin/env python3
"""Maintenance branch claims use the PR head repository for their whole lifecycle.

The claim remains cooperative and fail-open; git-safe-push's branch CAS is still the hard guard.
These tests stub only process execution, so they can pin repository selection without touching
GitHub.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_worker import round as round_mod  # noqa: E402


class FakeContext:
    def __init__(self):
        self.cleanups = []

    def add_cleanup(self, fn):
        self.cleanups.append(fn)


class Harness:
    def __init__(self, acquire_results, override=None):
        self.acquire_results = iter(acquire_results)
        self.acquired_from = []
        self.released_from = []
        self.heartbeats = []
        self.saved_env = os.environ.copy()
        self.real_run = round_mod.subprocess.run
        self.real_heartbeat = round_mod.Claims.start_heartbeat

        for key in (
            "CLAIM_REPO",
            "TAUCETI_CLAIM_KEY",
            "TAUCETI_CLAIM_REPO",
            "TAUCETI_CLAIM_SH",
            "TAUCETI_PUSH_EXPECT",
            "TAUCETI_PUSH_REF",
            "TAUCETI_PUSH_REMOTE",
        ):
            os.environ.pop(key, None)
        if override is not None:
            os.environ["CLAIM_REPO"] = override

        def fake_run(cmd, *, capture_output=False, env=None, **_kwargs):
            assert capture_output
            claim_repo = env["CLAIM_REPO"]
            if cmd[1] == "acquire":
                self.acquired_from.append(claim_repo)
                return SimpleNamespace(returncode=next(self.acquire_results))
            if cmd[1] == "release":
                self.released_from.append(claim_repo)
                return SimpleNamespace(returncode=0)
            raise AssertionError(f"unexpected claim command: {cmd}")

        def fake_heartbeat(_claims, key, claim_repo):
            self.heartbeats.append((key, claim_repo))

        round_mod.subprocess.run = fake_run
        round_mod.Claims.start_heartbeat = fake_heartbeat
        self.claims = round_mod.Claims(SimpleNamespace(), FakeContext())

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        round_mod.subprocess.run = self.real_run
        round_mod.Claims.start_heartbeat = self.real_heartbeat
        os.environ.clear()
        os.environ.update(self.saved_env)


def check(name, fn):
    try:
        fn()
        print(f"[OK ] {name}")
        return 0
    except Exception as exc:
        print(f"[BAD] {name}: {exc}")
        return 1


def fork_lifecycle():
    with Harness([0]) as h:
        assert h.claims.begin_branch_work(143, "abc", "feature", "alice", "TauCeti")
        assert h.acquired_from == ["alice/TauCeti"]
        assert h.heartbeats == [("branch/143", "alice/TauCeti")]
        assert "CLAIM_REPO" not in os.environ
        assert os.environ["TAUCETI_CLAIM_REPO"] == "alice/TauCeti"
        assert os.environ["TAUCETI_PUSH_REMOTE"] == "https://github.com/alice/TauCeti"
        h.claims.release()
        assert h.released_from == ["alice/TauCeti"]
        assert "TAUCETI_CLAIM_KEY" not in os.environ
        assert "TAUCETI_CLAIM_REPO" not in os.environ


def canonical_lifecycle():
    with Harness([0]) as h:
        assert h.claims.begin_branch_work(91, "abc", "feature", "TauCetiProject", "TauCeti")
        assert h.acquired_from == ["TauCetiProject/TauCeti"]
        assert h.heartbeats == [("branch/91", "TauCetiProject/TauCeti")]
        h.claims.release()
        assert h.released_from == ["TauCetiProject/TauCeti"]


def explicit_override():
    with Harness([0], override="coordination/claims") as h:
        assert h.claims.begin_branch_work(143, "abc", "feature", "alice", "TauCeti")
        assert h.acquired_from == ["coordination/claims"]
        assert h.heartbeats == [("branch/143", "coordination/claims")]
        assert os.environ["TAUCETI_CLAIM_REPO"] == "coordination/claims"
        h.claims.release()
        assert h.released_from == ["coordination/claims"]
        assert os.environ["CLAIM_REPO"] == "coordination/claims"


def skipped_candidate_does_not_leak():
    with Harness([1, 0]) as h:
        assert not h.claims.begin_branch_work(1, "a", "one", "alice", "TauCeti")
        assert "TAUCETI_CLAIM_KEY" not in os.environ
        assert "TAUCETI_CLAIM_REPO" not in os.environ
        assert h.claims.begin_branch_work(2, "b", "two", "bob", "TauCeti")
        assert h.acquired_from == ["alice/TauCeti", "bob/TauCeti"]
        assert h.heartbeats == [("branch/2", "bob/TauCeti")]
        h.claims.release()
        assert h.released_from == ["bob/TauCeti"]


def acquire_error_fails_open():
    with Harness([2]) as h:
        assert h.claims.begin_branch_work(143, "abc", "feature", "alice", "TauCeti")
        assert h.acquired_from == ["alice/TauCeti"]
        assert h.heartbeats == []
        assert h.claims.held is None
        assert "TAUCETI_CLAIM_KEY" not in os.environ
        assert "TAUCETI_CLAIM_REPO" not in os.environ
        assert "CLAIM_REPO" not in os.environ
        assert os.environ["TAUCETI_PUSH_EXPECT"] == "abc"


def heartbeat_child_uses_selected_repo():
    captured = {}
    real_popen = round_mod.subprocess.Popen

    class FakeProcess:
        def terminate(self):
            pass

        def wait(self, _timeout):
            return 0

    def fake_popen(cmd, *, pass_fds, env):
        captured.update(cmd=cmd, pass_fds=pass_fds, env=env)
        return FakeProcess()

    round_mod.subprocess.Popen = fake_popen
    claims = round_mod.Claims(SimpleNamespace(), FakeContext())
    try:
        claims.start_heartbeat("branch/143", "alice/TauCeti")
        assert captured["env"]["CLAIM_REPO"] == "alice/TauCeti"
        assert captured["env"]["TAUCETI_CLAIM_SH"] == round_mod.CLAIM_SH
    finally:
        claims.stop_heartbeat()
        round_mod.subprocess.Popen = real_popen


def safe_push_scopes_claim_repo():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        claim_log = tmp / "claim.log"
        claim = tmp / "claim.sh"
        claim.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$CLAIM_REPO" "$1" >> "$CLAIM_LOG"\nexit 0\n')
        claim.chmod(0o755)
        git = tmp / "git"
        git.write_text("#!/bin/sh\nexit 0\n")
        git.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ['PATH']}",
            "CLAIM_LOG": str(claim_log),
            "CLAIM_REPO": "coordination/global",
            "TAUCETI_CLAIM_KEY": "branch/143",
            "TAUCETI_CLAIM_REPO": "alice/TauCeti",
            "TAUCETI_CLAIM_SH": str(claim),
            "TAUCETI_PUSH_REF": "feature",
            "TAUCETI_PUSH_EXPECT": "abc",
            "TAUCETI_PUSH_REMOTE": "https://github.com/alice/TauCeti",
        }
        result = subprocess.run([REPO / "scripts" / "git-safe-push"], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert claim_log.read_text().splitlines() == ["alice/TauCeti|holds"]


fails = sum(
    check(name, case)
    for name, case in (
        ("fork head repository is used for acquire, heartbeat, and release", fork_lifecycle),
        ("canonical head repository remains canonical", canonical_lifecycle),
        ("explicit CLAIM_REPO remains authoritative", explicit_override),
        ("a skipped fork candidate cannot leak into the next candidate", skipped_candidate_does_not_leak),
        ("claim errors still proceed unclaimed under branch CAS", acquire_error_fails_open),
        ("heartbeat child inherits the selected repository", heartbeat_child_uses_selected_repo),
        ("git-safe-push scopes its lease check to the selected repository", safe_push_scopes_claim_repo),
    )
)
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
