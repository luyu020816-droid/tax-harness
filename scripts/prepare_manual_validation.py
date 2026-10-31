"""Create a private, isolated human-review baseline without touching preview data."""

import argparse
import json
from pathlib import Path

from app.config import Settings
from evaluate_retrieval import database, write


def main(args):
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    s, db = database(Settings(), "development", args.output)
    try:
        values = {
            key.upper(): getattr(s, key)
            for key in [
                "database_url",
                "admin_database_url",
                "model_base_url",
                "model_id",
                "model_api_key",
                "model_context_limit",
                "model_extra_body",
                "input_budget",
                "output_reserve",
            ]
        }
        (args.output / ".env").write_text(
            "".join(k + "=" + json.dumps(v, ensure_ascii=False) + "\n" for k, v in values.items()),
            encoding="utf-8",
        )
        with db.transaction(readonly=True) as conn:
            baseline = {
                "invoices": conn.execute("SELECT count(*) AS n FROM invoices").fetchone()["n"],
                "bank_transactions": conn.execute(
                    "SELECT count(*) AS n FROM bank_entries"
                ).fetchone()["n"],
                "F002": conn.execute(
                    "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
                ).fetchone(),
            }
        write(args.output / "baseline.json", baseline)
        print("Isolated manual review baseline created; credentials kept in its private .env.")
    finally:
        db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    main(p.parse_args())
