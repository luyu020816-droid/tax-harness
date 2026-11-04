"""Same-candidate, read-only controlled ablation; never a historical binary replay.

Only the allowlisted test DB is reset, serially. Uses real Runtime/HttpModel and
business guards. The fixed-window strategy is an intentional lossy control.
"""

import argparse
import asyncio
import json
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import psycopg
from app import context
from app.cli import initialize
from app.config import ROOT, Settings
from app.db import Database
from app.errors import AppError
from app.experiment_metrics import aggregate
from app.identity import create_session, switch_company
from app.runtime import Runtime
from app.telemetry import code_fingerprint
from app.trace import read_run
from app.turns import accept_turn, read_turn
from app.values import digest, uid
from evaluate_advanced import MeasuredModel, check, replace_rule, update_fixture
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from verify_conversations_live import AUTH, seed

PROFILES = ("fixed12", "budget", "budget_search")


@contextmanager
def strategy(profile):
    """Experiment-only seams; restore globals before the next serial case."""
    if profile not in PROFILES:
        raise ValueError("Unknown strategy")
    original_groups, original_definitions = (
        context.conversation_groups,
        context.tool_definitions,
    )

    original_assemble = context.assemble

    def projected(*args, **kwargs):
        result = original_groups(*args, **kwargs)
        return result[-12:] if profile == "fixed12" else result

    def definitions():
        return [
            tool
            for tool in original_definitions()
            if profile == "budget_search"
            or tool["function"]["name"] != "search_conversations"
        ]

    def assembled(*args, **kwargs):
        payload, manifest, dropped = original_assemble(*args, **kwargs)
        if profile == "fixed12":
            # Explicitly lossy control: the former window policy discards older
            # groups to meet the same hard budget. Only this memory invariant
            # differs; business permissions/source/write checks remain intact.
            manifest["experiment_discarded_groups"] = len(dropped)
            return payload, manifest, []
        return payload, manifest, dropped

    with (
        patch.object(context, "conversation_groups", projected),
        patch.object(context, "assemble", assembled),
        patch.object(context, "tool_definitions", definitions),
    ):
        yield


def reset(base):
    if conninfo_to_dict(base.test_database_url).get("dbname") != "tax_harness_test":
        raise RuntimeError("Only allowlisted isolated test DB may be reset")
    with psycopg.connect(
        make_conninfo(base.admin_database_url, dbname="tax_harness_test")
    ) as conn:
        conn.execute("TRUNCATE users,companies,import_batches,rule_versions CASCADE")
    initialize(base, test=True)
    settings = base.model_copy(update={"database_url": base.test_database_url})
    db = Database(settings.database_url)
    db.open()
    return settings, db


async def run_case(base, case, profile, repetition, directory, price):
    settings, db = reset(base)
    settings = settings.model_copy(
        update={
            "context_use_summary": profile != "fixed12",
            "context_auto_compact": profile != "fixed12",
        }
    )
    model = MeasuredModel(settings, price)
    runtime = Runtime(db, settings, model)
    execute = runtime.tools.execute

    def dispatch(execution, call):
        if (
            profile != "budget_search"
            and call["function"]["name"] == "search_conversations"
        ):
            raise AppError("ACCESS_DENIED", "实验策略未启用历史搜索。", 403)
        return execute(execution, call)

    runtime.tools.execute = dispatch
    try:
        session = str(create_session(db, AUTH, "C001")["session_id"])
        seed(db, session, case["history"])
        if case["kind"] == "cross_session":
            session = str(create_session(db, AUTH, "C001")["session_id"])
        # Enough material to exercise budget compaction, with each group small
        # enough that the fixed12 control remains within the identical budget.
        for n in range(30):
            seed(
                db,
                session,
                f"合成背景第{n}轮：" + "资料只作讨论，不授权保存或变更。" * 12,
            )
        if case["kind"] == "data_change":
            update_fixture(db, settings, session)
        if case["kind"] == "company_switch":
            switch_company(db, AUTH, session, "C002")
        if case["kind"] == "rule_change":
            with psycopg.connect(
                make_conninfo(base.admin_database_url, dbname="tax_harness_test"),
                autocommit=True,
                row_factory=dict_row,
            ) as admin:
                replace_rule(admin)
        with db.transaction(readonly=True) as conn:
            before = conn.execute("SELECT count(*) n FROM operations").fetchone()["n"]
        turn = accept_turn(db, AUTH, session, uid(), case["question"])
        execution = runtime.claim()
        started = time.monotonic()
        with strategy(profile):
            await runtime.run(execution)
        elapsed = round((time.monotonic() - started) * 1000)
        result = read_turn(db, AUTH, turn["turn_id"])
        trace = read_run(db, AUTH, execution.run_id)
        failures = check(db, case, result, before)
        observed = []
        for snapshot in trace["contexts"]:
            if snapshot["manifest"].get("kind") == "compaction":
                continue
            names = {
                t["function"]["name"] for t in snapshot["payload"].get("tools", [])
            }
            count = snapshot["manifest"].get("raw_group_count", 0)
            assert ("search_conversations" in names) == (profile == "budget_search")
            assert profile != "fixed12" or count <= 12
            observed.append(
                {
                    "raw_group_count": count,
                    "selected_group_count": snapshot["manifest"].get(
                        "history_group_count"
                    ),
                    "discarded_by_budget": snapshot["manifest"].get(
                        "experiment_discarded_groups", 0
                    ),
                    "search_enabled": "search_conversations" in names,
                }
            )
        row = {
            "case_id": case["id"],
            "kind": case["kind"],
            "profile": profile,
            "repetition": repetition,
            "correct": not failures,
            "failures": failures,
            "total_ms": elapsed,
            "calls": model.calls,
            "tool_calls": len(trace["tools"]),
            "observed_policies": observed,
            "summary_contexts": sum(
                c["manifest"].get("kind") == "compaction" for c in trace["contexts"]
            ),
        }
        (directory / "result.json").write_text(
            json.dumps(
                {**row, "result": result, "trace": trace}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
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
        raise RuntimeError("Real configured model required")
    suite = json.loads(
        (ROOT / "evals/advanced-v1/cases.json").read_text(encoding="utf-8")
    )
    cases = [c for c in suite["cases"] if not args.case or c["id"] in args.case]
    if not cases or set(args.case or ()) - {c["id"] for c in cases}:
        raise ValueError("Unknown case")
    # Fixed reference normalization, not a provider bill or peak-hour inference.
    price = {
        "model": base.model_id,
        "currency": "USD",
        "as_of": "2026-11-04",
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "kind": "fixed_off_peak_flash_reference_not_invoice",
        "input_cache_hit_per_million": "0.003",
        "input_cache_miss_per_million": "0.15",
        "output_per_million": "0.6",
    }
    if base.model_id not in ("deepseek-flash", "deepseek-v4-flash"):
        price["model"] = "deepseek-flash"  # Other models remain unpriced.
    args.output.mkdir(parents=True, exist_ok=False)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "backend_fingerprint": code_fingerprint(),
        "git_revision": (
            await asyncio.to_thread(
                subprocess.check_output, ["git", "rev-parse", "HEAD"], text=True
            )
        ).strip(),
        "dataset_hash": digest(suite),
        "model": base.model_id,
        "budget": base.context_budget,
        "price": price,
        "profiles": list(PROFILES),
        "rows": [],
        "limitations": [
            "Same-candidate ablation, not old binary replay. fixed12 intentionally discards budget overflow; all business guards remain enabled.",
            "Known six-case development set; not independent human-labeled holdout or statistical proof.",
            "All strategies retain exact source reading; only budget_search enables history search.",
            "Scoring requires verified source quotes/business cards, not free-text answer quality.",
            "All actual summary/repair/failure requests included; fixture setup excluded from elapsed.",
            "Price is fixed official off-peak Flash reference, not invoice; caches/network not controlled.",
        ],
    }
    path = args.output / "report.json"
    for repetition in range(1, args.runs + 1):
        for index, case in enumerate(cases):
            rotation = (repetition + index - 1) % len(PROFILES)
            for profile in PROFILES[rotation:] + PROFILES[:rotation]:
                directory = args.output / f"{case['id']}-{profile}-{repetition}"
                directory.mkdir()
                report["rows"].append(
                    await run_case(base, case, profile, repetition, directory, price)
                )
                report["aggregate"] = {
                    p: aggregate([r for r in report["rows"] if r["profile"] == p])
                    for p in PROFILES
                }
                path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
    report["finished_at"] = datetime.now(UTC).isoformat()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--case", action="append")
    asyncio.run(main(parser.parse_args()))
