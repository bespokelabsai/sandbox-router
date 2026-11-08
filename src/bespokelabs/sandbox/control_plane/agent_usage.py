"""Persistable agent usage and explicitly labeled Codex API-cost estimates."""

from __future__ import annotations

import math
from decimal import Decimal

from bespokelabs.sandbox.types import AgentRunResult

# Standard, short-context API rates, USD per million tokens. This is a dated
# baseline estimate, not a subscription charge or an invoice. Preserve the
# selected rates on each run so future price changes do not rewrite history.
PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICE_AS_OF = "2026-11-07"
CODEX_RATES = {
    "gpt-5.3-codex": ("1.75", "0.175", "14"),
    "gpt-6-astra": ("10", "1", "50"),
    "gpt-6.1-sol": ("2", "0.10", "10"),
    "gpt-6.1-terra": ("1", "0.05", "5"),
    "gpt-6-luna": ("0.10", "0.01", "0.50"),
}
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def empty_agent_summary() -> dict:
    """Return an additive summary that distinguishes unknown from zero."""
    return {
        **dict.fromkeys(TOKEN_FIELDS, 0),
        "total_tokens": 0,
        "runs": 0,
        "unknown_usage_runs": 0,
        "incomplete_usage_runs": 0,
        "reported_cost_usd": "0",
        "estimated_cost_usd": "0",
        "reported_cost_runs": 0,
        "estimated_cost_runs": 0,
        "unknown_cost_runs": 0,
    }


def add_agent_usage(summary: dict, usage: dict) -> None:
    """Accumulate one persisted run without combining reported and estimates."""
    summary["runs"] += 1
    for field in (*TOKEN_FIELDS, "total_tokens"):
        summary[field] += usage[field]
    summary["unknown_usage_runs"] += not usage["tokens_known"]
    summary["incomplete_usage_runs"] += usage["usage_is_incomplete"]
    state = usage["cost_state"]
    if state in {"reported", "estimated"}:
        field = f"{state}_cost_usd"
        summary[field] = str(
            Decimal(summary[field]) + Decimal(usage["cost_usd"])
        )
        summary[f"{state}_cost_runs"] += 1
    else:
        summary["unknown_cost_runs"] += 1


def _valid_number(value: object) -> bool:
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )
    except (OverflowError, ValueError):
        return False


def _token_block(value: object, *, opencode: bool = False) -> bool:
    fields = (
        ("input", "output") if opencode else ("input_tokens", "output_tokens")
    )
    return isinstance(value, dict) and all(
        _valid_number(value.get(k)) for k in fields
    )


def _has_tokens(record: dict, harness: str) -> bool:
    events = record.get("events")
    if not isinstance(events, list):
        events = [record]
    for event in events:
        if not isinstance(event, dict):
            continue
        if harness == "opencode":
            part = event.get("part")
            if event.get("type") == "step_finish" and isinstance(part, dict):
                tokens = part.get("tokens")
                if _token_block(tokens, opencode=True):
                    return True
        elif _token_block(event.get("usage")):
            return True
        elif isinstance(event.get("message"), dict):
            if _token_block(event["message"].get("usage")):
                return True
    return False


def agent_usage_record(
    result: AgentRunResult,
    *,
    harness: str,
    model: str | None,
    extra_args: list[str] | None = None,
) -> dict:
    """Describe reported spend, or estimate Codex from an explicit priced model.

    Never guess the CLI's default model, price an unknown alias, or present a
    partial/unreported cost as a reported zero. The legacy SDK Usage remains
    unchanged; the dashboard has a nullable cost plus provenance.
    """
    raw = result.raw or {}
    events = raw.get("events", [])
    events = (
        [e for e in events if isinstance(e, dict)]
        if isinstance(events, list)
        else []
    )
    sources = [raw, *(e for e in events if isinstance(e, dict))]
    incomplete = any(s.get("usage_is_incomplete") for s in sources)
    if harness == "opencode":
        incomplete = (
            incomplete
            or result.exit_code != 0
            or any(e.get("type") == "error" for e in events)
        )
    partial_cost = any(s.get("cost_is_partial") for s in sources)
    known = _has_tokens(raw, harness)
    if events and known:
        if harness == "codex":
            boundaries = [
                e.get("type")
                for e in events
                if e.get("type")
                in {"turn.started", "turn.completed", "turn.failed"}
            ]
            incomplete = (
                incomplete
                or not boundaries
                or boundaries[-1] != "turn.completed"
            )
        elif harness == "grok":
            incomplete = incomplete or not any(
                e.get("type") in {"end", "result", "error"} or "text" in e
                for e in events
            )
    record = {
        "harness": harness,
        "model": model,
        **{name: getattr(result.usage, name) for name in TOKEN_FIELDS},
        "total_tokens": result.usage.total_tokens,
        "tokens_known": known,
        "usage_is_incomplete": incomplete,
        "cost_usd": None,
        "cost_state": "unavailable",
        "cost_reason": "The CLI did not report a complete dollar cost.",
        "pricing_source": None,
        "pricing_as_of": None,
        "rates_per_million": None,
    }
    cost = raw.get("total_cost_usd")
    # OpenCode's normalizer emits zero even with no step events. It is only
    # a reported price if each completed step actually supplied a cost.
    reported = _valid_number(cost)
    if events and harness in {"grok", "codex"}:
        costs = [e["total_cost_usd"] for e in events if "total_cost_usd" in e]
        reported = (
            reported and bool(costs) and all(_valid_number(c) for c in costs)
        )
    if harness == "opencode":
        steps = [
            e.get("part") for e in events if e.get("type") == "step_finish"
        ]
        reported = bool(steps) and all(
            isinstance(p, dict) and _valid_number(p.get("cost")) for p in steps
        )
    if reported and not incomplete and not partial_cost:
        record.update(
            cost_usd=str(cost), cost_state="reported", cost_reason=None
        )
        return record
    if harness != "codex":
        return record
    overrides = (
        "--model",
        "-m",
        "--config",
        "-c",
        "--profile",
        "-p",
        "--oss",
        "--local-provider",
    )
    if any(
        arg.split("=", 1)[0] in overrides
        or (arg.startswith(("-m", "-c", "-p")) and not arg.startswith("--"))
        for arg in extra_args or []
    ):
        record["cost_reason"] = (
            "CLI model/provider overrides prevent a reliable estimate."
        )
        return record
    if model not in CODEX_RATES:
        record["cost_reason"] = (
            "Pass an explicit model with a published rate to estimate Codex cost."
        )
        return record
    if not known or incomplete or result.usage.cache_creation_tokens:
        record["cost_reason"] = (
            "Complete, priceable token usage was not reported."
        )
        return record
    input_rate, cache_rate, output_rate = map(Decimal, CODEX_RATES[model])
    estimate = (
        result.usage.input_tokens * input_rate
        + result.usage.cache_read_tokens * cache_rate
        + result.usage.output_tokens * output_rate
    ) / Decimal(1_000_000)
    record.update(
        cost_usd=str(estimate),
        cost_state="estimated",
        cost_reason="Standard short-context API-rate estimate; excludes fast/long-context premiums, tools, regional charges, and subscription billing.",
        pricing_source=PRICE_SOURCE,
        pricing_as_of=PRICE_AS_OF,
        rates_per_million={
            "input": str(input_rate),
            "cached_input": str(cache_rate),
            "output": str(output_rate),
        },
    )
    return record
