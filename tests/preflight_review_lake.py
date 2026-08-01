#!/usr/bin/env python3
"""Host authoring checks plain Lake through the login shell the agent command tools use."""

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0
_MISSING = object()


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


def replace(module, name, value, saved):
    saved.append((module, name, getattr(module, name, _MISSING)))
    setattr(module, name, value)


def restore(saved):
    for module, name, value in reversed(saved):
        if value is _MISSING:
            delattr(module, name)
        else:
            setattr(module, name, value)


def opts(only):
    return tc.RoundOpts(only=only, agent="claude", work_model="claude", sandbox_host=True, dry_run=False)


# The low-level lookup must use the selected login shell, not the parent process's shutil.which().
helper_saved = []
helper_calls = []


def fake_shell_run(argv, **kwargs):
    helper_calls.append((argv, kwargs))
    return SimpleNamespace(returncode=0, stdout="/shared/elan/bin/lake\n", stderr="")


try:
    replace(tc.agents, "_host_shell", lambda: "/the/login-shell", helper_saved)
    replace(tc.agents.subprocess, "run", fake_shell_run, helper_saved)
    supplied_env = {"PATH": "/agent/path", "ELAN_HOME": "/shared/elan"}
    resolved = tc.agents.host_login_shell_which("lake", env=supplied_env)
    check("Lake lookup returns the login shell's path", resolved == "/shared/elan/bin/lake")
    check(
        "Lake lookup invokes the selected shell with -lc and plain command -v",
        bool(helper_calls)
        and helper_calls[-1][0] == ["/the/login-shell", "-lc", "command -v lake"]
        and helper_calls[-1][1].get("env") == supplied_env,
    )
finally:
    restore(helper_saved)


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cfg = SimpleNamespace(
        wid="lake-preflight",
        home=root / "home",
        state=root / "state",
        checkout=root / "checkout",
        logdir=root / "logs",
        quota_cache=root / "quota-cache",
    )
    cfg.home.mkdir()
    cfg.state.mkdir()
    cfg.checkout.mkdir()

    cache_env = {
        "LAKE_CONFIG": str(root / "lake-config.toml"),
        "LAKE_CACHE_DIR": str(root / "cache"),
        "LAKE_ARTIFACT_CACHE": "true",
        "LAKE_RESTORE_ARTIFACTS": "true",
    }
    base_env = {
        "HOME": str(root / "agent-home"),
        "ELAN_HOME": str(root / "shared-elan"),
        "PATH": "/operator/bin:/usr/bin",
        "TAUCETI_TEST_AGENT_ENV": "inherited",
    }
    env_keys = (*base_env, *cache_env)
    saved_env = {key: os.environ.get(key, _MISSING) for key in env_keys}
    os.environ.update(base_env)

    saved = []
    calls = []
    expected_push = str(REPO / "scripts" / "git-safe-push")
    login_result = {"lake": None, "git-safe-push": expected_push}
    parent_lake = {"present": True}

    def fake_have(tool):
        calls.append(("which", tool))
        return parent_lake["present"] if tool == "lake" else True

    def fake_configure(got):
        calls.append(("configure", got))
        os.environ.update(cache_env)
        return dict(cache_env)

    def fake_lake_env(got):
        calls.append(("lake-env", got))
        return dict(cache_env)

    def fake_login_which(tool, env=None):
        calls.append(("login", tool, env))
        return login_result.get(tool)

    try:
        replace(tc.cli, "_have", fake_have, saved)
        replace(tc.cli, "configure_host_lake_cache", fake_configure, saved)
        replace(tc.cli, "host_lake_env", fake_lake_env, saved)
        replace(tc.cli, "host_login_shell_which", fake_login_which, saved)

        # A parent-process Lake does not help if login-shell startup removes it.
        calls.clear()
        parent_lake["present"] = True
        login_result["lake"] = None
        try:
            tc.cli.preflight(cfg, opts(["fix"]))
            error = ""
        except tc.Die as exc:
            error = str(exc)
        check("host authoring fails when the agent login shell cannot resolve Lake", "login shell" in error)
        kinds = [call[0] for call in calls]
        check(
            "cache environment is configured before the Lake probe",
            "configure" in kinds and "login" in kinds and kinds.index("configure") < kinds.index("login"),
        )
        login_call = next(call for call in calls if call[0] == "login")
        probe_env = login_call[2] or {}
        check("preflight probes plain lake", login_call[1] == "lake")
        check("Lake probe receives every cache variable", all(probe_env.get(k) == v for k, v in cache_env.items()))
        _, agent_env = tc.agents.host_agent_argv("", "claude")
        check(
            "probe and agent receive the same HOME, PATH, Elan, and cache environment",
            all(probe_env.get(k) == agent_env.get(k) for k in ("HOME", "PATH", "ELAN_HOME", *cache_env)),
        )

        # Conversely, the login-shell result is authoritative even if parent shutil.which would fail.
        calls.clear()
        parent_lake["present"] = False
        login_result["lake"] = "/shared/elan/bin/lake"
        try:
            tc.cli.preflight(cfg, opts(["fix"]))
            error = ""
        except tc.Die as exc:
            error = str(exc)
        check("agent-shell Lake succeeds independently of parent PATH", not error)
        check("preflight never asks parent shutil.which about Lake", ("which", "lake") not in calls)
        check(
            "preflight verifies the safe-push helper in the same login shell",
            any(call[:2] == ("login", "git-safe-push") for call in calls),
        )

        calls.clear()
        login_result["git-safe-push"] = None
        try:
            tc.cli.preflight(cfg, opts(["fix"]))
            error = ""
        except tc.Die as exc:
            error = str(exc)
        check("host authoring fails before launch when safe helpers fall off login PATH", "git-safe-push" in error)
        login_result["git-safe-push"] = expected_push

        # Review runs no local Lean build and should not require or configure Lake.
        calls.clear()
        login_result["lake"] = None
        try:
            tc.cli.preflight(cfg, opts(["review"]))
            error = ""
        except tc.Die as exc:
            error = str(exc)
        check("host review without Lake is not blocked", not error)
        check("host review skips cache setup and Lake lookup", not any(c[0] in ("configure", "login") for c in calls))

        # Doctor reports the same direct path, or a clear missing result, without writing cache config.
        replace(tc.cli.Config, "resolve", staticmethod(lambda *a, **k: cfg), saved)
        replace(
            tc.cli.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
            saved,
        )
        replace(tc.cli, "_claude_keychain_creds", lambda: None, saved)

        calls.clear()
        login_result["lake"] = None
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            tc.cli.cmd_doctor(SimpleNamespace())
        lake_line = next(line for line in output.getvalue().splitlines() if "lake (agent shell)" in line.lower())
        check("doctor marks missing agent-shell Lake", "MISSING" in lake_line and "not resolvable" in lake_line)

        calls.clear()
        login_result["lake"] = "/shared/elan/bin/lake"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            tc.cli.cmd_doctor(SimpleNamespace())
        lake_line = next(line for line in output.getvalue().splitlines() if "lake (agent shell)" in line.lower())
        check("doctor reports the real Lake path", "ok" in lake_line.lower() and "/shared/elan/bin/lake" in lake_line)
        helper_line = next(line for line in output.getvalue().splitlines() if "agent helpers" in line.lower())
        check("doctor reports the safe helper path", "ok" in helper_line.lower() and expected_push in helper_line)
        check("doctor does not materialize cache configuration", not any(c[0] == "configure" for c in calls))
    finally:
        restore(saved)
        for key, value in saved_env.items():
            if value is _MISSING:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
