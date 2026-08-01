#!/usr/bin/env python3
"""Authoring workers build globally but run axiom/lint checks on changed modules."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ("roadmap.md", "fix.md", "fix-ci.md", "rebase.md", "bump.md")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


for name in PROMPTS:
    text = (ROOT / "prompts" / name).read_text()
    check(
        "tauceti-axioms --changed-since-merge-base origin/main" in text,
        f"{name}: missing changed-module axiom audit",
    )
    check(
        "tauceti-lint-env --changed-since-merge-base origin/main" in text,
        f"{name}: missing changed-module environment lint",
    )
    check(
        re.search(r"(?m)^lake build(?: --iofail)?$", text) is not None,
        f"{name}: global incremental build must remain in the gate",
    )
    check(
        re.search(r"(?m)^lake exe axioms$", text) is None,
        f"{name}: repository-wide axiom audit returned to an authoring prompt",
    )
    check(
        re.search(r"(?m)^(?:lake env )?bash scripts/lint-env\.sh$", text) is None,
        f"{name}: repository-wide environment lint returned to an authoring prompt",
    )
    check(
        "tauceti-local-checks" not in text,
        f"{name}: obsolete bundled helper returned",
    )
    check("--changed-from" not in text, f"{name}: retired scoped-check flag returned")

    report = text.split("## Report", 1)[1]
    check(
        not re.search(r"\b(?:lake|axiom|lint|verification)\b", report, re.IGNORECASE)
        or "You don't need to make claims about" in report,
        f"{name}: routine verification leaked into report guidance",
    )

print(f"scoped_check_prompts: PASS ({len(PROMPTS)} prompts)")
