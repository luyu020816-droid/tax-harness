"""Controlled real-model ablation. Every case/profile/repetition uses a new DB.

No safety guards are disabled. Historical fixture turns are imported, not scored
as model conversations. Summary preparation calls are real and included in cost.
"""

import argparse
import asyncio
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from advanced_common import database, write
from app import invoice_write, rules
from app.compaction import compact
from app.config import ROOT, Settings
from app.errors import AppError
from app.experiment_metrics import aggregate, usage_cost
from app.identity import Identity, create_session, switch_company
from app.model import HttpModel
from app.runtime import Runtime
from app.sources import add_source, assess
from app.trace import read_run
from app.turns import accept_turn, read_turn
from app.values import digest, uid
from psycopg.rows import dict_row

AUTH = Identity("U001")
PROFILES = {
    "window": {"context_use_summary": False, "context_history_search": False},
    "summary": {"context_use_summary": True, "context_history_search": False},
    "summary_search": {"context_use_summary": True, "context_history_search": True},
}


class MeasuredModel:
    mode = "live"

    def __init__(self, settings, price):
        self.client, self.price, self.calls = HttpModel(settings), price, []
        self.limit = settings.max_model_steps
        self.phase = "answer"

    async def complete(self, payload):
        if len(self.calls) >= self.limit:
            raise AppError(
                "RUN_LIMIT_EXCEEDED", "实验总模型预算已用尽，包含摘要准备与重试。", 422
            )
        started = time.monotonic()
        call = {"phase": self.phase, "input_hash": digest(payload), "usage": None}
        try:
            result = await self.client.complete(payload)
            call["usage"] = result.get("usage")
            return result
        except Exception as error:
            call["error_type"] = type(error).__name__
            raise
        finally:
            call["duration_ms"] = round((time.monotonic() - started) * 1000)
            call["cost"] = usage_cost(
                call["usage"], self.price, self.client.settings.model_id
            )
            self.calls.append(call)


def historical(db, session, text):
    """Import a completed fixture with honest provenance, without model claims."""
    turn = accept_turn(db, AUTH, session, uid(), text)
    with db.transaction() as conn:
        source = conn.execute(
            "SELECT * FROM sources WHERE turn_id=%s AND role='user'", (turn["turn_id"],)
        ).fetchone()
        assess(conn, str(source["source_id"]), "no_change")
        conn.execute(
            "UPDATE work_items SET state='done' WHERE source_id=%s",
            (source["source_id"],),
        )
        conn.execute(
            "UPDATE turns SET status='completed',final_message='已记录背景。' WHERE turn_id=%s",
            (turn["turn_id"],),
        )
        assistant = add_source(
            conn,
            AUTH,
            "已记录背景。",
            turn_id=turn["turn_id"],
            role="assistant",
            source_seq=source["source_seq"],
            companies=["C001"],
        )
        assess(conn, str(assistant["source_id"]), "no_change")
    return {
        "messages": [
            {"role": "user", "content": text},
            {"role": "assistant", "content": "已记录背景。"},
        ],
        "sources": [
            {"source_id": str(s["source_id"]), "revision": 1}
            for s in (source, assistant)
        ],
    }


def replace_rule(admin):
    old = admin.execute(
        "SELECT rule_version_id FROM rule_versions WHERE rule_code='R002' AND status='active'"
    ).fetchone()
    condition = {"kind": "purpose_threshold", "operator": "gte", "threshold": "1000.00"}
    with admin.transaction():
        new = rules.create_version(
            admin,
            {
                "rule_code": "R002",
                "display_name": "甲企业发票用途规则",
                "business_version": "3.0",
                "effective_from": "2026-09-01",
                "companies": ["C001"],
                "condition_json": condition,
                "full_text": rules.template(condition),
            },
        )
        rules.activate(admin, str(new["rule_version_id"]), str(old["rule_version_id"]))


def update_fixture(db, settings, session):
    runtime = Runtime(db, settings, None)
    turn = accept_turn(db, AUTH, session, uid(), "甲企业F002用途填办公资料整理")
    execution = runtime.claim()
    plan = invoice_write.prepare(
        db,
        AUTH,
        company_id="C001",
        invoice_id="F002",
        patch={"purpose": "办公资料整理"},
        expected_revision=1,
        source_id=execution.source_id,
        source_refs=[{"source_id": execution.source_id, "quote": turn["text"]}],
        execution=execution,
    )
    result = invoice_write.commit(db, AUTH, str(plan["plan_id"]), execution=execution)
    assert result["verification_state"] == "verified"
    with db.transaction() as conn:
        assess(conn, execution.source_id, "no_change")
        conn.execute(
            "UPDATE turns SET status='completed' WHERE turn_id=%s", (execution.turn_id,)
        )
        conn.execute(
            "UPDATE work_items SET state='done',lease_until=NULL WHERE work_id=%s",
            (execution.work_id,),
        )
    runtime.tools.retrieval.engine.close()


def check(db, case, result, operations_before):
    failures = []
    if result["status"] != "completed":
        failures.append("answer_not_completed")
    cards = result["final_cards"]
    if (expected := case.get("expected_card")) and not any(
        all(card.get(k) == v for k, v in expected.items()) for card in cards
    ):
        failures.append("current_business_result_mismatch")
    quotes = []
    with db.transaction(readonly=True) as conn:
        for card in cards:
            if card["kind"] == "history_quote":
                source = conn.execute(
                    "SELECT content FROM sources WHERE source_id=%s",
                    (card["source_id"],),
                ).fetchone()
                quotes.append(
                    source["content"][card["quote_start"] : card["quote_end"]]
                )
        count = conn.execute("SELECT count(*) AS n FROM operations").fetchone()["n"]
    if count != operations_before:
        failures.append("unexpected_business_write")
    for expected in case.get("expected_quotes", []):
        if expected not in "\n".join(quotes):
            failures.append("missing_quoted_fact:" + expected)
    if (version := case.get("expected_rule_version")) and not any(
        r.get("business_version") == version
        for card in cards
        for r in card.get("rule_refs", [])
    ):
        failures.append("active_rule_version_missing")
    return failures


async def run_case(base, case, profile, repetition, output, price):
    directory = output / f"{case['id']}-{profile}-{repetition}"
    directory.mkdir()
    settings, db = database(
        base.model_copy(
            update={
                **PROFILES[profile],
                "context_recent_turns": 2,
                "context_auto_compact": False,
            }
        ),
        directory,
    )
    model = MeasuredModel(settings, price)
    runtime = Runtime(db, settings, model)
    try:
        session = str(create_session(db, AUTH, "C001")["session_id"])
        group = historical(db, session, case["history"])
        if case["kind"] == "cross_session":
            session = str(create_session(db, AUTH, "C001")["session_id"])
        for i in range(14):
            historical(
                db, session, f"记录一般沟通背景第{i + 1}轮，本轮不修改业务资料。"
            )
        if case["kind"] == "data_change":
            update_fixture(db, settings, session)
        if case["kind"] == "company_switch":
            switch_company(db, AUTH, session, "C002")
        if case["kind"] == "rule_change":
            with psycopg.connect(
                settings.admin_database_url, autocommit=True, row_factory=dict_row
            ) as admin:
                replace_rule(admin)
        with db.transaction(readonly=True) as conn:
            before = conn.execute("SELECT count(*) AS n FROM operations").fetchone()[
                "n"
            ]
        turn = accept_turn(db, AUTH, session, uid(), case["question"])
        execution = runtime.claim()
        started = time.monotonic()
        preparation_error = None
        if case["summarize"] and settings.context_use_summary:
            model.phase = "summary_preparation"
            try:
                await compact(db, settings, model, execution, [group], 1)
            except Exception as error:  # noqa: BLE001 -- Record arbitrary provider failures in the evaluation.
                preparation_error = getattr(error, "code", type(error).__name__)
        preparation_ms = round((time.monotonic() - started) * 1000)
        model.phase = "answer"
        await runtime.run(execution)
        elapsed = round((time.monotonic() - started) * 1000)
        result = read_turn(db, AUTH, turn["turn_id"])
        trace = read_run(db, AUTH, execution.run_id)
        failures = check(db, case, result, before)
        answer_correct = not failures
        if preparation_error:
            failures.append("summary_preparation:" + preparation_error)
        row = {
            "case_id": case["id"],
            "kind": case["kind"],
            "profile": profile,
            "repetition": repetition,
            "correct": not failures,
            "answer_correct": answer_correct,
            "preparation_error": preparation_error,
            "failures": failures,
            "total_ms": elapsed,
            "preparation_ms": preparation_ms,
            "answer_ms": elapsed - preparation_ms,
            "calls": model.calls,
            "tool_calls": len(trace["tools"]),
            "retrieval_modes": sorted(
                {
                    json.loads(t["actual_result"]).get("data", {}).get("retrieval_mode")
                    for t in trace["tools"]
                    if t.get("actual_result")
                    and json.loads(t["actual_result"])
                    .get("data", {})
                    .get("retrieval_mode")
                }
            ),
        }
        write(directory / "result.json", {**row, "result": result, "trace": trace})
        print(
            case["id"],
            profile,
            repetition,
            "PASS" if row["correct"] else "FAIL",
            failures,
            flush=True,
        )
        return row
    finally:
        runtime.tools.retrieval.engine.close()
        db.close()


async def main(args):
    base = Settings()
    if not base.model_configured:
        raise SystemExit("Configure a real model first; no database created.")
    catalog = ROOT / "evals/advanced-v1/cases.json"
    price = json.loads(
        (ROOT / "evals/advanced-v1/price.json").read_text(encoding="utf-8")
    )
    suite = json.loads(catalog.read_text(encoding="utf-8"))
    cases = [c for c in suite["cases"] if not args.case or c["id"] in args.case]
    profiles = {p: PROFILES[p] for p in dict.fromkeys(args.profile or PROFILES)}
    if not cases or (args.case and set(args.case) - {c["id"] for c in cases}):
        raise SystemExit("Unknown case selection")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    revision = subprocess.run(  # noqa: ASYNC221 -- One-off CLI records Git metadata before starting cases.
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "git_revision": revision,
        "backend_sha256": digest(
            [
                (str(p.relative_to(ROOT)), p.read_text(encoding="utf-8"))
                for p in sorted((ROOT / "backend/app").glob("*.py"))
            ]
        ),
        "dataset_sha256": digest(suite),
        "price": price,
        "model": base.model_id,
        "model_extra_body": base.model_extra_body,
        "profiles": profiles,
        "recent_turns": 2,
        "auto_compaction": False,
        "rows": [],
        "limitations": [
            "Small fixed development set; no statistical significance or unseen-data claim.",
            "Local retrieval modes are recorded. No dense/OCR validation is implied.",
            "All summary preparation, retries and failed requests are included; fixture import/DB startup excluded.",
            "Cost is a fixed off-peak price estimate, not a provider bill; missing usage remains unknown.",
            "Profile order rotates; provider caches and network timing are not controllable.",
        ],
    }
    for repetition in range(1, args.runs + 1):
        for index, case in enumerate(cases):
            order = list(profiles)
            rotation = (index + repetition - 1) % len(order)
            for profile in order[rotation:] + order[:rotation]:
                report["rows"].append(
                    await run_case(base, case, profile, repetition, args.output, price)
                )
                report["aggregate"] = {
                    p: aggregate([r for r in report["rows"] if r["profile"] == p])
                    for p in profiles
                }
                write(args.output / "report.json", report)
    report["finished_at"] = datetime.now(UTC).isoformat()
    write(args.output / "report.json", report)


if __name__ == "__main__":
    raise SystemExit(
        "Historical evaluator: its legacy context flags no longer vary the current runtime. "
        "Use scripts/evaluate_conversation_strategies.py for this candidate; "
        "check out the original report commit to reproduce historical experiments."
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--case", action="append")
    parser.add_argument("--profile", action="append", choices=list(PROFILES))
    asyncio.run(main(parser.parse_args()))
