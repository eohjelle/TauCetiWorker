#!/usr/bin/env bash
# Lifecycle tests (plan milestone 6) — flock, fd-leak negative test, timeout teardown, signal codes.
# Read-only: uses the TAUCETI_TEST_* hooks, never touches GitHub. Run from the repo root.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=$(command -v python3)
WID="lifecycle-test"
export TAUCETI_WORKER_ID="$WID"
LOCK="state/$WID/round.lock"
pass=0; fail=0
ok()  { echo "  [PASS] $1"; pass=$((pass+1)); }
no()  { echo "  [FAIL] $1"; fail=$((fail+1)); }

run() { "$PY" ./tauceti "$@"; }   # plain python3 (lifecycle hooks don't need rich)

echo "== 1. flock: a second concurrent round dies on the lock =="
TAUCETI_TEST_SLEEP=4 run _round >/tmp/lc1.log 2>&1 &
bg=$!; sleep 1
TAUCETI_TEST_SLEEP=4 run _round >/tmp/lc2.log 2>&1; rc=$?
grep -q "holds .*round.lock" /tmp/lc2.log && (( rc == 1 )) && ok "second round refused (rc=$rc)" || no "second round not refused (rc=$rc)"
wait $bg

echo "== 2. fd-leak negative: a sleeping grandchild must NOT keep the lock =="
TAUCETI_TEST_HOLD=30 run _round >/tmp/lc3.log 2>&1; rc=$?
(( rc == 0 )) || no "holding round exited rc=$rc"
# immediately try to take the lock again; the grandchild (sleep 30) is still alive but must not hold it
TAUCETI_TEST_SLEEP=0 run _round >/tmp/lc4.log 2>&1; rc=$?
(( rc == 0 )) && ok "second round took the lock immediately despite live grandchild" \
              || { no "second round blocked (rc=$rc) — grandchild leaked the lock fd"; cat /tmp/lc4.log; }

echo "== 3. timeout teardown: a wedged round + its grandchild are killed =="
# Start a round that holds the lock and sleeps long, with a sleeping grandchild, then kill its group.
TAUCETI_TEST_SLEEP=60 setsid "$PY" ./tauceti _round >/tmp/lc5.log 2>&1 &
child=$!; sleep 1
pgid=$(ps -o pgid= -p "$child" | tr -d ' ')
kill -TERM -- "-$pgid" 2>/dev/null
sleep 1
kill -0 "$child" 2>/dev/null && { no "round survived SIGTERM"; kill -KILL -- "-$pgid" 2>/dev/null; } || ok "round group torn down by SIGTERM"

echo "== 4. signal exit codes: SIGTERM→143, SIGINT→130 =="
TAUCETI_TEST_SLEEP=30 setsid "$PY" ./tauceti _round >/tmp/lc6.log 2>&1 &
c=$!; sleep 1; kill -TERM "$c"; wait "$c"; rc=$?
(( rc == 143 )) && ok "SIGTERM → 143" || no "SIGTERM → $rc (expected 143)"
TAUCETI_TEST_SLEEP=30 setsid "$PY" ./tauceti _round >/tmp/lc7.log 2>&1 &
c=$!; sleep 1; kill -INT "$c"; wait "$c"; rc=$?
(( rc == 130 )) && ok "SIGINT → 130" || no "SIGINT → $rc (expected 130)"

# cleanup the test grandchild from step 2 if still around
pkill -f "sleep 30" 2>/dev/null || true
echo
echo "lifecycle: $pass passed, $fail failed"
exit $(( fail > 0 ))
