"""Owned subprocess used only by verify_system_faults; never a production worker."""

import argparse
import time
from contextlib import contextmanager

import psycopg
from app import invoice_write
from app.config import Settings
from app.db import Database
from app.identity import Identity
from app.runtime import Runtime
from evaluate_retrieval import write


class NoModel:
    mode = "fault-injection-no-model"

    async def complete(self, *args, **kwargs):
        raise AssertionError("This fault probe must never call a model")


def pause():
    while True:
        time.sleep(0.2)


def main(args):
    settings = Settings()
    db = Database(settings.database_url)
    db.open()
    execution = Runtime(db, settings, NoModel()).claim()
    assert execution
    auth = Identity("U001")
    with db.transaction(readonly=True) as conn:
        source = conn.execute(
            "SELECT * FROM sources WHERE source_id=%s", (execution.source_id,)
        ).fetchone()
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE company_id='C001' AND invoice_id=%s", (args.invoice,)
        ).fetchone()
    plan = invoice_write.prepare(
        db,
        auth,
        company_id="C001",
        invoice_id=args.invoice,
        patch={"purpose": args.purpose},
        expected_revision=invoice["revision"],
        source_id=execution.source_id,
        source_refs=[{"source_id": execution.source_id, "quote": source["content"]}],
        execution=execution,
    )
    metadata = {
        "plan_id": str(plan["plan_id"]),
        "work_id": execution.work_id,
        "turn_id": execution.turn_id,
        "fencing_token": execution.fencing_token,
        "source_id": execution.source_id,
    }
    write(args.control / "claimed.json", metadata)
    original = db.transaction
    committed = False

    @contextmanager
    def transaction(*, readonly=False):
        nonlocal committed
        if readonly and committed and args.mode == "read-failure":
            committed = False
            raise psycopg.OperationalError("Injected read-back connection loss")
        with original(readonly=readonly) as conn:
            yield conn
            if not readonly:
                row = conn.execute(
                    "SELECT 1 FROM operations WHERE source_id=%s", (execution.source_id,)
                ).fetchone()
                if row:
                    if args.mode == "before":
                        write(args.control / "boundary.json", metadata)
                        pause()
                    committed = True

    db.transaction = transaction
    result = invoice_write.commit(db, auth, str(plan["plan_id"]), execution=execution)
    if args.mode == "after":
        write(args.control / "boundary.json", metadata)
        pause()
    write(args.control / "result.json", result)
    db.close()


if __name__ == "__main__":
    from pathlib import Path

    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["before", "after", "read-failure"])
    p.add_argument("--control", type=Path, required=True)
    p.add_argument("--invoice", required=True)
    p.add_argument("--purpose", required=True)
    main(p.parse_args())
