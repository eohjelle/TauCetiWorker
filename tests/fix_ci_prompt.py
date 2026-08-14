#!/usr/bin/env python3
"""Authoring prompts build changed modules and leave repository-wide checks to CI."""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROMPT_NAMES = ("fix.md", "fix-ci.md", "bump.md", "rebase.md", "roadmap.md")

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


axioms = "tauceti-axioms --changed-from origin/main"
lint = "tauceti-lint-env --changed-from origin/main"
axiom_wrapper = (REPO / "scripts" / "tauceti-axioms").read_text()
lint_wrapper = (REPO / "scripts" / "tauceti-lint-env").read_text()

for prompt_name in PROMPT_NAMES:
    prompt = (REPO / "prompts" / prompt_name).read_text()
    check(f"{prompt_name} requires scoped module builds", "Run `lake build` only on" in prompt)
    check(
        f"{prompt_name} has no bare repository-wide build",
        re.search(r"(?m)^lake build(?: --iofail)?$", prompt) is None,
    )
    check(
        f"{prompt_name} does not prescribe change-discovery mechanics",
        "git diff --name-only" not in prompt and "TauCeti.<Module>" not in prompt,
    )
    check(f"{prompt_name} uses changed-module axiom wrapper", axioms in prompt)
    check(f"{prompt_name} uses changed-module lint wrapper", lint in prompt)
    check(f"{prompt_name} no longer uses the bundled helper", "tauceti-local-checks" not in prompt)
    check(
        f"{prompt_name} does not bypass the axiom wrapper",
        "lake exe axioms --changed-from" not in prompt,
    )
    check(
        f"{prompt_name} does not bypass the lint wrapper",
        "scripts/lint-env.sh --changed-from" not in prompt,
    )
    check(
        f"{prompt_name} has no repository-wide axiom command",
        re.search(r"(?m)^lake exe axioms$", prompt) is None,
    )
    check(
        f"{prompt_name} has no repository-wide lint command",
        re.search(r"(?m)^(?:lake env )?bash scripts/lint-env\.sh$", prompt) is None,
    )
    check(
        f"{prompt_name} identifies CI as the repository-wide backstop",
        "CI" in prompt and "repository-wide" in prompt,
    )

    report = prompt.split("## Report", 1)[1]
    check(
        f"{prompt_name} report guidance omits routine verification",
        not re.search(r"\b(?:lake|axiom|lint|verification)\b", report, re.IGNORECASE),
    )

fix_ci = (REPO / "prompts" / "fix-ci.md").read_text()
diagnosis, final_gate = fix_ci.split("## Final gate before pushing", 1)
check(
    "fix-ci inspects logs before rebasing and local compilation",
    0
    <= diagnosis.find("gh run view <run-id>")
    < diagnosis.find("git rebase origin/main")
    < diagnosis.find("smallest failing Lean module or declaration"),
)
check("fix-ci diagnosis keeps targeted reproduction", "smallest failing Lean module or declaration" in diagnosis)
check("fix-ci diagnosis uses scoped-check wrappers", axioms in diagnosis and lint in diagnosis)
check("fix-ci diagnosis does not run the complete gate", "lake exe cache get" not in diagnosis)
check(
    "fix-ci final gate orders targeted build guidance and audits",
    0
    <= final_gate.find("Run `lake build` only on")
    < final_gate.find(axioms)
    < final_gate.find(lint),
)
check(
    "only fix-ci retains targeted module-system failure diagnosis",
    all(
        ("lake exe module-system" in (REPO / "prompts" / name).read_text()) == (name == "fix-ci.md")
        for name in PROMPT_NAMES
    ),
)
check(
    "fix-ci lint guidance preserves non-local-effect diagnosis",
    all(term in diagnosis for term in ("`simp` lemma", "instance", "import", "attribute")),
)

for prompt_name in ("fix.md", "fix-ci.md", "bump.md", "rebase.md"):
    prompt = (REPO / "prompts" / prompt_name).read_text()
    check(f"{prompt_name} rebases the writable branch", "git rebase origin/main" in prompt)

roadmap = (REPO / "prompts" / "roadmap.md").read_text()
roadmap_commands = [line.strip() for line in roadmap.splitlines()]
check("roadmap leaves cache warming to pre-agent setup", "lake exe cache get" not in roadmap_commands)

for wrapper, source in (
    (axiom_wrapper, "scripts/Axioms.lean?ref=codex%2Fnative-scoped-checks"),
    (lint_wrapper, "scripts/lint-env.sh?ref=codex%2Fnative-scoped-checks"),
):
    check(f"wrapper reads {source} from the TauCeti branch", source in wrapper)
    check(
        "wrapper requests GitHub's byte-preserving raw response",
        "Accept: application/vnd.github.raw" in wrapper,
    )
    check("wrapper cleans up its temporary source", "trap cleanup EXIT" in wrapper)

check(
    "axiom wrapper forwards its arguments",
    'lake env lean --run "$source_file" "$@"' in axiom_wrapper,
)
check(
    "lint wrapper preserves the target script's checkout-relative $0",
    'bash -c \'source "$1" "${@:2}"\' "$repo_root/scripts/lint-env.sh" "$source_file" "$@"' in lint_wrapper,
)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
