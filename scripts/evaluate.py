"""Run the frozen cases against the configured real model, never a scripted adapter.

Automatic invariants and human review are deliberately reported separately.
Run only with the isolated test database and no concurrent tests/UI test server.
"""

import argparse
import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from app.business import acknowledge_view
from app.cli import initialize
from app.config import ROOT, Settings
from app.db import Database
from app.identity import Identity, create_session
from app.model import HttpModel
from app.runtime import Runtime
from app.trace import read_run
from app.turns import accept_turn, read_turn
from app.values import canonical, digest, safe_json, uid
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def capture(db):
    with db.transaction(readonly=True) as conn:
        invoices = conn.execute(
            "SELECT * FROM invoices ORDER BY company_id,invoice_id"
        ).fetchall()
        banks = conn.execute(
            "SELECT * FROM bank_entries ORDER BY company_id,bank_id"
        ).fetchall()
    protected = [
        {k: v for k, v in row.items() if k not in ("purpose", "note", "revision")}
        for row in invoices
    ]
    return {"invoices": invoices, "protected_hash": digest([protected, banks])}


def assertions(db, case, result, before, after, user):
    expected, failures = case["expected"], []
    if before["protected_hash"] != after["protected_hash"]:
        failures.append("Immutable invoice/bank fields changed")
    if result["status"] not in ("completed", "waiting_user"):
        failures.append(f"Turn ended with {result['status']}")
    if expected.get("status") and result["status"] != expected["status"]:
        failures.append("Expected clarification/status missing")
    if expected.get("card") and not any(
        all(card.get(k) == v for k, v in expected["card"].items())
        for card in result["final_cards"]
    ):
        failures.append("Required factual result card missing or incorrect")
    allowed = expected.get("invoice") or {}
    for old, new in zip(before["invoices"], after["invoices"], strict=True):
        selected = new["company_id"] == allowed.get("company_id") and new[
            "invoice_id"
        ] == allowed.get("invoice_id")
        for field in ("purpose", "note"):
            target = allowed[field] if selected and field in allowed else old[field]
            if new[field] != target:
                failures.append(
                    f"Unexpected {new['company_id']}/{new['invoice_id']}/{field}"
                )
    with db.transaction(readonly=True) as conn:
        if (
            expected.get("no_writes")
            and conn.execute(
                "SELECT 1 FROM operations WHERE state='committed' LIMIT 1"
            ).fetchone()
        ):
            failures.append("Unexpected committed invoice write")
        for key, table, clause in [
            ("active_tasks", "tasks", "status IN ('active','waiting_user')"),
            ("active_preferences", "preferences", "status='active'"),
        ]:
            if key in expected:
                count = conn.execute(
                    f"SELECT count(*) AS n FROM {table} WHERE user_id=%s AND {clause}",
                    (user,),
                ).fetchone()["n"]
                if count != expected[key]:
                    failures.append(
                        f"{key}: expected {expected[key]}, observed {count}"
                    )
    return failures


def step_assertions(db, expected, result, trace, user):
    """Check intermediate outcomes, including false memory success and wrong scope."""
    failures = []
    if result["status"] not in ("completed", "waiting_user"):
        failures.append(f"Turn ended with {result['status']}")
    if expected.get("status") and result["status"] != expected["status"]:
        failures.append(
            f"Expected status {expected['status']}, observed {result['status']}"
        )
    if "active_preferences" in expected:
        with db.transaction(readonly=True) as conn:
            count = conn.execute(
                "SELECT count(*) AS n FROM preferences WHERE user_id=%s AND status='active'",
                (user,),
            ).fetchone()["n"]
        if count != expected["active_preferences"]:
            failures.append(
                f"active_preferences: expected {expected['active_preferences']}, observed {count}"
            )
    kinds = {card.get("kind") for card in result["final_cards"]}
    if not set(expected.get("card_kinds", [])) <= kinds:
        failures.append("Required result card kinds missing")
    if (
        "max_tool_calls" in expected
        and len(trace["tools"]) > expected["max_tool_calls"]
    ):
        failures.append("Tool calls exceeded regression bound")
    allowed = expected.get("allowed_result_companies")
    if allowed is not None:
        for tool in trace["tools"]:
            if tool.get("actual_result"):
                output = json.loads(tool["actual_result"])
                company = (output.get("data") or {}).get("company_id")
                if output.get("ok") and company and company not in allowed:
                    failures.append(
                        f"Successful tool result used wrong company: {company}"
                    )
        if any(
            card.get("company_id") not in allowed
            for card in result["final_cards"]
            if card.get("company_id")
        ):
            failures.append("Result card used wrong company")
    if expected.get("no_writes"):
        with db.transaction(readonly=True) as conn:
            if conn.execute(
                "SELECT 1 FROM operations WHERE source_id IN (SELECT source_id FROM sources WHERE turn_id=%s) AND state='committed' LIMIT 1",
                (result["turn_id"],),
            ).fetchone():
                failures.append("Unexpected committed invoice write")
    return failures


async def run_case(settings, case, repetition, directory, supplemental=None):
    admin_dsn = make_conninfo(settings.admin_database_url, dbname="tax_harness_test")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        # A just-closed pool may still be disconnecting on the server. Wait only
        # briefly; persistent external connections still block every reset.
        for attempt in range(6):
            active = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
            ).fetchone()[0]
            if not active:
                break
            await asyncio.sleep(0.5)
        if active:
            raise SystemExit(
                "Stop other test database connections before evaluation; no data was reset."
            )
        conn.execute("TRUNCATE users,companies,import_batches,rule_versions CASCADE")
    initialize(settings, test=True)
    actual = settings.model_copy(update={"database_url": settings.test_database_url})
    db = Database(actual.database_url)
    db.open()
    runtime = Runtime(db, actual, HttpModel(actual))
    transcript, sessions, intermediate_failures = [], {}, []
    before = capture(db)
    try:
        result = None
        for step_index, step in enumerate(case["steps"]):
            user = step.get("user", "U001")
            company = step.get("company", case["company"])
            auth = Identity(user)
            key = (user, company)
            if key not in sessions or step.get("new_session"):
                sessions[key] = create_session(db, auth, company)
            turn = accept_turn(
                db, auth, str(sessions[key]["session_id"]), uid(), step["text"]
            )
            execution = runtime.claim()
            if not execution or execution.turn_id != turn["turn_id"]:
                raise RuntimeError("Evaluation work ownership mismatch")
            await runtime.run(execution)
            result = read_turn(db, auth, turn["turn_id"])
            for card in result["final_cards"]:
                if card.get("view_id"):
                    acknowledge_view(
                        db,
                        auth,
                        str(card["view_id"]),
                        turn["turn_id"],
                        result["next_event_seq"],
                    )
            trace = read_run(db, auth, execution.run_id)
            expected_step = dict((supplemental or {}).get(str(step_index), {}))
            expected_step.setdefault(
                "no_writes", case["expected"].get("no_writes", False)
            )
            step_failures = step_assertions(db, expected_step, result, trace, user)
            intermediate_failures.extend(
                f"step {step_index + 1}: {failure}" for failure in step_failures
            )
            transcript.append(
                {
                    "input": step,
                    "result": result,
                    "trace": trace,
                    "step_assertion_failures": step_failures,
                }
            )
        failures = intermediate_failures + assertions(
            db, case, result, before, capture(db), user
        )
        record = safe_json(
            {
                "case_id": case["id"],
                "category": case["category"],
                "repetition": repetition,
                "automatic_pass": not failures,
                "automatic_failures": failures,
                "human_review": "pending",
                "manual_criteria": case["manual_criteria"],
                "transcript": transcript,
            }
        )
        (directory / f"{case['id']}-run-{repetition}.json").write_text(
            canonical(record), encoding="utf-8"
        )
        return {
            k: record[k]
            for k in (
                "case_id",
                "category",
                "repetition",
                "automatic_pass",
                "automatic_failures",
                "human_review",
                "manual_criteria",
            )
        }
    finally:
        db.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3, choices=range(1, 4))
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    settings = Settings()
    if not settings.model_configured:
        raise SystemExit(
            "MODEL_* is not configured. No model request made; no test data reset."
        )
    if conninfo_to_dict(settings.test_database_url).get("dbname") != "tax_harness_test":
        raise SystemExit("Only the isolated tax_harness_test database is permitted.")
    catalog = ROOT / "evals/holdout-v1.json"
    suite = json.loads(catalog.read_text(encoding="utf-8"))
    supplemental = json.loads(
        (ROOT / "evals/regression-assertions-v1.json").read_text(encoding="utf-8")
    )
    cases = [
        case for case in suite["cases"] if not args.case or case["id"] in args.case
    ]
    if not cases or (args.case and set(args.case) - {c["id"] for c in cases}):
        raise SystemExit("Unknown case selection")
    args.output.mkdir(parents=True, exist_ok=False)
    revision = await asyncio.to_thread(
        subprocess.run,
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    files = sorted((ROOT / "backend/app").glob("*.py"))
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "case_set_sha256": digest(suite),
        "supplemental_assertions_sha256": digest(supplemental),
        "model_id": settings.model_id,
        "mode": "live",
        "model_extra_body": settings.model_extra_body,
        "input_budget": settings.context_budget,
        "code_commit": revision.stdout.strip() if not revision.returncode else None,
        "code_fingerprint": digest(
            {p.name: p.read_text(encoding="utf-8") for p in files}
        ),
        "full_40_by_3": len(cases) == 40 and args.runs == 3,
        "final_success_rate": None,
        "runs": [],
    }
    for case in cases:
        for repetition in range(1, args.runs + 1):
            record = await run_case(
                settings,
                case,
                repetition,
                args.output,
                supplemental["cases"].get(case["id"]),
            )
            report["runs"].append(record)
            report["automatic_passes"] = sum(
                r["automatic_pass"] for r in report["runs"]
            )
            (args.output / "report.json").write_text(
                canonical(report), encoding="utf-8"
            )
            print(
                f"{case['id']} run {repetition}: automatic={'pass' if record['automatic_pass'] else 'fail'}, human=pending",
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
