import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.errors import AppError


def uid() -> str:
    return str(uuid4())


def safe_json(value):
    if isinstance(value, Decimal):
        return format(value, ".2f")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): safe_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [safe_json(v) for v in value]
    return value


def canonical(value) -> str:
    return json.dumps(safe_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def jsonb(value) -> Jsonb:
    return Jsonb(safe_json(value))


def month_bounds(period: str) -> tuple[date, date]:
    try:
        start = date.fromisoformat(period + "-01")
        if len(period) != 7:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise AppError("INVALID_ARGUMENT", "月份须为 YYYY-MM，例如 2026-09。") from exc
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start, end


def is_blank(value: str | None) -> bool:
    return value is None or not value.strip()
