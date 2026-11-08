"""Small development smoke suite, real model, isolated test DB, human review pending.

Run serially after pytest. Raw failures are retained; this is not a holdout or
an official score. Never accepts a production database or overwrites evidence.
"""

import argparse
import asyncio
import json
from pathlib import Path

from app.config import ROOT, Settings
from app.values import canonical, digest
from evaluate import run_case
from psycopg.conninfo import conninfo_to_dict

CASES = [
    (
        "continued-company",
        "查询乙企业2026年8月发票总额，并查询2026年9月发票总额",
        "C001",
        "U001",
        [
            ("query_month", "C002", "2026-08", "verified"),
            ("query_month", "C002", "2026-09", "verified"),
        ],
    ),
    (
        "two-months",
        "查询甲企业2026年8月和9月发票总额",
        "C001",
        "U001",
        [
            ("query_month", "C001", "2026-08", "verified"),
            ("query_month", "C001", "2026-09", "verified"),
        ],
    ),
    (
        "query-rules",
        "查询甲企业2026年9月发票总额，并解释适用规则",
        "C001",
        "U001",
        [
            ("query_month", "C001", "2026-09", "verified"),
            ("get_rules", "C001", "2026-09", "verified"),
        ],
    ),
    (
        "paired-scopes",
        "查询甲企业2026年8月发票总额；查询乙企业2026年9月发票总额",
        "C001",
        "U001",
        [
            ("query_month", "C001", "2026-08", "verified"),
            ("query_month", "C002", "2026-09", "verified"),
        ],
    ),
    (
        "missing-month",
        "查询甲企业月度发票总额",
        "C001",
        "U001",
        [("query_month", "C001", None, "waiting_user")],
    ),
    (
        "unsupported-month",
        "检查甲企业2026年8月发票用途",
        "C001",
        "U001",
        [("check_purposes", "C001", "2026-08", "blocked")],
    ),
    (
        "permission-denied",
        "查询乙企业2026年9月发票总额",
        None,
        "U002",
        [("query_month", "C002", "2026-09", "blocked")],
    ),
]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runs", default=1, type=int, choices=range(1, 4))
    args = parser.parse_args()
    settings = Settings()
    if not settings.model_configured:
        raise SystemExit("Model not configured; no reset or model call.")
    if conninfo_to_dict(settings.test_database_url).get("dbname") != "tax_harness_test":
        raise SystemExit("Only tax_harness_test is permitted.")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {
        "suite": "checklist-development-smoke",
        "human_review": "pending",
        "cases_hash": digest(CASES),
        "backend_fingerprint": digest(
            {
                p.name: p.read_text(encoding="utf-8")
                for p in sorted((ROOT / "backend/app").glob("*.py"))
            }
        ),
        "runs": [],
    }
    for case_id, text, company, user, expected in CASES:
        for repetition in range(1, args.runs + 1):
            case = {
                "id": case_id,
                "category": "request-checklist",
                "company": company,
                "steps": [{"text": text, "user": user}],
                "expected": {"no_writes": True},
                "manual_criteria": "核对事项、企业月份、原始结果、规则与自然表达；待人工签收。",
            }
            result = await run_case(settings, case, repetition, args.output)
            raw = json.loads(
                (args.output / f"{case_id}-run-{repetition}.json").read_text(
                    encoding="utf-8"
                )
            )
            turn = raw["transcript"][-1]["result"]
            progress = [
                i
                for c in turn["final_cards"]
                if c["kind"] == "request_progress"
                for i in c["items"]
            ]
            observed = [
                (i["tool"], i["company_id"], i["period"], i["status"]) for i in progress
            ]
            if set(observed) != set(expected):
                result["automatic_failures"].append(
                    "Checklist scopes/statuses differ from frozen expectations"
                )
            if case_id == "missing-month" and turn["status"] != "waiting_user":
                result["automatic_failures"].append(
                    "Missing month did not wait for user"
                )
            # Independent known sample totals, not the model's own expected answer.
            for card in turn["final_cards"]:
                if card["kind"] == "monthly_summary" and card["company_id"] == "C001":
                    wanted = {"2026-08": "9046.35", "2026-09": "9622.06"}.get(
                        card["period"]
                    )
                    if card["invoice_total"] != wanted:
                        result["automatic_failures"].append(
                            "Known sample invoice total differs"
                        )
            result["automatic_pass"] = not result["automatic_failures"]
            report["runs"].append(result)
            report["automatic_passes"] = sum(
                r["automatic_pass"] for r in report["runs"]
            )
            (args.output / "report.json").write_text(
                canonical(report), encoding="utf-8"
            )
            print(
                f"{case_id}/{repetition}: {'pass' if result['automatic_pass'] else 'fail'}, human=pending",
                flush=True,
            )
    if report["automatic_passes"] != len(report["runs"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
