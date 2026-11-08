"""Contracts for Codex exec and Grok Build headless output.

Wire references:
https://developers.openai.com/codex/noninteractive
https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from bespokelabs.sandbox import AsyncSandbox, Sandbox, Usage
from bespokelabs.sandbox._usage import (
    parse_codex_result,
    parse_grok_result,
    usage_from_result,
)
from bespokelabs.sandbox.types import SandboxConfig, SandboxResult


def stream(*events):
    return "\n".join(json.dumps(event) for event in events)


CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {
            "id": "item-1",
            "type": "agent_message",
            "text": "Checking files.",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item-2",
            "type": "command_execution",
            "aggregated_output": "ls",
        },
    },
    {
        "type": "item.completed",
        "item": {"id": "item-3", "type": "agent_message", "text": "Done."},
    },
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 30,
            "reasoning_output_tokens": 10,
        },
    },
]
GROK_USAGE = {
    "input_tokens": 20,
    "cache_read_input_tokens": 80,
    "cache_creation_input_tokens": 5,
    "output_tokens": 30,
    "reasoning_tokens": 10,
    "total_tokens": 135,
}
GROK_RESULT = {
    "text": "Done.",
    "sessionId": "grok-1",
    "stopReason": "end_turn",
    "usage": GROK_USAGE,
    "total_cost_usd": 0.125,
    "total_cost_usd_ticks": 1250000000,
}


def sandbox(stdout, exit_code=0):
    session = Mock()
    session.execute_command.return_value = SandboxResult(
        stdout=stdout, stderr="diagnostic", exit_code=exit_code
    )
    sb = Sandbox._from_session("local", session, SandboxConfig(backend="local"))
    return sb, session


class CodexParsingTests(unittest.TestCase):

    def test_final_answer_and_disjoint_token_buckets(self):
        record = parse_codex_result(stream(*CODEX_EVENTS))
        self.assertEqual(record["result"], "Done.")
        self.assertEqual(record["thread_id"], "thread-1")
        self.assertEqual(record["events"], CODEX_EVENTS)
        self.assertEqual(
            usage_from_result(record),
            Usage(input_tokens=20, cache_read_tokens=80, output_tokens=30),
        )
        self.assertEqual(usage_from_result(record).total_tokens, 130)
        self.assertNotIn("total_cost_usd", record)

    def test_multiple_turns_and_repeated_completion_snapshot(self):
        record = parse_codex_result(
            stream(
                *CODEX_EVENTS,
                CODEX_EVENTS[-1],
                {"type": "turn.started"},
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 7},
                    "total_cost_usd": 0.25,
                },
            )
        )
        usage = usage_from_result(record)
        self.assertEqual(usage.input_tokens, 27)
        self.assertEqual(usage.cache_read_tokens, 80)
        self.assertEqual(usage.llm_cost_usd, 0.25)

    def test_failed_turn_preserves_completed_usage_and_error(self):
        error = {"type": "turn.failed", "error": {"message": "Disconnected"}}
        record = parse_codex_result(stream(*CODEX_EVENTS, error))
        self.assertEqual(usage_from_result(record).total_tokens, 130)
        self.assertEqual(record["events"][-1], error)

    def test_cached_tokens_are_bounded_by_full_input(self):
        record = parse_codex_result(
            stream(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 5, "cached_input_tokens": 20},
                }
            )
        )
        self.assertEqual(usage_from_result(record).total_tokens, 5)
        self.assertEqual(usage_from_result(record).input_tokens, 0)


class GrokParsingTests(unittest.TestCase):

    def test_single_pretty_json_includes_cost_and_metadata(self):
        record = parse_grok_result(json.dumps(GROK_RESULT, indent=2))
        self.assertEqual(record["result"], "Done.")
        self.assertEqual(record["sessionId"], "grok-1")
        self.assertEqual(record["total_cost_usd_ticks"], 1250000000)
        self.assertEqual(
            usage_from_result(record),
            Usage(
                input_tokens=20,
                output_tokens=30,
                cache_read_tokens=80,
                cache_creation_tokens=5,
                llm_cost_usd=0.125,
            ),
        )
        self.assertEqual(usage_from_result(record).total_tokens, 135)

    def test_stream_terminal_usage_overrides_response_usage(self):
        events = [
            {"type": "text", "data": "Do"},
            {"type": "text", "data": "ne."},
            {"type": "usage", "messageId": "msg-1", "usage": GROK_USAGE},
            {"type": "end", **GROK_RESULT},
        ]
        del events[-1]["text"]
        record = parse_grok_result(stream(*events))
        self.assertEqual(record["result"], "Done.")
        self.assertEqual(record["events"], events)
        self.assertEqual(usage_from_result(record).total_tokens, 135)
        self.assertEqual(usage_from_result(record).llm_cost_usd, 0.125)

    def test_partial_stream_sums_distinct_responses_without_duplicates(self):
        first = {"type": "usage", "messageId": "a", "usage": GROK_USAGE}
        error = {"type": "error", "message": "Disconnected"}
        record = parse_grok_result(
            stream(
                {"type": "text", "data": "Partial"},
                first,
                first,
                {
                    "type": "usage",
                    "messageId": "b",
                    "usage": {"input_tokens": 3},
                },
                error,
            )
        )
        self.assertEqual(record["result"], "Partial")
        self.assertEqual(record["message"], "Disconnected")
        self.assertEqual(usage_from_result(record).total_tokens, 138)
        self.assertNotIn("total_cost_usd", record)

    def test_messages_stream_final_result_is_authoritative(self):
        assistant = {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {
                "id": "msg-1",
                "content": [{"type": "text", "text": "Partial"}],
                "usage": GROK_USAGE,
            },
        }
        terminal = {
            "type": "result",
            "result": "Done.",
            "usage": GROK_USAGE,
            "total_cost_usd": 0.125,
        }
        record = parse_grok_result(stream(assistant, terminal))
        self.assertEqual(record["result"], "Done.")
        self.assertEqual(usage_from_result(record).total_tokens, 135)
        partial = parse_grok_result(stream(assistant, assistant))
        self.assertEqual(partial["result"], "Partial")
        self.assertEqual(usage_from_result(partial).total_tokens, 135)

    def test_error_spend_is_not_lost_to_empty_end(self):
        record = parse_grok_result(
            stream(
                {"type": "error", "usage": GROK_USAGE, "total_cost_usd": 0.125},
                {"type": "end", "stopReason": "cancelled"},
            )
        )
        self.assertEqual(usage_from_result(record).total_tokens, 135)
        self.assertEqual(usage_from_result(record).llm_cost_usd, 0.125)

    def test_unreported_and_incomplete_cost_are_not_inferred(self):
        for flag in (None, "cost_is_partial", "usage_is_incomplete"):
            with self.subTest(flag=flag):
                event = {**GROK_RESULT, "modelUsage": {"grok": {"costUSD": 99}}}
                if flag is None:
                    del event["total_cost_usd"]
                else:
                    event[flag] = True
                record = parse_grok_result(json.dumps(event))
                self.assertNotIn("total_cost_usd", record)
                self.assertEqual(usage_from_result(record).llm_cost_usd, 0)
                self.assertEqual(usage_from_result(record).total_tokens, 135)


class MalformedOutputTests(unittest.TestCase):

    def test_invalid_json_shapes_and_noise(self):
        noise = 'install log\nnull\n[]\n{\n{"type": []}\n{"type": {}}\n'
        for parser, payload in [
            (parse_codex_result, stream(*CODEX_EVENTS)),
            (parse_grok_result, json.dumps(GROK_RESULT)),
        ]:
            with self.subTest(parser=parser.__name__):
                self.assertIsNone(parser(noise))
                self.assertIsNone(parser("plain text"))
                self.assertEqual(parser(noise + payload)["result"], "Done.")
        self.assertEqual(
            parse_codex_result(json.dumps(CODEX_EVENTS))["result"], "Done."
        )

    def test_bad_numeric_fields_do_not_break_or_pollute_usage(self):
        for value in (
            None,
            True,
            "invalid",
            -1,
            float("nan"),
            float("inf"),
            -float("inf"),
            10**400,
        ):
            for parser, event in [
                (parse_codex_result, {"type": "turn.completed"}),
                (parse_grok_result, {"text": "Done."}),
            ]:
                with self.subTest(parser=parser.__name__, value=str(value)):
                    event.update(
                        total_cost_usd=value,
                        usage={
                            "input_tokens": value,
                            "output_tokens": value,
                            "cached_input_tokens": value,
                            "cache_read_input_tokens": value,
                            "cache_creation_input_tokens": value,
                        },
                    )
                    self.assertEqual(
                        usage_from_result(parser(json.dumps(event))), Usage()
                    )


class HarnessCommandTests(unittest.TestCase):

    def test_codex_fresh_and_resume_commands(self):
        sb, session = sandbox(stream(*CODEX_EVENTS), exit_code=1)
        with sb, patch.object(sb, "estimate_compute_cost", return_value=0.25):
            for resume in (False, True):
                result = sb.run_agent(
                    "--review 'quotes' $text",
                    harness="codex",
                    model="test-model",
                    resume=resume,
                    command="/opt/bin/codex",
                    extra_args=["--skip-git-repo-check"],
                )
                args = ["exec"] + (["resume", "--last"] if resume else [])
                session.execute_command.assert_called_with(
                    "/opt/bin/codex",
                    args
                    + [
                        "--json",
                        "--model",
                        "test-model",
                        "--skip-git-repo-check",
                        "--",
                        "--review 'quotes' $text",
                    ],
                )
                self.assertEqual(result.text, "Done.")
                self.assertEqual(result.stderr, "diagnostic")
                self.assertEqual(result.exit_code, 1)
            self.assertEqual(sb.usage.total_tokens, 260)
            self.assertEqual(sb.usage.compute_cost_usd, 0.5)

    def test_grok_command_model_resume_and_cost(self):
        sb, session = sandbox(json.dumps(GROK_RESULT), exit_code=1)
        with sb, patch.object(sb, "estimate_compute_cost", return_value=0.25):
            result = sb.run_agent(
                "--review 'quotes' $text",
                harness="grok",
                model="test-model",
                resume=True,
                command="/opt/bin/grok",
                extra_args=["--max-turns", "3"],
            )
            session.execute_command.assert_called_once_with(
                "/opt/bin/grok",
                [
                    "--no-auto-update",
                    "--output-format",
                    "json",
                    "-c",
                    "--model",
                    "test-model",
                    "--max-turns",
                    "3",
                    "--single=--review 'quotes' $text",
                ],
            )
            self.assertEqual(result.text, "Done.")
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.usage.total_cost_usd, 0.375)
            self.assertEqual(sb.usage, result.usage)

    def test_text_output_and_grok_stream_formats(self):
        for harness, formats in [
            ("codex", ["text"]),
            (
                "grok",
                ["text", "plain", "streaming-json", "streaming-messages-json"],
            ),
        ]:
            for fmt in formats:
                with self.subTest(harness=harness, fmt=fmt):
                    sb, session = sandbox("plain answer")
                    with sb:
                        result = sb.run_agent(
                            "hi", harness=harness, output_format=fmt
                        )
                    command, args = session.execute_command.call_args.args
                    self.assertEqual(command, harness)
                    if harness == "grok":
                        self.assertEqual(
                            args[2], "plain" if fmt == "text" else fmt
                        )
                    else:
                        self.assertEqual(args, ["exec", "--", "hi"])
                    self.assertEqual(result.text, "plain answer")
                    self.assertEqual(result.usage.total_tokens, 0)

    def test_invalid_formats_fail_before_execution(self):
        for harness, fmt in [("codex", "stream-json"), ("grok", "stream-json")]:
            sb, session = sandbox("")
            with sb, self.assertRaises(ValueError):
                sb.run_agent("hi", harness=harness, output_format=fmt)
            session.execute_command.assert_not_called()


class AsyncHarnessTests(unittest.IsolatedAsyncioTestCase):

    async def test_async_forwards_new_harnesses_and_resume(self):
        for harness, payload in [
            ("grok", json.dumps(GROK_RESULT)),
            ("codex", stream(*CODEX_EVENTS)),
        ]:
            sb, session = sandbox(payload)
            async with AsyncSandbox(sb) as async_sb:
                result = await async_sb.run_agent(
                    "hi",
                    harness=harness,
                    model="test-model",
                    resume=True,
                )
                self.assertEqual(async_sb.usage, result.usage)
            command, args = session.execute_command.call_args.args
            self.assertEqual(command, harness)
            self.assertIn("test-model", args)
            self.assertIn("--last" if harness == "codex" else "-c", args)
            self.assertEqual(result.text, "Done.")
