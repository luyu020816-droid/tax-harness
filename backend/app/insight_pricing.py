"""Explicit local price snapshots; no model aliases or network price lookups."""

import json
from datetime import date
from decimal import Decimal, InvalidOperation

from app.config import Settings


def validate_price(price):
    if not isinstance(price, dict) or not all(
        isinstance(price.get(k), str) and price[k] for k in ("model", "currency", "as_of", "source")
    ):
        raise ValueError("Price identity, currency, date and source are required")
    for key in (
        "input_cache_hit_per_million",
        "input_cache_miss_per_million",
        "output_per_million",
    ):
        if not isinstance(price[key], str):
            raise ValueError("Rates must be decimal strings")
        rate = Decimal(price[key])
        if not rate.is_finite() or rate < 0:
            raise ValueError("Invalid price")
    date.fromisoformat(price["as_of"])
    return price


def load_prices():
    path = Settings().insight_price_file
    if not path.is_file() or path.stat().st_size > 65536:
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return {p["model"]: validate_price(p) for p in rows}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, InvalidOperation):
        return {}
