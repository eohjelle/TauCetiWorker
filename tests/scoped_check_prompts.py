#!/usr/bin/env python3
"""Authoring workers build globally but run axiom/lint checks on changed modules."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ("roadmap.md", "fix.md", "fix-ci.md", "rebase.md", "bump.md")
AXIOM_WRAPPER = (ROOT / "scripts" / "tauceti-axioms").read_text()
LINT_WRAPPER = (ROOT / "scripts" / "tauceti-lint-env").read_text()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


for name in PROMPTS:
    text = (ROOT / "prompts" / name).read_text()
    check(
        "tauceti-axioms --changed-from origin/main" in text,
        f"{name}: missing changed-module axiom audit",
    )
    check(
        "tauceti-lint-env --changed-from origin/main" in text,
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
    check(
        "lake exe axioms --changed-from" not in text,
        f"{name}: bypasses the feature-branch axiom wrapper",
    )
    check(
        "scripts/lint-env.sh --changed-from" not in text,
        f"{name}: bypasses the feature-branch lint wrapper",
    )

    report = text.split("## Report", 1)[1]
    check(
        not re.search(r"\b(?:lake|axiom|lint|verification)\b", report, re.IGNORECASE),
        f"{name}: routine verification leaked into report guidance",
    )

for wrapper, source in (
    (AXIOM_WRAPPER, "scripts/Axioms.lean?ref=codex%2Fnative-scoped-checks"),
    (LINT_WRAPPER, "scripts/lint-env.sh?ref=codex%2Fnative-scoped-checks"),
):
    check(source in wrapper, f"wrapper does not read {source} from the TauCeti branch")
    check(
        "Accept: application/vnd.github.raw" in wrapper,
        "wrapper does not request GitHub's byte-preserving raw response",
    )
    check("trap cleanup EXIT" in wrapper, "wrapper does not clean up its temporary source")

check(
    'lake env lean --run "$source_file" "$@"' in AXIOM_WRAPPER,
    "axiom wrapper does not forward arguments to the remote Lean program",
)
check(
    "bash -c 'source \"$1\" \"${@:2}\"' \"$repo_root/scripts/lint-env.sh\" \"$source_file\" \"$@\""
    in LINT_WRAPPER,
    "lint wrapper does not preserve the target script's checkout-relative $0",
)

print(f"scoped_check_prompts: PASS ({len(PROMPTS)} prompts)")
