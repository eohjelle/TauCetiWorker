#!/usr/bin/env python3
"""Existing Codex credits require an explicit override and positive, usable telemetry. Offline."""

import copy
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


payload = {
    "rate_limit": {
        "allowed": False,
        "limit_reached": True,
        "primary_window": {"used_percent": 100, "limit_window_seconds": 604800, "reset_after_seconds": 300000},
        "secondary_window": None,
    },
    "credits": {"has_credits": True, "overage_limit_reached": False},
    "spend_control": {"reached": False},
}
q = tc.Quota.__new__(tc.Quota)
eligible = q._codex_from_payload(payload)
check("confirmed credits remain separate from paced availability", (eligible.available, eligible.model), (False, None))
check("explicit Codex override can use confirmed credits", tc._ignore_quota_verdict(None, eligible), "credits")

for section, field, values in (
    ("credits", "has_credits", (False, None, 1, "true")),
    ("credits", "overage_limit_reached", (True, None, 0, "false")),
    ("spend_control", "reached", (True, None, 0, "false")),
    ("rate_limit", "allowed", (True, None, 0, "false")),
    ("rate_limit", "limit_reached", (False, None, 1, "true")),
):
    for value in values:
        data = copy.deepcopy(payload)
        data[section][field] = value
        check(f"{section}.{field}={value!r} does not admit credits", q._codex_from_payload(data).credits_available, False)
    data = copy.deepcopy(payload)
    del data[section][field]
    check(f"missing {section}.{field} does not admit credits", q._codex_from_payload(data).credits_available, False)

for section in ("credits", "spend_control", "rate_limit"):
    for value in (None, [], "corrupt"):
        data = copy.deepcopy(payload)
        data[section] = value
        check(f"malformed {section}={value!r} does not admit credits", q._codex_from_payload(data).credits_available, False)

for field, values in (
    ("used_percent", (None, "100", True, float("nan"), float("inf"), -1, 101)),
    ("reset_after_seconds", (None, "300000", True, float("nan"), 0, -1)),
):
    for value in values:
        data = copy.deepcopy(payload)
        data["rate_limit"]["primary_window"][field] = value
        check(f"invalid {field}={value!r} despite limit_reached stays blocked", q._codex_from_payload(data).credits_available, False)

for secondary in ({}, "corrupt", {"used_percent": 10}):
    data = copy.deepcopy(payload)
    data["rate_limit"]["secondary_window"] = secondary
    check("unknown sibling window prevents credit admission", q._codex_from_payload(data).credits_available, False)
data = copy.deepcopy(payload)
data["rate_limit"]["primary_window"] = None
check("no windows prevents credit admission", q._codex_from_payload(data).credits_available, False)
data = copy.deepcopy(payload)
data["rate_limit"]["primary_window"]["used_percent"] = 10
check("unexplained denial is not proven subscription exhaustion", q._codex_from_payload(data).credits_available, False)

for p in (replace(eligible, error="codex usage HTTP 401"), replace(eligible, rate_limited=True)):
    for chosen in (None, "codex"):
        check("error/429 beats cached credit or chosen model", tc._ignore_quota_verdict(chosen, p), "wait")
check("Claude cannot use Codex credit exception", tc._ignore_quota_verdict(None, replace(eligible, name="claude")), "wait")

# Exercise actual ordinary selection and the one-round entry point: credits alone never make
# forced/automatic paced workers eligible, while the explicit override can launch Codex.
q.codex = lambda **_kw: eligible
q.claude = lambda **_kw: tc.Provider("claude", False, None)
check("ordinary pinned Codex remains blocked", q.choose("codex")[0], None)
check("automatic model selection remains blocked", q.choose(None)[0], None)
saved_choose = tc.loop.choose_model
choose_calls = []
tc.loop.choose_model = lambda _cfg, agent, _cmd, **kw: choose_calls.append(kw) or q.choose(
    None if agent == "auto" else agent, **kw
)
try:
    check(
        "explicit pinned override resolves a real round",
        tc.loop.resolve_work_model(object(), "codex", dry=False, ignore_quota=True, fresh=True),
        ("codex", False),
    )
    tc.loop.resolve_work_model(object(), "codex", dry=False, ignore_quota=True, fresh=False)
    check("loop child also refreshes credit authorization", choose_calls[-1]["refresh"], True)
    try:
        tc.loop.resolve_work_model(object(), "codex", dry=False, ignore_quota=False, fresh=True)
    except tc.NoProgress:
        refused = True
    else:
        refused = False
    check("a round without --ignore-quota refuses credit spending", refused, True)
    try:
        tc.loop.resolve_work_model(object(), "auto", dry=False, ignore_quota=True, fresh=True)
    except SystemExit:
        refused = True
    else:
        refused = False
    check("automatic override remains forbidden", refused, True)
finally:
    tc.loop.choose_model = saved_choose

# The production reader preserves ordinary cache behavior across provider failures, but credit
# admission must not survive those failures. No real credentials, files or HTTP are used here.
q = tc.Quota.__new__(tc.Quota)
q.cfg = SimpleNamespace(home=Path("/unused"))
q._codex_credentials_store = lambda: None
q._codex_creds = lambda: {"tokens": {"access_token": "fake-test-token"}}
q._codex_account_id = lambda _auth: "fake-test-account"
q._cached_codex = lambda _fp: (payload, time.time())
q._cooldown = lambda *_a: (0, None)
q._start_cooldown = lambda *_a: None
q._store_raw = lambda *_a: None
saved_http, saved_mirror = tc.quota._http_get_json, tc.quota.mirror_creds
tc.quota.mirror_creds = lambda _cfg: None
try:
    for code in (401, 403, 429, 500):
        tc.quota._http_get_json = lambda *_a, code=code: (code, {}, 60)
        p = q.codex(refresh=True)
        check(f"HTTP {code} never authorizes cached credits", tc._ignore_quota_verdict(None, p), "wait")
        check(f"cached read after HTTP {code} cannot resurrect credit authorization", q.codex().credits_available, False)

    def offline(*_args):
        raise tc.GitHubError("network unavailable")

    tc.quota._http_get_json = offline
    check("network failure never authorizes cached credits", q.codex(refresh=True).credits_available, False)
    check("cached read after network failure cannot resurrect credit authorization", q.codex().credits_available, False)
    for code in (401, 403, 429, 500):
        q._cooldown = lambda *_a, code=code: (60, code)
        check(f"HTTP {code} cooldown never authorizes cached credits", q.codex().credits_available, False)
    q._cooldown = lambda *_a: (0, None)
    tc.quota._http_get_json = lambda *_a: (200, payload, None)
    check("fresh successful telemetry authorizes explicit credits", q.codex(refresh=True).credits_available, True)
    check("cached subscription snapshot never authorizes credit spending", q.codex().credits_available, False)
    q._codex_creds = lambda: {"tokens": {}}
    check("missing access token cannot reuse cached credits", q.codex().credits_available, False)
finally:
    tc.quota._http_get_json, tc.quota.mirror_creds = saved_http, saved_mirror

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
