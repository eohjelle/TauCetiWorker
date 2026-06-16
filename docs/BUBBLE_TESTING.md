# Bubble end-to-end testing (Incus-equipped machine)

`tauceti` defaults every model-running mode to **bubble** (the sandbox); `--host` opts out. The host
paths are fully tested, but the bubble paths can only run on a machine with a working **Incus**
runtime, which the primary dev host does not have. This doc is the checklist for validating the bubble
paths on such a machine. Until it passes, keep `loop.sh`/`round.sh` as the production worker and do
**not** delete them (plan milestone M14 is gated on this).

Everything here mutates a real repo. Use scratch PRs you control, and a distinct `--worker-id` so the
claims/state don't collide with production workers.

## 0. Prerequisites

```bash
# Incus (NixOS): add to configuration.nix, then rebuild + init
#   virtualisation.incus.enable = true;
#   networking.nftables.enable = true;
#   users.users.<you>.extraGroups = [ "incus-admin" ];
sudo nixos-rebuild switch
sudo incus admin init --minimal

# bubble CLI
uv tool install git+https://github.com/kim-em/bubble.git
bubble list          # must succeed (probes the runtime), not "Incus is required"

# the worker checkout
cd TauCetiWorker
./tauceti doctor      # 'bubble' must show [ok]; gh auth ok; codex/claude creds present
```

`tauceti doctor` is the fast gate: it must report `bubble [ok]`. If it says MISSING, the CLI isn't on
PATH; if `bubble list` fails, the Incus runtime isn't up.

## 1. No-container sanity (cheap, run first)

These need no Incus and re-confirm the wiring on the new host:

```bash
./tauceti status                       # dashboard renders; quota line sane
python3 tests/parity_selectors.py      # 0 selector mismatches
bash tests/lifecycle.sh                # 5/5 (flock, fd-leak, timeout, signals)
python3 tests/agent_cmds.py            # host agent argv byte-for-byte
./tauceti work --dry-run               # picks one unit; prints sandbox=bubble (the opt-out default)

# Bubble command construction WITHOUT opening a container (prints argv, returns 0):
TAUCETI_WORKER_ID=echo TAUCETI_CLAIM_SH=/tmp/stub-claim.sh TAUCETI_AGENT_ECHO=1 \
  ./tauceti work --only roadmap --codex      # expect a `bubble open ... --command "env PATH=/opt/round:$PATH ... codex exec ..."` line
```

(`/tmp/stub-claim.sh` = a script that just `exit 0`s, so no real claim ref is written.)

## 2. Authoring/fixing in bubble (the existing run_in_bubble path)

For each workflow, run ONE real round against a scratch PR, with `--codex` and again with `--claude`.
Bubble is the default, so do **not** pass `--bubble` (it's a deprecated no-op) and do **not** pass
`--host`:

```bash
WID=bubble-test
# fix: needs a scratch PR of yours with a blocking review at head
TAUCETI_WORKER_ID=$WID ./tauceti work --only fix     --codex
# fix-ci: a scratch PR whose `build` check is red at head
TAUCETI_WORKER_ID=$WID ./tauceti work --only fix-ci  --codex
# rebase: a scratch PR made CONFLICTING vs main
TAUCETI_WORKER_ID=$WID ./tauceti work --only rebase  --codex
# bump: only fires if mathlib master is ahead of the pin and no bump PR is open
TAUCETI_WORKER_ID=$WID ./tauceti work --only bump     --codex
# roadmap: authors a new PR (stages TauCetiRoadmap/TauCetiReview as read-only mounts)
TAUCETI_WORKER_ID=$WID ./tauceti work --only roadmap  --claude
```

For each, verify:
- [ ] The container opens, the agent runs `lake exe cache get` / `lake build` **inside** it, and the
      round exits 0 (or a clean no-progress if nothing eligible).
- [ ] The push happened through bubble's auth proxy via `git-safe-push` (the host `kim-em` token never
      entered the container). Confirm the PR updated / opened.
- [ ] **The container is popped afterward** (`bubble list` shows no leftover `tauceti-worker-<wid>`).
      Kill a round mid-build (Ctrl-C / SIGTERM) and confirm cleanup still pops it.
- [ ] No host config leaked in: the agent had no `~/.claude/CLAUDE.md`, skills, or the other model's
      credential (only the one work-model credential was seeded).
- [ ] The shared Mathlib cache is an overlay (a round can't poison a later round's build).

## 3. Review in bubble (NEW path — review_in_bubble — highest risk)

This runs `uvx tauceti-review` **inside** bubble: the container boundary on the outside, the engine's
own read-only-tool + throwaway-HOME isolation on the inside (defense in depth). It is **untested** —
validate it carefully, the FORK case first.

```bash
WID=bubble-review
# (a) FIRST: a PR from a community FORK (the integration risk — proxy + base-repo PR API)
TAUCETI_WORKER_ID=$WID ./tauceti work --only review --codex      # picks a build-green, unreviewed PR
# (b) then a same-repo PR
```

Integration unknowns to confirm (these are the likely failure points):
- [ ] **`uvx` is available in the bubble image.** If not, the engine can't launch — the image needs
      `uv`. (Report this to kim-em/bubble if missing.)
- [ ] **A fork PR can be read through the repo-scoped proxy** (the engine fetches the PR diff/context
      via the base-repo PR API). If the proxy blocks fork branch access, review-in-bubble needs a proxy
      policy change — capture the exact failure.
- [ ] The reviewer model's credential is seeded (`--codex-credentials` / `--claude-credentials`); the
      engine authenticates inside its throwaway HOME.
- [ ] The scoreboard comment posts to the PR through the proxy (`gh api graphql`, repo-scoped — OK).
- [ ] The store is container-local/ephemeral (fine — the GitHub scoreboard is the source of truth).
- [ ] `--host` still falls back to host-side review (`uvx tauceti-review` on the host) and works.

If a fork PR can't be reviewed in bubble, that's the one finding that may need a bubble-side change;
note it and fall back to `--host` for review until resolved.

## 4. OpenRouter (DeepSeek / MiniMax) in bubble

Expected to **fail early** until kim-em/bubble#299 lands `pi` + openrouter.ai egress in the image:

```bash
./tauceti work --only roadmap --deepseek     # expect: "--agent deepseek requires --host until ... bubble#299"
./tauceti work --only roadmap --deepseek --host   # works today (host path)
```

Once the image has `pi`, set `TAUCETI_ALLOW_OPENROUTER_BUBBLE=1` and re-test the bubble path.

## 5. The loop, in bubble

```bash
# safest first: housekeeping-only loop (no model, no bubble)
TAUCETI_WORKER_ID=loop ./tauceti work --loop --only merge

# a real bubble loop (auto model, bubble default). Ctrl-C must stop the current round and exit.
TAUCETI_WORKER_ID=loop ./tauceti work --loop
```

Verify: the loop spawns each round as a child, a hung round is torn down at `ROUND_TIMEOUT` (the whole
process group, incl. the container), Ctrl-C reaches the round and pops the bubble, and the back-off
escalates on consecutive no-progress rounds.

## 6. Multi-worker (optional)

```bash
./tauceti work --loop --worker-id alice --isolate-home --only review
./tauceti work --loop --worker-id bob   --isolate-home --only roadmap
```

Verify two workers don't collide: distinct state/checkout/bubble per worker-id, claims dedup branch
work, and `git-safe-push`'s branch CAS rejects the loser of a concurrent push (it STOPs, doesn't
clobber).

## Sign-off

Bubble validation passes when sections 2, 3, and 5 are green for both `--codex` and `--claude`,
review-in-bubble works for a fork PR (or the gap is documented and `--host` review is the agreed
fallback), and no container leaks. Then M14 (delete `loop.sh`/`round.sh`, final README) can proceed.
