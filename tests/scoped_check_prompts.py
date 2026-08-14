#!/usr/bin/env python3
"""Authoring workers build changed modules and leave repository-wide checks to CI."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ("roadmap.md", "fix.md", "fix-ci.md", "rebase.md", "bump.md")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


for name in PROMPTS:
    text = (ROOT / "prompts" / name).read_text()
    check(
        "lake exe axioms --changed-since-merge-base origin/main" in text,
        f"{name}: missing changed-module axiom audit",
    )
    check(
        "lake env bash scripts/lint-env.sh --changed-since-merge-base origin/main" in text,
        f"{name}: missing changed-module environment lint",
    )
    check(
        "lake build TauCeti.<Module>" in text,
        f"{name}: missing targeted module-build guidance",
    )
    check(
        re.search(r"(?m)^lake build(?: --iofail)?$", text) is None,
        f"{name}: repository-wide local build returned",
    )
    check(
        re.search(r"Do\s+not\s+run a bare `lake build`", text) is not None,
        f"{name}: missing explicit global-build prohibition",
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

    report = text.split("## Report", 1)[1]
    check(
        not re.search(r"\b(?:lake|axiom|lint|verification)\b", report, re.IGNORECASE),
        f"{name}: routine verification leaked into report guidance",
    )

print(f"scoped_check_prompts: PASS ({len(PROMPTS)} prompts)")
