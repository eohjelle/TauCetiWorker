"""Readable, bounded transcripts for structured coding-agent event streams.

Codex and Claude expose different JSONL protocols for non-interactive runs.  The
worker asks each CLI for that structured stream, then normalizes the useful
events here before either writing a logfile or displaying ``--stream`` output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

_DETAIL_LIMIT = 4000
_MAX_FILE_CHANGES = 30


def _clip(value: object, limit: int = _DETAIL_LIMIT) -> str:
    """Render a bounded value while retaining both its beginning and end."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = text.strip()
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n… [{omitted} chars omitted] …\n{text[-tail:]}"


def _block(label: str, value: object) -> str | None:
    text = _clip(value)
    return f"[{label}]\n{text}" if text else None


def _message_text(value: object) -> str:
    """Extract readable text from Claude/MCP content without assuming one schema."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_message_text(part) for part in value]
        return "\n".join(part for part in parts if part)
    if not isinstance(value, dict):
        return str(value)

    kind = value.get("type")
    if kind in {"image", "image_url"}:
        return "[image]"
    for key in ("text", "message", "error"):
        text = value.get(key)
        if isinstance(text, str):
            return text
    if "content" in value:
        text = _message_text(value["content"])
        if text:
            return text
    return _clip(value)


def _error_text(value: object) -> str:
    if isinstance(value, dict):
        message = value.get("message") or value.get("error")
        if message:
            return _message_text(message)
    return _message_text(value)


@dataclass
class AgentLogRenderer:
    """Stateful line renderer for one agent subprocess.

    Non-JSON output is preserved verbatim. That matters for Bubble's setup
    preamble and for CLI diagnostics on stderr, which the runner merges into the
    same pipe. Unknown structured events are summarized rather than dumped, so
    a schema addition cannot reintroduce giant diffs or token deltas.
    """

    provider: str | None
    _structured_started: bool = False
    _started_items: set[str] = field(default_factory=set)
    _tool_names: dict[str, str] = field(default_factory=dict)
    _completed_tools: set[str] = field(default_factory=set)
    _last_assistant_text: str | None = None
    _last_todo: str | None = None

    def render_line(self, raw_line: str) -> list[str]:
        line = raw_line.rstrip("\r\n")
        if self.provider not in {"codex", "claude"}:
            return [line]
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return [line]
        if not isinstance(event, dict):
            return [line]

        event_type = event.get("type")
        known = (
            self._known_codex_event(event_type) if self.provider == "codex" else self._known_claude_event(event_type)
        )
        if not self._structured_started and not known:
            return [line]
        self._structured_started = True
        return self._render_codex(event) if self.provider == "codex" else self._render_claude(event)

    @staticmethod
    def _known_codex_event(event_type: object) -> bool:
        return isinstance(event_type, str) and (
            event_type in {"thread.started", "turn.started", "turn.completed", "turn.failed", "error"}
            or event_type.startswith("item.")
        )

    @staticmethod
    def _known_claude_event(event_type: object) -> bool:
        return isinstance(event_type, str) and event_type in {
            "system",
            "assistant",
            "user",
            "result",
            "stream_event",
            "rate_limit_event",
        }

    def _render_codex(self, event: dict) -> list[str]:
        event_type = str(event.get("type") or "")
        # Some richer Codex protocols publish rolling diff snapshots. They are
        # deliberately not part of a readable activity transcript.
        if "diff" in event_type or event_type.endswith(".delta"):
            return []
        if event_type == "thread.started":
            thread = event.get("thread_id")
            return [f"[session] codex {thread}"] if thread else []
        if event_type == "turn.started":
            return []
        if event_type == "turn.completed":
            usage = event.get("usage")
            if not isinstance(usage, dict):
                return ["[done]"]
            fields = (
                ("input", "input_tokens"),
                ("cached", "cached_input_tokens"),
                ("output", "output_tokens"),
                ("reasoning", "reasoning_output_tokens"),
            )
            parts = [f"{label}={usage[key]}" for label, key in fields if isinstance(usage.get(key), int)]
            return [f"[done] {' '.join(parts)}".rstrip()]
        if event_type == "turn.failed":
            detail = _error_text(event.get("error")) or "turn failed"
            return [f"[error] {detail}"]
        if event_type == "error":
            detail = _error_text(event.get("message") or event.get("error")) or "unknown Codex error"
            return [f"[error] {detail}"]
        if event_type.startswith("item."):
            item = event.get("item")
            if isinstance(item, dict):
                phase = event_type.removeprefix("item.")
                return self._render_codex_item(phase, item)
            return []
        return self._render_unknown("codex", event)

    def _render_codex_item(self, phase: str, item: dict) -> list[str]:
        kind = str(item.get("type") or "unknown")
        item_id = str(item.get("id") or "")

        if kind == "command_execution":
            return self._render_codex_command(phase, item, item_id)
        if kind == "mcp_tool_call":
            return self._render_codex_tool(phase, item, item_id)
        if kind == "file_change":
            return self._render_codex_files(phase, item)
        if kind == "todo_list":
            return self._render_codex_todo(item)
        if kind == "web_search":
            if phase == "started" or (phase == "completed" and item_id not in self._started_items):
                if item_id:
                    self._started_items.add(item_id)
                query = _clip(item.get("query") or "search")
                return [f"[web] {query}"]
            return []
        if phase != "completed":
            return []
        if kind == "agent_message":
            text = _message_text(item.get("text"))
            if text:
                self._last_assistant_text = text.strip()
            block = _block("assistant", text)
            return [block] if block else []
        if kind == "reasoning":
            block = _block("reasoning", item.get("text"))
            return [block] if block else []
        if kind == "error":
            detail = _error_text(item.get("message") or item.get("error")) or "unknown item error"
            return [f"[error] {detail}"]

        text = item.get("text") or item.get("message")
        if text:
            block = _block(kind, text)
            return [block] if block else []
        return [f"[item] {kind}"]

    def _render_codex_command(self, phase: str, item: dict, item_id: str) -> list[str]:
        command = _clip(item.get("command") or "")
        if phase == "started":
            if item_id:
                self._started_items.add(item_id)
            return [f"[command] $ {command}"] if command else ["[command]"]
        if phase != "completed":
            return []

        lines: list[str] = []
        if not item_id or item_id not in self._started_items:
            lines.append(f"[command] $ {command}" if command else "[command]")
        output = _block("command output", item.get("aggregated_output"))
        if output:
            lines.append(output)
        status, exit_code = item.get("status"), item.get("exit_code")
        if status == "failed" or (isinstance(exit_code, int) and exit_code != 0):
            suffix = f" (exit {exit_code})" if isinstance(exit_code, int) else ""
            lines.append(f"[command failed]{suffix}")
        return lines

    def _render_codex_tool(self, phase: str, item: dict, item_id: str) -> list[str]:
        server, tool = item.get("server"), item.get("tool")
        name = ".".join(str(part) for part in (server, tool) if part) or "tool"
        if item_id:
            self._tool_names[item_id] = name
        if phase == "started":
            if item_id:
                self._started_items.add(item_id)
            args = _clip(item.get("arguments"))
            return [f"[tool] {name}{' ' + args if args else ''}"]
        if phase != "completed" or (item_id and item_id in self._completed_tools):
            return []
        if item_id:
            self._completed_tools.add(item_id)

        lines: list[str] = []
        if not item_id or item_id not in self._started_items:
            args = _clip(item.get("arguments"))
            lines.append(f"[tool] {name}{' ' + args if args else ''}")
        error = _error_text(item.get("error"))
        if error or item.get("status") == "failed":
            lines.append(f"[tool error] {name}{': ' + error if error else ''}")
            return lines
        result = item.get("result")
        if isinstance(result, dict):
            result = result.get("structured_content") or result.get("content") or result
        text = _clip(_message_text(result))
        if text:
            lines.append(f"[tool result] {name} [ok]\n{text}")
        else:
            lines.append(f"[tool result] {name} [ok]")
        return lines

    def _render_codex_files(self, phase: str, item: dict) -> list[str]:
        if phase != "completed":
            return []
        changes = item.get("changes")
        rendered: list[str] = []
        if isinstance(changes, dict):
            changes = [{"path": path, "kind": kind} for path, kind in changes.items()]
        if isinstance(changes, list):
            for change in changes[:_MAX_FILE_CHANGES]:
                if not isinstance(change, dict):
                    continue
                path = change.get("path") or change.get("file")
                kind = change.get("kind") or change.get("type") or "update"
                if path:
                    rendered.append(f"{kind} {path}")
            if len(changes) > _MAX_FILE_CHANGES:
                rendered.append(f"… +{len(changes) - _MAX_FILE_CHANGES} more")
        status = item.get("status")
        label = "file changes failed" if status == "failed" else "files"
        return [f"[{label}] {', '.join(rendered) if rendered else 'details unavailable'}"]

    def _render_codex_todo(self, item: dict) -> list[str]:
        todos = item.get("items")
        if not isinstance(todos, list):
            return []
        parts = []
        for todo in todos:
            if not isinstance(todo, dict):
                continue
            mark = "x" if todo.get("completed") else " "
            parts.append(f"[{mark}] {todo.get('text', '')}".rstrip())
        text = "\n".join(parts)
        if not text or text == self._last_todo:
            return []
        self._last_todo = text
        return [f"[plan]\n{text}"]

    def _render_claude(self, event: dict) -> list[str]:
        event_type = event.get("type")
        if event_type == "stream_event":
            return []  # token/content deltas; full messages arrive separately
        if event_type == "system":
            return self._render_claude_system(event)
        if event_type == "assistant":
            return self._render_claude_assistant(event)
        if event_type == "user":
            return self._render_claude_user(event)
        if event_type == "result":
            return self._render_claude_result(event)
        if event_type == "rate_limit_event":
            detail = event.get("message") or event.get("status") or event.get("rate_limit_info")
            return [f"[rate limit] {_clip(detail)}"] if detail else []
        return self._render_unknown("claude", event)

    def _render_claude_system(self, event: dict) -> list[str]:
        subtype = event.get("subtype")
        if subtype == "init":
            details = ["claude"]
            if event.get("model"):
                details.append(str(event["model"]))
            if event.get("session_id"):
                details.append(str(event["session_id"]))
            return [f"[session] {' '.join(details)}"]
        if subtype == "api_retry":
            attempt = event.get("attempt")
            maximum = event.get("max_retries")
            delay = event.get("retry_delay_ms")
            error = event.get("error") or "request failed"
            count = f" {attempt}/{maximum}" if attempt is not None and maximum is not None else ""
            wait = f", retrying in {delay}ms" if delay is not None else ""
            return [f"[retry{count}] {error}{wait}"]
        if subtype in {"hook_started", "hook_progress"}:
            return []
        detail = event.get("message") or event.get("error") or event.get("status")
        return [f"[system] {subtype}: {_clip(detail)}"] if detail else ([f"[system] {subtype}"] if subtype else [])

    def _render_claude_assistant(self, event: dict) -> list[str]:
        message = event.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return []

        lines: list[str] = []
        subagent = event.get("parent_tool_use_id") is not None
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = _message_text(block.get("text"))
                if text:
                    self._last_assistant_text = text.strip()
                    rendered = _block("assistant/subagent" if subagent else "assistant", text)
                    if rendered:
                        lines.append(rendered)
            elif kind == "tool_use":
                tool_id = str(block.get("id") or "")
                if tool_id and tool_id in self._started_items:
                    continue
                name = str(block.get("name") or "tool")
                if tool_id:
                    self._started_items.add(tool_id)
                    self._tool_names[tool_id] = name
                args = _clip(block.get("input"))
                lines.append(f"[tool] {name}{' ' + args if args else ''}")
            # Deliberately omit raw thinking blocks. Text narration provides
            # observable progress without persisting hidden chain-of-thought.
        return lines

    def _render_claude_user(self, event: dict) -> list[str]:
        message = event.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if not isinstance(content, list):
            return []

        lines: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id") or "")
            if tool_id and tool_id in self._completed_tools:
                continue
            if tool_id:
                self._completed_tools.add(tool_id)
            name = self._tool_names.get(tool_id, "tool")
            text = _message_text(block.get("content"))
            if block.get("is_error"):
                lines.append(f"[tool error] {name}{': ' + _clip(text) if text else ''}")
            else:
                detail = _clip(text)
                lines.append(f"[tool result] {name} [ok]\n{detail}" if detail else f"[tool result] {name} [ok]")
        return lines

    def _render_claude_result(self, event: dict) -> list[str]:
        lines: list[str] = []
        result = _message_text(event.get("result"))
        failed = bool(event.get("is_error")) or event.get("subtype") not in {None, "success"}
        if result and result.strip() != self._last_assistant_text:
            rendered = _block("error" if failed else "assistant", result)
            if rendered:
                lines.append(rendered)
        elif failed:
            error = _error_text(event.get("error")) or str(event.get("subtype") or "Claude run failed")
            lines.append(f"[error] {error}")

        stats = []
        turns = event.get("num_turns")
        duration = event.get("duration_ms")
        if isinstance(turns, int):
            stats.append(f"turns={turns}")
        if isinstance(duration, (int, float)):
            stats.append(f"duration={duration / 1000:.1f}s")
        if not failed:
            lines.append(f"[done] {' '.join(stats)}".rstrip())
        return lines

    @staticmethod
    def _render_unknown(provider: str, event: dict) -> list[str]:
        event_type = str(event.get("type") or "unknown")
        if "diff" in event_type or "delta" in event_type or "progress" in event_type:
            return []
        detail = event.get("message") or event.get("error") or event.get("text")
        if detail:
            return [f"[{provider} {event_type}] {_clip(detail)}"]
        return [f"[{provider} event] {event_type}"]
