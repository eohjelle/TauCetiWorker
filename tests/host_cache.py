#!/usr/bin/env python3
"""Host authoring warms both Lean caches on current main before launching an agent.

This pins the host-only contract: the generated public Lake configuration and cache directory are
outside isolated HOME, all Lake restore variables reach the agent, Mathlib download failure is fatal
after one retry, a TauCeti cache miss is advisory, and no eager full build is introduced.  It also
guards the dispatch ordering that keeps machine-wide setup failures out of fix-CI attempt counters.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0
LAKE_KEYS = (
    "LAKE_CONFIG",
    "LAKE_CACHE_DIR",
    "LAKE_ARTIFACT_CACHE",
    "LAKE_RESTORE_ARTIFACTS",
)


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


def restore_env(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def temp_cfg(root: Path):
    return SimpleNamespace(
        state=root / "state",
        checkout=root / "checkout",
        home=root / "home",
        logdir=root / "logs",
        wid="host-cache-test",
    )


# The host owns the service configuration.  It must not fall back to the isolated HOME's ~/.lake,
# and later shells spawned by the model must see the exact same settings used by the current-main fetch.
saved_lake_env = {key: os.environ.get(key) for key in LAKE_KEYS}
try:
    for key in LAKE_KEYS:
        os.environ[key] = f"/stale/operator/value/{key}"
    with tempfile.TemporaryDirectory() as td:
        cfg = temp_cfg(Path(td))
        env = tc.configure_host_lake_cache(cfg)
        config_path = Path(env["LAKE_CONFIG"])
        cache_path = Path(env["LAKE_CACHE_DIR"])
        config = config_path.read_text()

        check("Lake config path is absolute", config_path.is_absolute())
        check(
            "Lake config lives under per-worker state",
            config_path == cfg.state or cfg.state in config_path.parents,
        )
        check("Lake cache path is absolute", cache_path.is_absolute())
        check("Lake cache is checkout-local", cache_path == cfg.checkout / ".lake" / "cache")
        check("fresh host-cache preflight leaves the clone target absent", not cfg.checkout.exists())
        check(
            "Lake artifact cache respects an operator override",
            env["LAKE_ARTIFACT_CACHE"] == "/stale/operator/value/LAKE_ARTIFACT_CACHE",
        )
        check("Lake restores artifacts during later builds", env["LAKE_RESTORE_ARTIFACTS"] == "true")
        check("public config selects TauCeti service", 'cache.defaultService = "tauceti-public"' in config)
        check("public config has one service block", config.count("[[cache.service]]") == 1)
        check("public config names TauCeti service", 'name = "tauceti-public"' in config)
        check("public config uses the S3 service kind", 'kind = "s3"' in config)
        check(
            "public config has the canonical artifact endpoint",
            f'artifactEndpoint = "{tc.TAUCETI_CACHE_ARTIFACT_URL}"' in config,
        )
        check(
            "public config has the canonical revision endpoint",
            f'revisionEndpoint = "{tc.TAUCETI_CACHE_REVISION_URL}"' in config,
        )
        check("public config contains only the two canonical endpoints", config.count(tc.TAUCETI_CACHE_DOMAIN) == 2)
        check("configure updates the process environment", all(os.environ.get(key) == env[key] for key in LAKE_KEYS))

        _, agent_env = tc.host_agent_argv("PROMPT", "codex")
        check("host agent inherits every Lake cache variable", all(agent_env.get(key) == env[key] for key in LAKE_KEYS))

        # prepare_host_authoring calls the same setup again once prepare_checkout has cloned the
        # repository. At that point materialize the checkout-local cache as before.
        (cfg.checkout / ".git").mkdir(parents=True)
        tc.configure_host_lake_cache(cfg)
        check("host cache materializes after the checkout exists", cache_path.is_dir())
finally:
    restore_env(saved_lake_env)

with tempfile.TemporaryDirectory() as td:
    cfg = temp_cfg(Path(td))
    saved_artifact_cache = os.environ.pop("LAKE_ARTIFACT_CACHE", None)
    try:
        check("Lake artifact cache defaults to writable", tc.host_lake_env(cfg)["LAKE_ARTIFACT_CACHE"] == "true")
        os.environ["LAKE_ARTIFACT_CACHE"] = "false"
        check("explicit read-only artifact cache is preserved", tc.host_lake_env(cfg)["LAKE_ARTIFACT_CACHE"] == "false")
    finally:
        if saved_artifact_cache is None:
            os.environ.pop("LAKE_ARTIFACT_CACHE", None)
        else:
            os.environ["LAKE_ARTIFACT_CACHE"] = saved_artifact_cache


def exercise_prepare(mathlib_rcs, tauceti_rc):
    """Run prepare_host_authoring with checkout/network effects replaced by a command recorder."""
    root = Path(tempfile.mkdtemp())
    cfg = temp_cfg(root)
    order = []
    calls = []
    mathlib_rcs = iter(mathlib_rcs)
    saved_prepare_checkout = tc.agents.prepare_checkout
    saved_run = tc.agents.subprocess.run
    saved_prune = tc.agents.PRUNE_OBSOLETE_LEAN_TOOLCHAINS
    saved_env = {key: os.environ.get(key) for key in LAKE_KEYS}

    def fake_prepare_checkout(got):
        check("prepare receives the requested worker config", got is cfg)
        order.append("prepare-main")
        (cfg.checkout / ".git").mkdir(parents=True)
        (cfg.checkout / "lean-toolchain").write_text("leanprover/lean4:v4.test\n")
        return True

    def fake_run(argv, **kwargs):
        rendered = " ".join(str(arg) for arg in argv)
        if "elan toolchain list" in rendered:
            kind, rc = "elan-list", 0
            stdout = "leanprover/lean4:v4.old\nleanprover/lean4:v4.test\ncustom-linked-toolchain\n"
        elif "elan toolchain uninstall" in rendered:
            kind, rc, stdout = "elan-uninstall", 0, ""
        elif "lake exe cache get" in rendered:
            kind, rc = "mathlib", next(mathlib_rcs)
            stdout = ""
        elif "lake cache get" in rendered:
            kind, rc = "tauceti", tauceti_rc
            stdout = ""
        else:
            kind, rc = "other", 0
            stdout = ""
        order.append(kind)
        calls.append((kind, list(argv), rendered, kwargs))
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")

    tc.agents.prepare_checkout = fake_prepare_checkout
    tc.agents.subprocess.run = fake_run
    tc.agents.PRUNE_OBSOLETE_LEAN_TOOLCHAINS = True
    try:
        result = tc.prepare_host_authoring(cfg)
        error = None
    except Exception as exc:
        result, error = None, exc
    finally:
        tc.agents.prepare_checkout = saved_prepare_checkout
        tc.agents.subprocess.run = saved_run
        tc.agents.PRUNE_OBSOLETE_LEAN_TOOLCHAINS = saved_prune
        restore_env(saved_env)
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    return result, error, order, calls


# Successful setup: current main first, then Mathlib, then TauCeti. A public cache miss only
# means more compilation later, so it must not prevent the semantic repair agent from launching.
_, error, order, calls = exercise_prepare([0], 1)
check("TauCeti cache miss is nonfatal", error is None)
check(
    "current main and toolchains are prepared before either cache fetch",
    order[:5] == ["prepare-main", "elan-list", "elan-uninstall", "mathlib", "tauceti"],
)
check("host setup runs no unexpected subprocess", "other" not in order)
check(
    "obsolete official Lean toolchain is uninstalled",
    any("elan toolchain uninstall leanprover/lean4:v4.old" in command for _, _, command, _ in calls),
)
uninstalls = [command for kind, _, command, _ in calls if kind == "elan-uninstall"]
check(
    "only obsolete official toolchains are uninstalled",
    len(uninstalls) == 1 and "leanprover/lean4:v4.old" in uninstalls[0],
)
check(
    "host setup never runs a pre-agent lake build",
    all("lake build" not in command for _, _, command, _ in calls),
)
check(
    "TauCeti fetch uses the named public service and canonical repository",
    any(
        kind == "tauceti"
        and "cache get" in command
        and f"--service {tc.TAUCETI_CACHE_SERVICE}" in command
        and f"--repo {tc.TAUCETI}" in command
        for kind, _, command, _ in calls
    ),
)
check(
    "cache commands run in the host checkout",
    all(Path(str(kwargs.get("cwd"))).resolve() == Path(str(calls[0][3]["cwd"])).resolve() for _, _, _, kwargs in calls)
    and Path(str(calls[0][3]["cwd"])).resolve().name == "checkout",
)
check(
    "cache commands receive all Lake variables",
    all(
        all((kwargs.get("env") or {}).get(key) for key in LAKE_KEYS)
        for kind, _, _, kwargs in calls
        if kind in ("mathlib", "tauceti")
    ),
)
check(
    "cache commands run through a login shell",
    all(len(argv) >= 3 and argv[-2] == "-lc" for _, argv, _, _ in calls),
)
check(
    "cache fetch invokes plain Lake",
    all(argv[-1].startswith("exec lake ") for kind, argv, _, _ in calls if kind in ("mathlib", "tauceti")),
)

# A shared Elan installation must retain the predecessor while a sibling worker checkout still names it.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    checkout = root / "checkouts" / "worker1" / "TauCeti"
    sibling = root / "checkouts" / "worker2" / "TauCeti"
    state = root / "state" / "worker1"
    checkout.mkdir(parents=True)
    sibling.mkdir(parents=True)
    state.mkdir(parents=True)
    (checkout / "lean-toolchain").write_text("leanprover/lean4:v4.new\n")
    (sibling / "lean-toolchain").write_text("leanprover/lean4:v4.old\n")
    cfg = SimpleNamespace(checkout=checkout, state=state)
    uninstall_calls = []
    saved_login_command = tc.agents._run_host_login_command

    def fake_login_command(argv, **_kwargs):
        uninstall_calls.append(list(argv))
        stdout = "leanprover/lean4:v4.old\nleanprover/lean4:v4.new\n" if argv[-1] == "list" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    saved_prune = tc.agents.PRUNE_OBSOLETE_LEAN_TOOLCHAINS
    tc.agents._run_host_login_command = fake_login_command
    tc.agents.PRUNE_OBSOLETE_LEAN_TOOLCHAINS = True
    try:
        tc.prune_obsolete_host_lean_toolchains(cfg)
        check("sibling checkout retains its requested toolchain", uninstall_calls == [["elan", "toolchain", "list"]])
        (sibling / "lean-toolchain").write_text("leanprover/lean4:v4.new\n")
        tc.prune_obsolete_host_lean_toolchains(cfg)
        check(
            "predecessor is retired after every checkout upgrades",
            uninstall_calls[-2:]
            == [["elan", "toolchain", "list"], ["elan", "toolchain", "uninstall", "leanprover/lean4:v4.old"]],
        )
    finally:
        tc.agents._run_host_login_command = saved_login_command
        tc.agents.PRUNE_OBSOLETE_LEAN_TOOLCHAINS = saved_prune

# A transient Mathlib outage gets one retry.  Once the retry succeeds, the TauCeti fetch still follows.
_, error, order, calls = exercise_prepare([1, 0], 0)
check(
    "Mathlib cache gets one retry",
    error is None and order == ["prepare-main", "elan-list", "elan-uninstall", "mathlib", "mathlib", "tauceti"],
)
check(
    "retry path still has no full build",
    all("lake build" not in command for _, _, command, _ in calls),
)

# Two Mathlib failures are a machine/setup failure: fail before touching TauCeti or launching a model.
_, error, order, calls = exercise_prepare([1, 1], 0)
check("repeated Mathlib failure raises Die", isinstance(error, tc.Die))
check(
    "fatal Mathlib path stops before TauCeti",
    order == ["prepare-main", "elan-list", "elan-uninstall", "mathlib", "mathlib"],
)
check(
    "fatal Mathlib path still has no full build",
    all("lake build" not in command for _, _, command, _ in calls),
)

# Cache retention is a soft high-water check at round boundaries. It removes only .lake/cache,
# preserves the incremental build tree, and also responds to low free space independently.
with tempfile.TemporaryDirectory() as td:
    cfg = temp_cfg(Path(td))
    artifact = cfg.checkout / ".lake" / "cache" / "artifacts" / "branch-output"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"branch artifact")
    build_output = cfg.checkout / ".lake" / "build" / "lib" / "lean" / "TauCeti.olean"
    build_output.parent.mkdir(parents=True)
    build_output.write_bytes(b"warm incremental build")
    saved_max = tc.agents.HOST_LAKE_CACHE_MAX_BYTES
    saved_min = tc.agents.HOST_LAKE_CACHE_MIN_FREE_BYTES
    saved_disk_usage = tc.agents.shutil.disk_usage
    saved_limit_env = {
        name: os.environ.pop(name, None) for name in ("TAUCETI_LAKE_CACHE_MAX_GIB", "TAUCETI_LAKE_CACHE_MIN_FREE_GIB")
    }
    try:
        tc.agents.HOST_LAKE_CACHE_MAX_BYTES = 1
        tc.agents.HOST_LAKE_CACHE_MIN_FREE_BYTES = 1
        check("10 GiB is the default artifact-cache limit", saved_max == 10 * 1024**3)
        check("8 GiB is the default filesystem safety floor", saved_min == 8 * 1024**3)
        check("oversized artifact cache is purged", tc.maintain_host_lake_cache(cfg, phase="test"))
        check("cache purge removes branch artifacts", not artifact.exists())
        check("cache purge preserves incremental build outputs", build_output.exists())

        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"new branch artifact")
        tc.agents.HOST_LAKE_CACHE_MAX_BYTES = 1024**4
        tc.agents.HOST_LAKE_CACHE_MIN_FREE_BYTES = 8 * 1024**3
        tc.agents.shutil.disk_usage = lambda _path: SimpleNamespace(free=1024**3)
        check("low filesystem space independently purges the cache", tc.maintain_host_lake_cache(cfg, phase="test"))

        # Once the disposable artifact cache is gone, the preflight must still enforce the free-space
        # floor instead of returning early and launching a model on a nearly full filesystem.
        import shutil

        shutil.rmtree(cfg.checkout / ".lake" / "cache")
        try:
            tc.maintain_host_lake_cache(cfg, phase="test", require_headroom=True)
            no_cache_error = None
        except Exception as exc:
            no_cache_error = exc
        check("low-space preflight fails closed without a disposable cache", isinstance(no_cache_error, tc.Die))

        os.environ["TAUCETI_LAKE_CACHE_MAX_GIB"] = "12"
        check(
            "operator can raise the cache limit",
            tc.agents._configured_gibibytes("TAUCETI_LAKE_CACHE_MAX_GIB", saved_max) == 12 * 1024**3,
        )
    finally:
        tc.agents.HOST_LAKE_CACHE_MAX_BYTES = saved_max
        tc.agents.HOST_LAKE_CACHE_MIN_FREE_BYTES = saved_min
        tc.agents.shutil.disk_usage = saved_disk_usage
        for name, value in saved_limit_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# dispatch() is the counter boundary.  Host preparation must happen before do_fix_ci, whose real
# implementation charges both semantic counters and eventually launches the model.
wu = tc.work_units
saved_dispatch_bits = {
    name: getattr(wu, name)
    for name in (
        "prepare_host_authoring",
        "_host_agent_binary",
        "do_fix_ci",
        "_progress_snapshot",
        "_progressed",
    )
}
events = []


class RecordingCounters:
    def __init__(self):
        self.keys = []

    def incr(self, key):
        self.keys.append(key)


counters = RecordingCounters()
w = SimpleNamespace(cfg=SimpleNamespace(), counters=counters)
c = tc.Candidate(123, "deadbeef", "red CI")
explicit_codex = tc.AuthoringProfile(
    provider="codex",
    model="gpt-5.6-sol",
    effort="high",
    model_source="test",
    effort_source="test",
)
opts = tc.RoundOpts(
    only=["fix-ci"],
    agent="codex",
    work_model="codex",
    sandbox_host=True,
    dry_run=False,
    authoring_profile=explicit_codex,
)


def fail_machine_setup(_cfg):
    events.append("prepare")
    raise tc.Die("host cache unavailable")


def fake_fix_ci(worker, _sv, candidate, _opts, _bubble):
    events.append("model")
    worker.counters.incr(f"ci-{candidate.pr}-{candidate.head[:12]}")
    worker.counters.incr(f"ci-pr-{candidate.pr}")
    return 0


try:
    wu.prepare_host_authoring = fail_machine_setup
    wu._host_agent_binary = lambda _stage, _model: None
    wu.do_fix_ci = fake_fix_ci
    wu._progress_snapshot = lambda *_args: None
    wu._progressed = lambda *_args: True
    try:
        wu.dispatch("fix-ci", w, SimpleNamespace(), c, opts)
        dispatch_error = None
    except Exception as exc:
        dispatch_error = exc
    check("dispatch surfaces host setup failure", isinstance(dispatch_error, tc.Die))
    check("dispatch attempts host setup exactly once", events == ["prepare"])
    check("host setup failure launches no model", "model" not in events)
    check("host setup failure charges no fix-CI counter", counters.keys == [])
finally:
    for name, value in saved_dispatch_bits.items():
        setattr(wu, name, value)

# A claim race returns None so run_round can try another candidate in the same stage. The current-main
# checkout and caches are already warm; dispatch must reuse them rather than fetching
# both caches again before the second claim.
reuse_saved = {
    name: getattr(wu, name)
    for name in (
        "prepare_host_authoring",
        "maintain_host_lake_cache",
        "maintain_worker_logs",
        "_host_agent_binary",
        "do_fix_ci",
        "_progress_snapshot",
    )
}
reuse_events = []
reuse_opts = tc.RoundOpts(
    only=["fix-ci"],
    agent="codex",
    work_model="codex",
    sandbox_host=True,
    dry_run=False,
    authoring_profile=explicit_codex,
)


def record_prepare(_cfg):
    reuse_events.append("prepare")


def claimed_then_run(*_args):
    reuse_events.append("stage")
    return None if reuse_events.count("stage") == 1 else 1


try:
    wu.prepare_host_authoring = record_prepare
    wu.maintain_host_lake_cache = lambda *_args, **_kwargs: reuse_events.append("maintain")
    wu.maintain_worker_logs = lambda *_args, **_kwargs: reuse_events.append("maintain-logs")
    wu._host_agent_binary = lambda _stage, _model: None
    wu.do_fix_ci = claimed_then_run
    wu._progress_snapshot = lambda *_args: None
    first = wu.dispatch("fix-ci", SimpleNamespace(cfg=SimpleNamespace()), SimpleNamespace(), c, reuse_opts)
    second = wu.dispatch("fix-ci", SimpleNamespace(cfg=SimpleNamespace()), SimpleNamespace(), c, reuse_opts)
    check("claim-raced candidate asks the caller to continue", first is None)
    check("second candidate completes without another host warmup", second == 1)
    check(
        "claim-raced candidates reuse one host cache preparation and check storage after each stage",
        reuse_events
        == [
            "prepare",
            "stage",
            "maintain",
            "maintain-logs",
            "stage",
            "maintain",
            "maintain-logs",
        ],
    )
finally:
    for name, value in reuse_saved.items():
        setattr(wu, name, value)


print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
