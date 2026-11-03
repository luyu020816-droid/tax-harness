"""Pure, explicit measurement semantics shared by API and tests."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.errors import AppError

TERMINAL = ("completed", "waiting_user", "failed", "cancelled", "interrupted")


def window(start=None, end=None):
    now = datetime.now(UTC)
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    default_end = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        a = datetime.fromisoformat(start) if start else default_end - timedelta(days=7)
        b = datetime.fromisoformat(end) if end else default_end
        if not a.tzinfo or not b.tzinfo or not timedelta(0) < b - a <= timedelta(days=31):
            raise ValueError
        return a, b
    except (ValueError, TypeError) as exc:
        raise AppError("INVALID_ARGUMENT", "时间必须带时区，范围须大于零且最多31天。", 422) from exc


def percentile(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1] if values else None


def union_ms(intervals):
    if any(a is None or b is None or b < a for a, b in intervals):
        return None
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a, b])
    return sum((b - a).total_seconds() * 1000 for a, b in merged)


def usage_valid(usage):
    return isinstance(usage, dict) and all(
        type(usage.get(k)) is int and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens")
    )


def costs(attempts, prices):
    from app.experiment_metrics import usage_cost

    known, unknown, currencies, versions = Decimal(0), 0, set(), set()
    upper = 0
    for attempt in attempts:
        if attempt.get("dispatched") is False:
            continue
        price = (
            attempt.get("price_snapshot")
            if attempt.get("price_captured")
            else prices.get(attempt.get("model"))
        )
        if attempt.get("dispatched") is None or not price:
            unknown += 1
            continue
        result = usage_cost(attempt.get("usage"), price, attempt["model"])
        if result["usd"] is None:
            unknown += 1
        else:
            known += Decimal(result["usd"])
            currencies.add(price["currency"])
            versions.add(price["as_of"])
            upper += result["kind"] == "all_input_miss_upper_estimate"
    return {
        "known_cost": str(known) if len(currencies) <= 1 else None,
        "complete_cost": str(known) if not unknown and len(currencies) <= 1 else None,
        "unknown_calls": unknown,
        "currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "pricing_versions": sorted(versions),
        "upper_estimate_calls": upper,
    }
