#!/usr/bin/env python3
"""Structured Codex/Claude streams become useful, bounded, provider-neutral logs."""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


def render(provider, events):
    renderer = tc.AgentLogRenderer(provider)
    return [line for event in events for line in renderer.render_line(json.dumps(event) + "\n")]


codex = render(
    "codex",
    [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "r1", "type": "reasoning", "text": "Inspect the launch path."},
        },
        {
            "type": "item.started",
            "item": {
                "id": "c1",
                "type": "command_execution",
                "command": "uv run python tests/agent_logging.py",
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "c1",
                "type": "command_execution",
                "command": "uv run python tests/agent_logging.py",
                "aggregated_output": "PASS\n",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "f1",
                "type": "file_change",
                "changes": [{"path": "tauceti_worker/agents.py", "kind": "update"}],
                "diff": "THIS FULL DIFF MUST NEVER APPEAR",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "m1", "type": "agent_message", "text": "Implemented and tested."},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            },
        },
    ],
)
codex_text = "\n".join(codex)
check("Codex preserves reasoning narration", "[reasoning]\nInspect the launch path." in codex_text, True)
check("Codex reports a command once", codex_text.count("uv run python tests/agent_logging.py"), 1)
check("Codex preserves bounded command results", "[command output]\nPASS" in codex_text, True)
check("Codex reduces file changes to paths", "[files] update tauceti_worker/agents.py" in codex_text, True)
check("Codex drops full diffs", "THIS FULL DIFF MUST NEVER APPEAR" in codex_text, False)
check("Codex preserves final response", "[assistant]\nImplemented and tested." in codex_text, True)
check("Codex reports compact usage", "[done] input=100 cached=80 output=20 reasoning=5" in codex_text, True)

claude = render(
    "claude",
    [
        {"type": "system", "subtype": "init", "model": "claude-opus-5", "session_id": "session-1"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "private chain of thought"},
                    {"type": "text", "text": "I found the launch path."},
                    {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "agents.py"}},
                ]
            },
            "parent_tool_use_id": None,
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": [{"type": "text", "text": "def run_agent_proc(...):"}],
                        "is_error": False,
                    }
                ]
            },
        },
        {
            "type": "system",
            "subtype": "api_retry",
            "attempt": 1,
            "max_retries": 3,
            "retry_delay_ms": 250,
            "error": "overloaded",
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Implemented and tested."}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Implemented and tested.",
            "num_turns": 3,
            "duration_ms": 12500,
        },
    ],
)
claude_text = "\n".join(claude)
check("Claude preserves intermediate narration", "[assistant]\nI found the launch path." in claude_text, True)
check("Claude reports tool input", '[tool] Read {"file_path":"agents.py"}' in claude_text, True)
check("Claude reports tool result", "[tool result] Read [ok]\ndef run_agent_proc(...):" in claude_text, True)
check("Claude reports retries", "[retry 1/3] overloaded, retrying in 250ms" in claude_text, True)
check("Claude omits hidden thinking", "private chain of thought" in claude_text, False)
check("Claude preserves final response once", claude_text.count("Implemented and tested."), 1)
check("Claude reports completion", "[done] turns=3 duration=12.5s" in claude_text, True)

renderer = tc.AgentLogRenderer("codex")
check("Bubble/plain diagnostics pass through", renderer.render_line("lake cache warmed\n"), ["lake cache warmed"])
renderer.render_line(json.dumps({"type": "turn.started"}) + "\n")
check(
    "unknown rolling diffs stay suppressed",
    renderer.render_line(json.dumps({"type": "turn.diff.updated", "diff": "huge"}) + "\n"),
    [],
)

huge = "a" * 9000
bounded = "\n".join(
    render(
        "codex",
        [
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "c2",
                    "type": "command_execution",
                    "command": "test",
                    "aggregated_output": huge,
                    "status": "completed",
                },
            },
        ],
    )
)
check("tool output is truncated", "chars omitted" in bounded and len(bounded) < 5000, True)

# Both output modes must consume the exact same incremental renderer.
raw_events = [
    json.dumps({"type": "turn.started"}) + "\n",
    json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "m2", "type": "agent_message", "text": "Same transcript."},
        }
    )
    + "\n",
    json.dumps({"type": "turn.completed", "usage": {}}) + "\n",
]


class FakeProc:
    def __init__(self):
        self.stdout = iter(raw_events)

    def wait(self):
        return 0


class FailingProc(FakeProc):
    def wait(self):
        return 7


saved_popen = tc.agents.subprocess.Popen
saved_stream = os.environ.get("TAUCETI_STREAM")
try:
    tc.agents.subprocess.Popen = lambda *_a, **_k: FakeProc()
    with tempfile.TemporaryDirectory() as td:
        logdir = Path(td)
        streamed = io.StringIO()
        os.environ["TAUCETI_STREAM"] = "1"
        with redirect_stdout(streamed):
            stream_rc = tc.run_agent_proc(
                ["agent"],
                env={},
                logdir=logdir,
                label="agent-codex",
                provider="codex",
            )
        os.environ.pop("TAUCETI_STREAM")
        log_rc = tc.run_agent_proc(
            ["agent"],
            env={},
            logdir=logdir,
            label="agent-codex",
            provider="codex",
        )
        logs = list(logdir.glob("agent-codex-*.log"))
        check("both output modes return the child status", (stream_rc, log_rc), (0, 0))
        check("logged mode created one transcript", len(logs), 1)
        check("stream and logfile content match", streamed.getvalue().strip(), logs[0].read_text().strip())

    tc.agents.subprocess.Popen = lambda *_a, **_k: FailingProc()
    with tempfile.TemporaryDirectory() as td:
        failed_tail = io.StringIO()
        with redirect_stdout(failed_tail):
            failed_rc = tc.run_agent_proc(
                ["agent"],
                env={},
                logdir=Path(td),
                label="agent-codex",
                provider="codex",
            )
        check("nonzero child status is preserved", failed_rc, 7)
        check("nonzero logfile tail preserves the final response", "Same transcript." in failed_tail.getvalue(), True)
finally:
    tc.agents.subprocess.Popen = saved_popen
    if saved_stream is None:
        os.environ.pop("TAUCETI_STREAM", None)
    else:
        os.environ["TAUCETI_STREAM"] = saved_stream

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
