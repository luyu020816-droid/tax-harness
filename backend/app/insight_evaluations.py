"""Import bounded, trusted offline reports without executing report content."""

import math

from app.errors import AppError
from app.experiment_metrics import aggregate, usage_cost
from app.identity import assert_access
from app.insight_access import redact
from app.insight_pricing import validate_price
from app.values import canonical, digest, jsonb, safe_json, uid


def normalize(report):
    try:
        if len(canonical(report).encode("utf-8")) > 10 * 1024 * 1024:
            raise ValueError
        price = validate_price(report["price"])
        rows = []
        for row in report["rows"]:
            if (
                type(row["correct"]) is not bool
                or type(row["total_ms"]) not in (float, int)
                or not math.isfinite(row["total_ms"])
                or row["total_ms"] < 0
            ):
                raise ValueError
            if type(row["tool_calls"]) is not int or row["tool_calls"] < 0:
                raise ValueError
            calls = []
            for call in row["calls"]:
                usage = call.get("usage")
                if isinstance(usage, dict):
                    usage = {
                        k: v
                        for k, v in usage.items()
                        if k
                        in (
                            "prompt_tokens",
                            "completion_tokens",
                            "prompt_cache_hit_tokens",
                            "prompt_cache_miss_tokens",
                            "total_tokens",
                        )
                    }
                calls.append({"usage": usage, "cost": usage_cost(usage, price, report["model"])})
            # Preserve only fixed report vocabulary, never prompts, raw outputs or trace paths.
            rows.append(
                {
                    k: row.get(k)
                    for k in (
                        "case_id",
                        "profile",
                        "repetition",
                        "correct",
                        "answer_correct",
                        "preparation_error",
                        "total_ms",
                        "tool_calls",
                    )
                }
                | {"calls": calls, "answer_correct": row.get("answer_correct", row["correct"])}
            )
        if not rows or len(rows) > 20000:
            raise ValueError
        profiles = sorted({str(r["profile"]) for r in rows})
        return redact(
            {
                "schema_version": 1,
                "model": report["model"],
                "price": price,
                "backend_sha256": report.get("backend_sha256"),
                "dataset_sha256": report.get("dataset_sha256"),
                "sample_type": "development",
                "human_review": "pending",
                "limitations": ["固定开发集；非线上正确率，不证明未见泛化；价格快照估算并非账单。"],
                "rows": rows,
                "aggregate": {
                    p: aggregate([r for r in rows if r["profile"] == p]) for p in profiles
                },
            }
        )
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise AppError("INVALID_REPORT", "报告格式、价格或计量不符合规范，未导入。", 422) from exc


def import_report(conn, owner, companies, report):
    from app.identity import Identity

    if not conn.execute("SELECT 1 FROM users WHERE user_id=%s", (owner,)).fetchone():
        raise AppError("INVALID_REPORT", "报告归属用户不存在。", 422)
    for company in companies:
        assert_access(conn, Identity(owner), company)
    normalized = normalize(report)
    row = conn.execute(
        "INSERT INTO insight_evaluations(evaluation_id,owner_id,company_ids,report_hash,title,report) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(owner_id,report_hash) DO UPDATE SET report_hash=excluded.report_hash RETURNING evaluation_id",
        (
            uid(),
            owner,
            jsonb(sorted(set(companies))),
            digest(normalized),
            "记忆与输入策略对比",
            jsonb(normalized),
        ),
    ).fetchone()
    return str(row["evaluation_id"])


def read(db, auth, evaluation_id=None):
    with db.transaction(readonly=True) as conn:
        rows = conn.execute(
            "SELECT evaluation_id,title,company_ids,imported_at,report FROM insight_evaluations e WHERE owner_id=%s AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(e.company_ids) c WHERE NOT EXISTS(SELECT 1 FROM user_company_grants g WHERE g.user_id=e.owner_id AND g.company_id=c AND g.can_read)) ORDER BY imported_at DESC",
            (auth.user_id,),
        ).fetchall()
        if evaluation_id:
            for row in rows:
                if str(row["evaluation_id"]) == evaluation_id:
                    return safe_json(row)
            raise AppError("RESOURCE_NOT_FOUND", "评测记录不可访问。", 404)
        return safe_json([{k: v for k, v in r.items() if k != "report"} for r in rows])
