"""Metrics for controlled experiments; provider usage is never fabricated."""

import math
from decimal import Decimal


def usage_cost(usage, price, model):
    if not usage or model != price["model"]:
        return {"usd": None, "reason": "usage_missing_or_model_not_priced"}
    try:
        prompt, output = usage["prompt_tokens"], usage["completion_tokens"]
        if any(type(n) is not int or n < 0 for n in (prompt, output)):
            raise ValueError
        hit = usage.get("prompt_cache_hit_tokens")
        miss = usage.get("prompt_cache_miss_tokens")
        cache_known = hit is not None or miss is not None
        if cache_known:
            hit = prompt - miss if hit is None else hit
            miss = prompt - hit if miss is None else miss
            if any(type(n) is not int or n < 0 for n in (hit, miss)) or hit + miss != prompt:
                raise ValueError
        else:
            hit, miss = 0, prompt
        value = (
            hit * Decimal(price["input_cache_hit_per_million"])
            + miss * Decimal(price["input_cache_miss_per_million"])
            + output * Decimal(price["output_per_million"])
        ) / Decimal(1_000_000)
        return {
            "usd": str(value),
            "kind": "price_snapshot_estimate" if cache_known else "all_input_miss_upper_estimate",
            "prompt_tokens": prompt,
            "completion_tokens": output,
            "cache_hit_tokens": hit if cache_known else None,
        }
    except (KeyError, TypeError, ValueError):
        return {"usd": None, "reason": "invalid_provider_usage"}


def aggregate(rows):
    """Include failed cases and all requests, including preparation/repair calls."""
    calls = [call for row in rows for call in row["calls"]]
    costs = [c["cost"]["usd"] for c in calls]
    known = sum((Decimal(c) for c in costs if c is not None), Decimal(0))
    unknown = sum(c is None for c in costs)
    elapsed = sorted(row["total_ms"] for row in rows)
    return {
        "cases": len(rows),
        "correct": sum(row["correct"] for row in rows),
        "answer_correct": sum(row.get("answer_correct", row["correct"]) for row in rows),
        "summary_preparation_failures": sum(bool(row.get("preparation_error")) for row in rows),
        "accuracy": sum(row["correct"] for row in rows) / len(rows) if rows else None,
        "model_attempts": len(calls),
        "missing_usage_calls": unknown,
        "known_cost_usd": str(known),
        "estimated_cost_usd": str(known) if not unknown else None,
        "prompt_tokens_observed": sum(
            (c.get("usage") or {}).get("prompt_tokens", 0) for c in calls
        ),
        "completion_tokens_observed": sum(
            (c.get("usage") or {}).get("completion_tokens", 0) for c in calls
        ),
        "tool_calls": sum(row["tool_calls"] for row in rows),
        "total_ms": sum(elapsed),
        "p50_ms": elapsed[max(0, math.ceil(len(elapsed) * 0.5) - 1)] if elapsed else None,
        "p95_ms": elapsed[max(0, math.ceil(len(elapsed) * 0.95) - 1)] if elapsed else None,
    }
