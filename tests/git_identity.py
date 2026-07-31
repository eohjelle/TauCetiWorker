#!/usr/bin/env python3
"""Git author identity follows the account authenticated with GitHub CLI."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, condition):
    global fails
    fails += not condition
    print(f"[{'OK ' if condition else 'BAD'}] {name}")


def completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess(["gh"], returncode, stdout=stdout, stderr="")


saved_gh_run = tc.github.gh_run
saved_identity = tc.agents.git_author_identity
root = Path(tempfile.mkdtemp(prefix="tauceti-git-identity-"))
checkout = root / "checkout"

try:
    tc.github.git_author_identity.cache_clear()
    tc.github.gh_run = lambda argv: completed(stdout=json.dumps(["alice", 12345]))
    check(
        "GitHub account becomes a Git author identity",
        tc.github.git_author_identity() == ("alice", "12345+alice@users.noreply.github.com"),
    )

    tc.github.git_author_identity.cache_clear()
    tc.github.gh_run = lambda argv: completed(stdout=json.dumps(["alice", None]))
    try:
        tc.github.git_author_identity()
        check("missing GitHub account id fails closed", False)
    except tc.Die:
        check("missing GitHub account id fails closed", True)

    subprocess.run(["git", "init", "-q", checkout], check=True)
    tc.agents.git_author_identity = lambda: ("bob", "67890+bob@users.noreply.github.com")
    check("checkout identity configuration succeeds", tc.agents.configure_checkout_git_identity(checkout))
    name = subprocess.run(
        ["git", "-C", checkout, "config", "--local", "--get", "user.name"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    email = subprocess.run(
        ["git", "-C", checkout, "config", "--local", "--get", "user.email"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    check("checkout author name follows GitHub", name == "bob")
    check("checkout author email is GitHub noreply", email == "67890+bob@users.noreply.github.com")
finally:
    tc.github.gh_run = saved_gh_run
    tc.github.git_author_identity.cache_clear()
    tc.agents.git_author_identity = saved_identity
    shutil.rmtree(root, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
