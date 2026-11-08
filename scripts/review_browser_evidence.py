"""Independently compare browser evidence with the isolated test database (read-only)."""

import argparse
import json
from datetime import date
from decimal import Decimal

import psycopg
from app.config import Settings
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    settings = Settings()
    if conninfo_to_dict(settings.test_database_url).get("dbname") != "tax_harness_test":
        raise SystemExit("Only tax_harness_test is permitted.")
    results = []
    seen = set()
    with psycopg.connect(settings.test_database_url, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        for path in sorted(args.evidence.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            turn = raw.get("result")
            if not isinstance(turn, dict) or "turn_id" not in turn:
                continue
            if turn["turn_id"] in seen:
                continue
            seen.add(turn["turn_id"])
            for card in turn.get("final_cards", []):
                if card["kind"] == "monthly_summary":
                    year, month = map(int, card["period"].split("-"))
                    start = date(year, month, 1)
                    end = date(year + (month == 12), month % 12 + 1, 1)
                    invoice = conn.execute(
                        "SELECT coalesce(sum(amount),0) total FROM invoices "
                        "WHERE company_id=%s AND invoice_date>=%s AND invoice_date<%s",
                        (card["company_id"], start, end),
                    ).fetchone()
                    bank = conn.execute(
                        "SELECT coalesce(sum(amount) FILTER(WHERE direction='income'),0) income,"
                        "coalesce(sum(amount) FILTER(WHERE direction='expense'),0) expense "
                        "FROM bank_entries WHERE company_id=%s "
                        "AND transaction_date>=%s AND transaction_date<%s",
                        (card["company_id"], start, end),
                    ).fetchone()
                    actual = [invoice["total"], bank["income"], bank["expense"]]
                    observed = [
                        Decimal(str(card[k]))
                        for k in ("invoice_total", "bank_income", "bank_expense")
                    ]
                    results.append(
                        {
                            "file": path.name,
                            "company": card["company_id"],
                            "period": card["period"],
                            "kind": "independent_amounts",
                            "pass": actual == observed,
                            "database": list(map(str, actual)),
                            "card": list(map(str, observed)),
                        }
                    )
                elif card["kind"] == "operation":
                    row = conn.execute(
                        "SELECT * FROM operations WHERE operation_id=%s",
                        (card["operation_id"],),
                    ).fetchone()
                    current = conn.execute(
                        "SELECT * FROM invoices WHERE company_id=%s AND invoice_id=%s",
                        (card["company_id"], card["invoice_id"]),
                    ).fetchone()
                    count = conn.execute(
                        "SELECT count(*) n FROM operations o JOIN sources s ON s.source_id=o.source_id "
                        "WHERE s.turn_id=%s AND o.state='committed'",
                        (turn["turn_id"],),
                    ).fetchone()["n"]
                    passed = (
                        bool(row and current)
                        and count == 1
                        and row["state"] == "committed"
                    )
                    passed = passed and all(
                        current[k] == v for k, v in card["patch"].items()
                    )
                    passed = passed and current["revision"] == card["after_revision"]
                    results.append(
                        {
                            "file": path.name,
                            "kind": "committed_receipt",
                            "operation_id": card["operation_id"],
                            "pass": passed,
                            "committed_count": count,
                            "current_revision": current["revision"]
                            if current
                            else None,
                        }
                    )
        total = conn.execute(
            "SELECT count(*) n FROM operations WHERE state='committed'"
        ).fetchone()["n"]
    report = {
        "scope": "current isolated database; run before resetting it",
        "checks": results,
        "all_pass": bool(results) and all(r["pass"] for r in results),
        "total_committed_operations": total,
        "review_type": "independent SQL checks, not human semantic signoff",
    }
    output = args.evidence / "independent-database-review.json"
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(
        f"{len(results)} independent checks; all_pass={report['all_pass']}; committed={total}"
    )
    if not report["all_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
