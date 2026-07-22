#!/usr/bin/env python3
"""GitHub.set_review_inprogress_label — make `review-in-progress` the sole pipeline-status label.

The worker sets this best-effort while it runs the review engine on a PR; TauCeti CI owns the other
four status labels and clears `review-in-progress` on the next real transition. This harness pins the
pure gh-argument decision (create the label, add ours, remove ONLY the other status labels that are
actually present, never raise) by stubbing the two gh-touching methods, so it needs no network.

Exit 0 = all cases pass; 1 = a failure.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from tauceti_worker import constants  # noqa: E402
from tauceti_worker.github import GitHub  # noqa: E402

OK = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
BAD = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")


class FakeGH(GitHub):
    """A GitHub whose only two gh-touching methods used by the label setter are stubbed:
    `_gh` records every argv (and can be made to fail a given verb), `pr_view` returns fixed labels."""

    def __init__(self, present, fail_verb=None):
        super().__init__("owner/repo")
        self._present = present
        self._fail_verb = fail_verb
        self.calls: list[list[str]] = []

    def _gh(self, args):
        self.calls.append(args)
        if self._fail_verb is not None and args[:1] == [self._fail_verb]:
            return BAD
        return OK

    def pr_view(self, pr, fields):
        return {"labels": [{"name": n} for n in self._present]}


def edit_call(calls):
    return next(c for c in calls if c[:2] == ["pr", "edit"])


ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(("ok   " if cond else "FAIL ") + name)


# 1) awaiting-review present -> removed; review-in-progress added; label created with --force.
g = FakeGH(present={"awaiting-review", "roadmap/PDE"})
res = g.set_review_inprogress_label(17)
create = next(c for c in g.calls if c[:2] == ["label", "create"])
edit = edit_call(g.calls)
check("returns True on a clean set", res is True)
check("creates the label with --force", "--force" in create and constants.REVIEW_INPROGRESS_LABEL in create)
check("adds review-in-progress", edit[edit.index("--add-label") + 1] == "review-in-progress")
check("removes the present CI label awaiting-review", "awaiting-review" in edit)
check("leaves a non-status label alone", "roadmap/PDE" not in edit)
check("does not try to remove absent status labels", "ready-to-merge" not in edit and "awaiting-CI" not in edit)
check(
    "never removes its own label (only adds it)",
    "--remove-label" not in edit or "review-in-progress" not in edit[edit.index("--add-label") + 2 :],
)

# 2) no status labels present -> add only, no --remove-label at all.
g = FakeGH(present=set())
g.set_review_inprogress_label(3)
check("no removals when nothing is present", "--remove-label" not in edit_call(g.calls))

# 3) all four CI labels present -> all four removed.
g = FakeGH(present={"awaiting-CI", "awaiting-review", "awaiting-author", "ready-to-merge"})
g.set_review_inprogress_label(9)
edit = edit_call(g.calls)
for lbl in ("awaiting-CI", "awaiting-review", "awaiting-author", "ready-to-merge"):
    check(f"removes {lbl}", lbl in edit)

# 4) a gh failure on the `pr edit` -> returns False, never raises.
g = FakeGH(present={"awaiting-review"}, fail_verb="pr")
check("returns False when the edit fails", g.set_review_inprogress_label(1) is False)

# 5) pr_view raising -> caught, returns False.
g = FakeGH(present=set())
g.pr_view = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
check("swallows exceptions and returns False", g.set_review_inprogress_label(1) is False)

sys.exit(0 if ok else 1)
