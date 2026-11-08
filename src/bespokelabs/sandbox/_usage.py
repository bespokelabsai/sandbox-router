"""Parse agent CLI output into a normalized :class:`Usage`.

Supports Claude Code, OpenCode, Grok Build, and Codex JSON output.
``Sandbox.run_agent`` is the single caller.
"""

from __future__ import annotations

import json
import math

from bespokelabs.sandbox.types import Usage


def parse_claude_result(stdout: str) -> dict | None:
    """Return Claude Code's final result record, or None if not found.

    Never raises on malformed/partial output — unparseable lines are skipped.
    """
    text = stdout.strip()
    if not text:
        return None

    # Fast path: the whole payload is one JSON value (`--output-format json`,
    # or a JSON array of stream records).
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list):
        for item in reversed(obj):
            if isinstance(item, dict) and _looks_like_result(item):
                return item
        return None

    # stream-json: scan NDJSON lines and keep the last result record.
    result: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(record, dict) and _looks_like_result(record):
            result = record
    return result


def usage_from_result(record: dict | None) -> Usage | None:
    """Build a :class:`Usage` from a parsed result record.

    Returns None when the record carries no token usage and no cost — i.e.
    there is nothing to attribute.
    """
    if record is None:
        return None
    usage = (
        record.get("usage") if isinstance(record.get("usage"), dict) else None
    )
    if usage is None and "total_cost_usd" not in record:
        return None
    usage = usage or {}
    return Usage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_int(usage.get("cache_creation_input_tokens")),
        llm_cost_usd=_float(record.get("total_cost_usd")),
    )


def parse_claude_usage(stdout: str) -> Usage | None:
    """Extract token usage and cost from Claude Code JSON output."""
    return usage_from_result(parse_claude_result(stdout))


def _json_records(stdout: str) -> list[dict]:
    """Read a JSON object, array, or JSONL, tolerating logs and partial lines."""
    try:
        value = json.loads(stdout)
    except ValueError:
        records = []
        for line in stdout.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _normalized_result(text: str, usage: Usage, events: list[dict]) -> dict:
    return {
        "result": text,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_tokens,
            "cache_creation_input_tokens": usage.cache_creation_tokens,
        },
        "events": events,
    }


def parse_codex_result(stdout: str) -> dict | None:
    """Normalize ``codex exec --json`` events and turn usage.

    Codex input includes cached tokens; split them into disjoint buckets.
    Output already includes reasoning. Standard Codex events omit dollar
    cost; only copy an explicitly reported total, never infer model prices.
    """
    events = [
        event
        for event in _json_records(stdout)
        if isinstance(event.get("type"), str)
        and event["type"]
        in {
            "thread.started",
            "turn.started",
            "turn.completed",
            "turn.failed",
            "item.started",
            "item.updated",
            "item.completed",
            "error",
        }
    ]
    if not events:
        return None
    text = ""
    turns = {}
    turn = 0
    thread_id = None
    for event in events:
        kind = event["type"]
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "turn.started":
            turn += 1
        elif kind == "item.completed":
            item = event.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                text = item["text"]
        elif kind in {"turn.completed", "turn.failed"}:
            if (
                isinstance(event.get("usage"), dict)
                or "total_cost_usd" in event
            ):
                # A repeated completion for this turn replaces its snapshot.
                turns[turn] = event
    usage = Usage()
    for event in turns.values():
        tokens = event.get("usage")
        tokens = tokens if isinstance(tokens, dict) else {}
        total_input = _int(tokens.get("input_tokens"))
        cached = min(total_input, _int(tokens.get("cached_input_tokens")))
        usage += Usage(
            input_tokens=total_input - cached,
            cache_read_tokens=cached,
            output_tokens=_int(tokens.get("output_tokens")),
            llm_cost_usd=_float(event.get("total_cost_usd")),
        )
    record = _normalized_result(text, usage, events)
    if any("total_cost_usd" in event for event in turns.values()):
        record["total_cost_usd"] = usage.llm_cost_usd
    if isinstance(thread_id, str):
        record["thread_id"] = thread_id
    return record


def parse_grok_result(stdout: str) -> dict | None:
    """Normalize Grok JSON and both headless JSON stream formats.

    Terminal spend is authoritative and must not be added to per-response
    usage. If interrupted, recover completed response usage by message ID.
    Grok's headless input excludes cache; output already includes reasoning.
    Original metadata, including incomplete-spend flags, stays in ``raw``.
    """
    events = _json_records(stdout)
    if not events:
        return None
    terminal = None
    spend = None
    texts = []
    responses = {}
    recognized = False
    for index, event in enumerate(events):
        kind = event.get("type")
        if kind is not None and not isinstance(kind, str):
            continue
        if kind in {"end", "result", "error"} or (
            kind is None and isinstance(event.get("text"), str)
        ):
            recognized = True
            terminal = event
            if (
                isinstance(event.get("usage"), dict)
                or "total_cost_usd" in event
            ):
                spend = event
        elif kind == "text" and isinstance(event.get("data"), str):
            recognized = True
            texts.append(event["data"])
        elif kind == "usage":
            recognized = True
            message_id = event.get("messageId")
            key = message_id if isinstance(message_id, str) else index
            responses[key] = event
        elif kind == "assistant" and isinstance(event.get("message"), dict):
            recognized = True
            # Subagent messages are accounted for by terminal spend only.
            if event.get("parent_tool_use_id") is not None:
                continue
            message = event["message"]
            message_id = message.get("id")
            key = message_id if isinstance(message_id, str) else index
            responses[key] = message
            content = message.get("content")
            if isinstance(content, list):
                texts = [
                    block["text"]
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ]
    if not recognized:
        return None
    usage = Usage()
    for response in [spend] if spend is not None else responses.values():
        usage += usage_from_result(response) or Usage()
    terminal = terminal or {}
    text = terminal.get("result", terminal.get("text"))
    record = {
        **terminal,
        **_normalized_result(
            text if isinstance(text, str) else "".join(texts), usage, events
        ),
    }
    # Missing or incomplete cost is unknown, not an inferred bill.
    record.pop("total_cost_usd", None)
    if (
        spend is not None
        and "total_cost_usd" in spend
        and not any(
            source.get(flag)
            for source in (spend, terminal)
            for flag in ("cost_is_partial", "usage_is_incomplete")
        )
    ):
        record["total_cost_usd"] = usage.llm_cost_usd
    return record


def parse_opencode_result(stdout: str) -> dict | None:
    """Normalize OpenCode NDJSON, summing usage across completed steps.

    Keep original events for diagnostics, ignore malformed lines, and count
    each part once if the CLI repeats an update. Reasoning tokens are included
    in output tokens because Usage has no separate reasoning field.
    """
    events = []
    parts = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") not in {
            "step_start",
            "step_finish",
            "text",
            "tool_use",
            "reasoning",
            "error",
        }:
            continue
        events.append(event)
        part = event.get("part")
        if isinstance(part, dict):
            part_id = part.get("id")
            key = (
                (event["type"], part_id)
                if isinstance(part_id, str)
                else len(events)
            )
            parts[key] = (event["type"], part)
    if not events:
        return None
    usage = Usage()
    texts = []
    for kind, part in parts.values():
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        if kind != "step_finish":
            continue
        tokens = part.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        usage = usage + Usage(
            input_tokens=_int(tokens.get("input")),
            output_tokens=_int(tokens.get("output"))
            + _int(tokens.get("reasoning")),
            cache_read_tokens=_int(cache.get("read")),
            cache_creation_tokens=_int(cache.get("write")),
            llm_cost_usd=_float(part.get("cost")),
        )
    return {
        "result": "\n\n".join(texts),
        "total_cost_usd": usage.llm_cost_usd,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_tokens,
            "cache_creation_input_tokens": usage.cache_creation_tokens,
        },
        "events": events,
    }


def _number(value: object) -> int | float:
    """Return a finite number, treating malformed CLI values as zero."""
    try:
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            return value
    except (OverflowError, TypeError, ValueError):
        pass
    return 0


def result_text(record: dict | None) -> str | None:
    """Return the assistant's text answer from a result record, if present."""
    if isinstance(record, dict) and isinstance(record.get("result"), str):
        return record["result"]
    return None


def _looks_like_result(record: dict) -> bool:
    return (
        record.get("type") == "result"
        or "total_cost_usd" in record
        or "usage" in record
    )


def _int(value: object) -> int:
    return max(0, int(_number(value)))


def _float(value: object) -> float:
    return max(0.0, float(_number(value)))
