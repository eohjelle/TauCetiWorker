"""Standalone verification resource signalling, also staged into Bubble."""

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RESOURCE_ERROR = re.compile(
    r"ThreadPoolBuildError|failed to (?:create|spawn) thread|thread constructor failed|pthread_create.*(?:unavailable|EAGAIN)",
    re.I,
)
DEFAULTS = {"LEAN_NUM_THREADS": "4", "RAYON_NUM_THREADS": "1"}
WAIT_ENV = "TAUCETI_BUILD_RESOURCE_WAIT"

# The pinned Tools11e608b asset overflows execve for full-library HeaderStyle checks.
# Tools e4ed3eab contains precisely this batching fix; the Lean checker is identical.
STYLE_UNBATCHED_SHA256 = "7d69471ef34c5de1285013f8b3b57dc2f22a96a1168cf5ea5c8b79aaaff36f53"
STYLE_BATCHED_SHA256 = "ae730ad9546b96109d8af3df5c7e061da8006813b32d72a58fafc0c5f5e553cc"
HEADER_STYLE_SHA256 = "6ff8b815e9e9ef37ea44e444cb572f25ad0cf78ae2e8f97614134d25f77ada7c"
UNBATCHED_HEADER = 'elif ! lake env lean --run "$TRUSTED_SCRIPTS/HeaderStyle.lean" "${files[@]}"; then\n'
BATCHED_HEADER = (
    "# Pass the NUL-separated list in batches: the whole-library path list exceeds ARG_MAX (E2BIG).\n"
    "# xargs exits non-zero if any batch fails, so a single header failure still fails the audit.\n"
    'elif ! xargs -0 -n 2000 lake env lean --run "$TRUSTED_SCRIPTS/HeaderStyle.lean" < "$lint_src/files"; then\n'
)


def batched_style(scripts: Path) -> bytes | None:
    """Return the known upstream fix, or None for the recognized already-fixed asset.

    Hash the installed templates before project-name rendering. Unknown checker or script
    versions fail closed instead of guessing at their semantics or dropping a check.
    """
    style = (scripts / "lint-style.sh").read_bytes()
    header = (scripts / "HeaderStyle.lean").read_bytes()
    if hashlib.sha256(header).hexdigest() != HEADER_STYLE_SHA256:
        raise ValueError("unrecognized trusted HeaderStyle.lean; refusing a batching adaptation")
    digest = hashlib.sha256(style).hexdigest()
    if digest == STYLE_BATCHED_SHA256:
        return None
    if digest != STYLE_UNBATCHED_SHA256 or style.count(UNBATCHED_HEADER.encode()) != 1:
        raise ValueError("unrecognized trusted lint-style.sh; refusing a batching adaptation")
    updated = style.replace(UNBATCHED_HEADER.encode(), BATCHED_HEADER.encode())
    if hashlib.sha256(updated).hexdigest() != STYLE_BATCHED_SHA256:
        raise ValueError("trusted header batching does not match the verified upstream fix")
    return updated


def run_shared_checks(args):
    """Run the pinned shared checks, adapting only the known HeaderStyle argv overflow."""
    from tauceti_tools import templates

    # Shared argparse accepts options before the phase. Conservatively scan the unchanged
    # arguments rather than duplicating its parser; an option value matching a phase merely
    # enables the same checked adaptation, never changes which checks the shared parser runs.
    if not {"style", "lint", "verify"}.intersection(args):
        return templates.run_script("local_checks.py", args)
    original = templates.ASSETS
    updated = batched_style(original / "scripts")
    if updated is None:
        return templates.run_script("local_checks.py", args)
    # Never edit the installed Tools package or the mathematical checkout's scripts. run_script
    # still performs its usual trusted rendering, project bootstrap and policy-file handling.
    with tempfile.TemporaryDirectory(prefix="tauceti-header-batching-") as name:
        assets = Path(name)
        shutil.copytree(original / "scripts", assets / "scripts")
        (assets / "scripts/lint-style.sh").write_bytes(updated)
        templates.ASSETS = assets
        try:
            return templates.run_script("local_checks.py", args)
        finally:
            templates.ASSETS = original


def defaults(env):
    for name, value in DEFAULTS.items():
        env.setdefault(name, value)
    return env


def queue_owned(labels):
    return "bors-staging" in {str(x.get("name", "") if isinstance(x, dict) else x).lower() for x in labels or []}


def wait_reason():
    try:
        return json.loads(Path(os.environ[WAIT_ENV]).read_text()).get("reason")
    except (KeyError, OSError, ValueError, TypeError):
        return None


def record_wait(reason):
    if path := os.environ.get(WAIT_ENV):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps({"reason": reason, "at": time.time()}))
        os.replace(temporary, target)


def clear_wait():
    if path := os.environ.get(WAIT_ENV):
        with contextlib.suppress(OSError):
            Path(path).unlink(missing_ok=True)


def verify_shared(args):
    """Stream the unchanged shared checks, recording resource failure before an agent can retry it."""
    if reason := wait_reason():
        print(f"Verification deferred: {reason}; preserve work for the next round.", flush=True)
        return 75
    defaults(os.environ)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        *args,
    ]
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1
    ) as child:
        for line in child.stdout:
            print(line, end="", flush=True)
            if RESOURCE_ERROR.search(line):
                record_wait("build tool could not create threads; host resource exhaustion")
        rc = child.wait()
    if rc == 0:
        clear_wait()
        return 0
    return 75 if wait_reason() else rc


if __name__ == "__main__":
    try:
        raise SystemExit(run_shared_checks(sys.argv[1:]))
    except (OSError, ValueError) as error:
        print(f"Shared verification unavailable: {error}", file=sys.stderr)
        raise SystemExit(75) from None
