#!/usr/bin/env python3
"""Opt-in roadmap priority and its configurable cap agree across CLI, survey and runtime."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, actual, expected):
    global fails
    ok = actual == expected
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: {actual!r} (want {expected!r})")


def make_survey(n=0):
    sv = tc.Survey(worker_id="test", roadmap_only="ReductiveGroups")
    sv._mine_open_prs = [
        tc.PRInfo.from_json({"number": i + 1, "labels": [{"name": "roadmap/ReductiveGroups"}]}) for i in range(n)
    ]
    # Another area's open PRs must not consume this worker's cap.
    sv._mine_open_prs.extend(
        tc.PRInfo.from_json({"number": i + 100, "labels": [{"name": "roadmap/Topology"}]}) for i in range(10)
    )
    sv.reviewable.actionable.append(tc.Candidate(42, "head", "review"))
    sv.rescope_roadmap()
    return sv


def run(sv, *, only=(), prs=(), decline=()):
    seen = []

    def dispatch(stage, *_a, **_kw):
        seen.append(stage)
        return None if stage in decline else 0

    worker = SimpleNamespace(cfg=None, gh=None, rs=None, counters=None)
    opts = SimpleNamespace(only=list(only), prs=prs, dry_run=True)
    with patch.object(tc.work_units, "survey", return_value=sv), patch.object(tc.work_units, "dispatch", dispatch):
        try:
            tc.work_units.run_round(worker, opts)
            reason = None
        except tc.NoProgress as exc:
            reason = str(exc)
    return seen, reason


with patch.dict(os.environ):
    for name in ("TAUCETI_ROADMAP_BEFORE_REVIEW", "TAUCETI_ROADMAP_PR_CAP"):
        os.environ.pop(name, None)
    check("default cap remains eight", tc.config.roadmap_pr_cap(), 8)
    check("default predictor prefers review", tc._next_auto_stage(make_survey()), "review")
    check("default runtime prefers review", run(make_survey())[0], ["review"])
    check(
        "default declined review falls through to roadmap",
        run(make_survey(), decline=("review",))[0],
        ["review", "roadmap"],
    )

    os.environ["TAUCETI_ROADMAP_BEFORE_REVIEW"] = "1"
    check("opt-in cap is five", tc.config.roadmap_pr_cap(), 5)
    sv = make_survey(4)
    check("other roadmap PRs do not consume capacity", sv.n_mine_open, 4)
    check("predictor prefers roadmap below cap", sv.next_auto_stage, "roadmap")
    check("runtime prefers roadmap below cap", run(sv)[0], ["roadmap"])
    sv = make_survey(5)
    check("five PRs reaches cap", sv.roadmap_backpressure, True)
    check("predictor reviews at cap", sv.next_auto_stage, "review")
    check("runtime reviews at cap", run(sv)[0], ["review"])
    check("dashboard reports configured cap", "5/5" in tc._roadmap_note(sv)[1], True)
    sv.reviewable.actionable.clear()
    check("no review available at cap reports backpressure", ">= 5" in (run(sv)[1] or ""), True)
    check(
        "declined roadmap can fall through to review",
        run(make_survey(), decline=("roadmap",))[0],
        ["roadmap", "review"],
    )
    check("review-only never authors", run(make_survey(), only=("review",))[0], ["review"])
    check("roadmap-only never reviews at cap", run(make_survey(5), only=("roadmap",))[0], [])
    check("targeting an actionable PR never authors", run(make_survey(), prs=(42,))[0], ["review"])
    check("targeting an absent PR never authors", run(make_survey(), prs=(99,))[0], [])
    for stage in ("rebase", "bump", "progress", "fix-ci", "fix"):
        sv = make_survey()
        sv.kind(stage).actionable.append(tc.Candidate(7, "head", stage))
        check(f"{stage} remains before roadmap in predictor", tc._next_auto_stage(sv), stage)
        check(f"{stage} remains before roadmap at runtime", run(sv)[0], [stage])

    os.environ["TAUCETI_ROADMAP_PR_CAP"] = "7"
    check("custom cap replaces five", make_survey(6).roadmap_backpressure, False)
    check("custom cap is enforced", make_survey(7).roadmap_backpressure, True)
    os.environ["TAUCETI_ROADMAP_BEFORE_REVIEW"] = "0"
    check("custom cap also works with review-first", tc.config.roadmap_pr_cap(), 7)
    check("explicit false preserves default order", tc._next_auto_stage(make_survey()), "review")

    for invalid in ("0", "-1", "", "five", "1.5"):
        os.environ["TAUCETI_ROADMAP_PR_CAP"] = invalid
        try:
            tc.config.roadmap_pr_cap()
            rejected = False
        except tc.Die:
            rejected = True
        check(f"invalid cap {invalid!r} is rejected", rejected, True)

    # Exercise the real CLI handoff, stopping immediately before any filesystem/network work.
    parser = tc.build_parser()
    args = parser.parse_args(["work", "--loop", "--roadmap-before-review", "--roadmap-pr-cap", "6"])

    def inspect_handoff(*_a):
        check("CLI overrides invalid environment cap", tc.config.roadmap_pr_cap(), 6)
        # A fresh interpreter inherits the resolved settings just as _round does.
        child = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "from tauceti_worker.config import auto_stages, roadmap_pr_cap; "
                "print(','.join(auto_stages()), roadmap_pr_cap())",
            ],
            cwd=REPO,
            text=True,
        ).strip()
        check("child inherits priority and cap", child, "rebase,bump,progress,fix-ci,fix,roadmap,review 6")
        raise KeyboardInterrupt

    with patch.object(tc.cli, "resolve_source", inspect_handoff):
        try:
            tc.cmd_work(args, only=[], agent="auto", one_round=False)
        except KeyboardInterrupt:
            pass
    round_args = parser.parse_args(["_round", "--roadmap-before-review", "--roadmap-pr-cap", "6"])
    check("internal round accepts both flags", (round_args.roadmap_before_review, round_args.roadmap_pr_cap), (True, 6))

print(f"\n{'FAIL' if fails else 'PASS'}: {fails} mismatch(es)")
sys.exit(bool(fails))
