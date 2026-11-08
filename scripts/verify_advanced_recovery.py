"""Kill owned child processes around COMMIT, then resume the original source.

This is deterministic fault injection on real PostgreSQL, not a live-model test.
It requires no OCR/dense model, creates a new database and never resets user data.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import psycopg
from advanced_common import database, write
from app import invoice_write
from app.config import ROOT, Settings
from app.errors import AppError
from app.execution import guard
from app.identity import Identity, create_session
from app.model import HttpModel
from app.runtime import Runtime
from app.trace import read_run
from app.turns import accept_turn, read_turn
from app.values import uid
from psycopg.rows import dict_row

AUTH = Identity("U001")


class ReceiptModel:
    mode = "deterministic-fault-test-not-live-model"

    async def complete(self, payload):
        outputs = [
            json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"
        ]
        receipts = [
            o["data"]
            for o in outputs
            if o.get("ok") and o["data"].get("kind") == "operation"
        ]
        assert receipts, "The runtime must read the old result before inference"
        return {
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "blocks": [
                            {
                                "type": "operation",
                                "operation_id": receipts[-1]["operation_id"],
                            }
                        ],
                        "status": "completed",
                    }
                ),
            },
            "usage": None,
        }


def stop(child):
    if child.poll() is None:
        result = subprocess.run(
            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
        )
        if result.returncode and child.poll() is None:
            raise RuntimeError(
                "Owned child termination failed: "
                + result.stdout.decode(errors="replace")
                + result.stderr.decode(errors="replace")
            )
    child.wait(timeout=10)


def wait_file(path, child):
    deadline = time.monotonic() + 35
    while not path.exists():
        assert child.poll() is None, "Child failed; inspect its private worker.log"
        assert time.monotonic() < deadline, "Transaction boundary not observed"
        time.sleep(0.1)
    return json.loads(path.read_text(encoding="utf-8"))


async def main(args):
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    configured = Settings()
    if args.live_model and not configured.model_configured:
        raise SystemExit("Configure a live model first.")
    settings, db = database(configured, args.output)
    envfile = args.output / ".env"
    envfile.write_text(
        "DATABASE_URL="
        + json.dumps(settings.database_url)
        + "\nADMIN_DATABASE_URL="
        + json.dumps(settings.admin_database_url)
        + "\n",
        encoding="utf-8",
    )
    runtime = Runtime(
        db, settings, HttpModel(settings) if args.live_model else ReceiptModel()
    )
    records = []
    try:
        with psycopg.connect(
            settings.admin_database_url, autocommit=True, row_factory=dict_row
        ) as admin:
            session = str(create_session(db, AUTH, "C001")["session_id"])
            for mode, invoice, purpose in [
                ("before", "F002", "办公资料整理"),
                ("after", "F003", "办公档案归集"),
                ("read-failure", "F004", "办公凭证整理"),
            ]:
                control = args.output / mode
                control.mkdir()
                turn = accept_turn(
                    db, AUTH, session, uid(), f"把甲企业{invoice}用途改为{purpose}"
                )
                before = admin.execute(
                    "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id=%s",
                    (invoice,),
                ).fetchone()
                with (control / "worker.log").open("wb") as logfile:
                    child = subprocess.Popen(  # noqa: ASYNC220 -- Fault harness owns the process and its commit boundary.
                        [
                            sys.executable,
                            str(ROOT / "scripts/fault_process_worker.py"),
                            mode,
                            "--control",
                            str(control),
                            "--invoice",
                            invoice,
                            "--purpose",
                            purpose,
                        ],
                        env={**os.environ, "TAX_HARNESS_ENV_FILE": str(envfile)},
                        stdout=logfile,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    try:
                        marker = wait_file(
                            control
                            / (
                                "result.json"
                                if mode == "read-failure"
                                else "boundary.json"
                            ),
                            child,
                        )
                        if mode == "before":
                            assert (
                                admin.execute(
                                    "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id=%s",
                                    (invoice,),
                                ).fetchone()
                                == before
                            )
                        if mode == "read-failure":
                            child.wait(timeout=10)
                            assert (
                                marker["commit_state"] == "committed"
                                and marker["verification_state"] == "pending"
                            )
                        else:
                            stop(child)
                    finally:
                        if child.poll() is None:
                            stop(child)
                meta = json.loads(
                    (control / "claimed.json").read_text(encoding="utf-8")
                )
                admin.execute(
                    "UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second' WHERE work_id=%s",
                    (meta["work_id"],),
                )
                takeover = runtime.claim()
                deadline = time.monotonic() + 10
                while takeover is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                    takeover = runtime.claim()
                assert (
                    takeover
                    and takeover.turn_id == turn["turn_id"]
                    and takeover.fencing_token > meta["fencing_token"]
                )
                # Check durable result first. If COMMIT rolled back there is none.
                prior = runtime._recovery_results(takeover)
                assert bool(prior) == (mode != "before")
                replay = invoice_write.commit(
                    db, AUTH, meta["plan_id"], execution=takeover
                )
                assert replay["verification_state"] == "verified" and replay[
                    "replayed"
                ] == (mode != "before")
                stale = replace(takeover, fencing_token=meta["fencing_token"])
                try:
                    with db.transaction() as conn:
                        guard(conn, stale)
                    raise AssertionError("Old worker remained authorized")
                except AppError as error:
                    assert error.code == "SOURCE_INVALIDATED"
                await runtime.run(takeover)
                result = read_turn(db, AUTH, turn["turn_id"])
                trace = read_run(db, AUTH, takeover.run_id)
                write(control / "turn.json", result)
                write(control / "trace.json", trace)
                assert result["status"] == "completed", result.get("error")
                started_tools = [
                    e for e in trace["events"] if e["kind"] == "tool_started"
                ]
                assert started_tools[0]["payload"]["name"] == "get_invoice_operation"
                count = admin.execute(
                    "SELECT count(*) AS n FROM operations WHERE source_id=%s",
                    (meta["source_id"],),
                ).fetchone()["n"]
                revision = admin.execute(
                    "SELECT revision FROM invoices WHERE company_id='C001' AND invoice_id=%s",
                    (invoice,),
                ).fetchone()["revision"]
                assert count == 1 and revision == before["revision"] + 1
                row = {
                    "mode": mode,
                    "pass": True,
                    "operations": count,
                    "invoice_revision": revision,
                    "first_action": "read_prior_result",
                    "operation_id": str(replay["operation_id"]),
                    "old_fence_rejected": True,
                }
                records.append(row)
                write(control / "trace.json", trace)
                write(
                    args.output / "report.json",
                    {
                        "kind": "real_process_and_database_faults",
                        "model_mode": runtime.model.mode,
                        "records": records,
                    },
                )
                print(mode, "PASS", flush=True)
    finally:
        envfile.unlink(missing_ok=True)
        runtime.tools.retrieval.engine.close()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--live-model",
        action="store_true",
        help="Use configured live model only for recovery replies, never fault boundaries.",
    )
    asyncio.run(main(parser.parse_args()))
