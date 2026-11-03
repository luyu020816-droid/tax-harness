"""Live business traces and autonomous pre-COMMIT recovery in a fresh DB.

The process kill is deterministic fault injection; all resumed decisions use
the configured real model. No script completes the uncommitted write.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from advanced_common import database, write
from app import business, insights, invoice_write
from app.config import ROOT, Settings
from app.identity import Identity, create_session
from app.model import HttpModel
from app.runtime import Runtime
from app.telemetry import code_fingerprint
from app.trace import read_run
from app.turns import accept_turn, read_turn
from verify_advanced_recovery import stop, wait_file

AUTH = Identity("U001")


async def verify(output, *, only_recovery=False):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    configured = Settings()
    if not configured.model_configured:
        raise SystemExit("A configured live model is required; no DB created")
    settings, db = database(configured, output)
    runtime = Runtime(db, settings, HttpModel(settings))
    session = str(create_session(db, AUTH, "C001")["session_id"])
    records = []
    current_case = ""
    failure = None

    async def run(text, name, *, allow_confirmation=False):
        nonlocal current_case
        current_case = name
        turn = accept_turn(db, AUTH, session, name, text)
        execution = runtime.claim()
        assert execution and execution.turn_id == turn["turn_id"]
        await runtime.run(execution)
        result = read_turn(db, AUTH, execution.turn_id)
        write(
            output / (name + ".json"),
            {
                "result": result,
                "trace": read_run(db, AUTH, execution.run_id),
                "insight": insights.detail(db, AUTH, execution.turn_id),
            },
        )
        confirmed = False
        if allow_confirmation and result["status"] == "waiting_user":
            assert result["pending_clarification"]["missing_fields"] == ["write_confirmation"]
            plan = next(
                c
                for c in result["final_cards"]
                if c["kind"] == "write_plan" and c["status"] == "needs_confirmation"
            )
            receipt = invoice_write.confirm(
                db,
                AUTH,
                plan["plan_id"],
                expected_revision=plan["revision"],
                intent_hash=plan["intent_hash"],
                request_id=name + "-simulated-user-confirmation",
            )
            confirmation = runtime.claim()
            assert confirmation and confirmation.turn_id == receipt["turn_id"]
            await runtime.run(confirmation)
            result = read_turn(db, AUTH, confirmation.turn_id)
            write(
                output / (name + "-confirmed.json"),
                {
                    "kind": "simulated_user_confirmation_on_synthetic_data",
                    "result": result,
                    "trace": read_run(db, AUTH, confirmation.run_id),
                    "insight": insights.detail(db, AUTH, confirmation.turn_id),
                },
            )
            confirmed = True
        assert result["status"] == "completed", result.get("error")
        records.append(
            {
                "case": name,
                "passed": True,
                "turn_id": execution.turn_id,
                "simulated_confirmation": confirmed,
            }
        )
        print(name, "PASS", flush=True)
        return execution, result

    envfile = output / ".worker.env"
    child = None
    try:
        if not only_recovery:
            _, result = await run(
                "查询甲企业2026年9月发票总额、银行收入和支出。只查询，不修改资料。", "query"
            )
            card = next(c for c in result["final_cards"] if c["kind"] == "monthly_summary")
            assert (
                card["invoice_total"] == "9622.06"
                and card["bank_income"] == "13000.50"
                and card["bank_expense"] == "7180.74"
            )
            await run("检查甲企业2026年9月哪些发票必须填用途却尚未填写。暂不修改。", "check")
            assert business.check_purposes(db, AUTH, "C001", "2026-09")["missing_count"] == 5
            await run(
                "请把甲企业F002用途填为办公资料整理，然后重新检查甲企业2026年9月还缺哪些用途。",
                "save",
                allow_confirmation=True,
            )
            assert business.check_purposes(db, AUTH, "C001", "2026-09")["missing_count"] == 4
            await run("重新检查甲企业2026年9月还缺哪些用途，只查询。", "post-save-check")
        # An actual owned process is killed before its invoice transaction commits.
        turn = accept_turn(
            db, AUTH, session, "autonomous-before", "把甲企业F003用途改为办公凭证整理"
        )
        current_case = "autonomous_precommit_recovery"
        control = output / "before"
        control.mkdir()
        envfile.write_text(
            "DATABASE_URL="
            + json.dumps(settings.database_url)
            + "\nADMIN_DATABASE_URL="
            + json.dumps(settings.admin_database_url)
            + "\n",
            encoding="utf-8",
        )
        with (control / "worker.log").open("wb") as log:
            # Standalone offline probe: synchronously own/stop this fault worker before resuming.
            child = subprocess.Popen(  # noqa: ASYNC220
                [
                    sys.executable,
                    str(ROOT / "scripts/fault_process_worker.py"),
                    "before",
                    "--control",
                    str(control),
                    "--invoice",
                    "F003",
                    "--purpose",
                    "办公凭证整理",
                ],
                env={**os.environ, "TAX_HARNESS_ENV_FILE": str(envfile)},
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            wait_file(control / "boundary.json", child)
            stop(child)
        meta = json.loads((control / "claimed.json").read_text(encoding="utf-8"))
        with db.transaction() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) n FROM operations WHERE source_id=%s", (meta["source_id"],)
                ).fetchone()["n"]
                == 0
            )
            conn.execute(
                "UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second' WHERE work_id=%s",
                (meta["work_id"],),
            )
        resumed = runtime.claim()
        deadline = time.monotonic() + 10
        while resumed is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            resumed = runtime.claim()
        assert resumed and resumed.turn_id == turn["turn_id"]
        await runtime.run(resumed)  # Intentionally no invoice_write.commit here.
        result = read_turn(db, AUTH, resumed.turn_id)
        trace = insights.detail(db, AUTH, resumed.turn_id)
        write(
            control / "resumed.json",
            {"result": result, "trace": trace, "raw": read_run(db, AUTH, resumed.run_id)},
        )
        assert result["status"] == "completed", result.get("error")
        with db.transaction(readonly=True) as conn:
            invoice = conn.execute(
                "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F003'"
            ).fetchone()
            count = conn.execute(
                "SELECT count(*) n FROM operations WHERE source_id=%s", (meta["source_id"],)
            ).fetchone()["n"]
        assert invoice == {"purpose": "办公凭证整理", "revision": 2} and count == 1
        spans = trace["runs"][-1]["spans"]
        first_model = next(s["started_at"] for s in spans if s["kind"] == "model")
        prior = next(s for s in spans if s["name"] == "恢复前核对旧操作")
        assert prior["ended_at"] <= first_model
        records.append(
            {
                "case": "autonomous_precommit_recovery",
                "passed": True,
                "turn_id": resumed.turn_id,
                "operations": 1,
                "revision": 2,
                "script_committed": False,
            }
        )
        print("autonomous_precommit_recovery PASS", flush=True)
    except Exception as exc:
        failure = {"stage": current_case, "error_type": type(exc).__name__}
        raise
    finally:
        if child is not None and child.poll() is None:
            stop(child)
        envfile.unlink(missing_ok=True)
        write(
            output / "report.json",
            {
                "code_fingerprint": code_fingerprint(),
                "model": settings.model_id,
                "mode": "live",
                "records": records,
                "passed": len(records) == (1 if only_recovery else 5) and failure is None,
                "failure": failure,
                "human_semantic_review": "pending",
            },
        )
        runtime.tools.retrieval.engine.close()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--only-recovery", action="store_true")
    args = parser.parse_args()
    asyncio.run(verify(args.output, only_recovery=args.only_recovery))
