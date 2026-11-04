"""Frozen agent-authored retrieval pack and 20 independent access negatives.

Not a human-labeled holdout and not an end-to-end model accuracy benchmark.
No model calls, no vectors, no tuning. Only the allowlisted test DB is reset.
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import psycopg
from app.config import ROOT, Settings
from app.conversation_history import evidence_in, read_page, search
from app.errors import AppError
from app.identity import Identity, create_session
from app.session_management import organize
from app.telemetry import code_fingerprint
from app.values import digest
from evaluate_conversation_strategies import reset
from psycopg.conninfo import make_conninfo
from verify_conversations_live import AUTH, seed


def observation(auth=AUTH):
    # Deliberately high watermark: cross-user isolation cannot pass merely
    # because the other user's source counter is lower than the owner's.
    return SimpleNamespace(auth=auth, source_seq=10**9)


def quality(base, suite):
    _, db = reset(base)
    try:
        session = str(create_session(db, AUTH, "C001")["session_id"])
        sources = {}
        for fixture in suite["fixtures"]:
            when = (
                datetime.fromisoformat(fixture["when"]) if fixture.get("when") else None
            )
            sources[fixture["id"]] = seed(db, session, fixture["text"], when)
        rows = []
        for case in suite["cases"]:
            started = time.monotonic()
            bundle = search(db, observation(), **case["arguments"])
            returned = [item["source_id"] for item in bundle["matches"]]
            gold = {sources[key] for key in case["gold"]}
            positions = [returned.index(key) + 1 for key in gold if key in returned]
            rows.append(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "gold_ids": case["gold"],
                    "recall_at_5": len(positions) / len(gold) if gold else None,
                    "reciprocal_rank": 1 / min(positions)
                    if positions
                    else 0
                    if gold
                    else None,
                    "correct": gold <= set(returned) if gold else not returned,
                    "false_positive": bool(returned) if not gold else None,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "bundle": bundle,
                }
            )
        with db.transaction(readonly=True) as conn:
            assert (
                conn.execute("SELECT count(*) n FROM knowledge_documents").fetchone()[
                    "n"
                ]
                == 0
            )
        return rows
    finally:
        db.close()


def negative(base, threat, channel):
    _, db = reset(base)
    try:
        session = str(create_session(db, AUTH, "C001")["session_id"])
        first = seed(db, session, "PRIVATE-BOUNDARY-481，用户原文。")
        seed(db, session, "PRIVATE-BOUNDARY-481，第二条原文。")
        with db.transaction(readonly=True) as conn:
            assistant = str(
                conn.execute(
                    "SELECT a.source_id FROM sources a JOIN sources u USING(turn_id) WHERE u.source_id=%s AND a.role='assistant'",
                    (first,),
                ).fetchone()["source_id"]
            )
        bundle = search(db, observation(), query="PRIVATE-BOUNDARY-481", page_size=1)
        assert bundle["matches"][0]["source_id"] == first and bundle["next_cursor"]
        auth = AUTH
        if threat == "other_user":
            auth = Identity(
                "U002"
            )  # Also authorized for C001; still cannot read U001 chat.
        elif threat == "deleted":
            organize(db, AUTH, session, "delete", 1)
        else:
            with psycopg.connect(
                make_conninfo(base.admin_database_url, dbname="tax_harness_test")
            ) as conn:
                if threat == "revoked":
                    conn.execute(
                        "DELETE FROM user_company_grants WHERE user_id='U001' AND company_id='C001'"
                    )
                elif threat == "suppressed":
                    conn.execute(
                        "UPDATE sources SET recall_state='suppressed' WHERE turn_id IN (SELECT turn_id FROM turns WHERE session_id=%s)",
                        (session,),
                    )
                else:
                    raise ValueError("Unknown negative threat")
        try:
            if channel == "search":
                result = search(db, observation(auth), query="PRIVATE-BOUNDARY-481")
            elif channel == "cursor":
                result = search(
                    db,
                    observation(auth),
                    query="PRIVATE-BOUNDARY-481",
                    page_size=1,
                    cursor=bundle["next_cursor"],
                )
            elif channel == "evidence":
                with db.transaction(readonly=True) as conn:
                    result = evidence_in(
                        conn, auth, bundle["matches"][0]["citation_id"]
                    )
            else:
                result = read_page(
                    db,
                    observation(auth),
                    source_id=first if channel == "read_user" else assistant,
                )
        except AppError as error:
            if error.code not in (
                "ACCESS_DENIED",
                "RESOURCE_NOT_FOUND",
                "SOURCE_INVALIDATED",
                "INVALID_ARGUMENT",
            ):
                raise
            return {
                "threat": threat,
                "channel": channel,
                "passed": True,
                "denied_code": error.code,
            }
        passed = channel in ("search", "cursor") and result["matches"] == []
        return {
            "threat": threat,
            "channel": channel,
            "passed": passed,
            "denied_code": "empty" if passed else None,
        }
    finally:
        db.close()


def main(args):
    base = Settings()
    suite = json.loads(
        (ROOT / "evals/conversation-v1/cases.json").read_text(encoding="utf-8")
    )
    args.output.mkdir(parents=True, exist_ok=False)
    rows = quality(base, suite)
    report = {
        "backend_fingerprint": code_fingerprint(),
        "dataset_hash": digest(suite),
        "label_status": suite["label_status"],
        "retrieval_only": True,
        "quality": rows,
        "safety": [],
        "by_category": {},
    }
    for category in dict.fromkeys(row["category"] for row in rows):
        group = [row for row in rows if row["category"] == category]
        answerable = [row for row in group if row["recall_at_5"] is not None]
        report["by_category"][category] = {
            "count": len(group),
            "correct": sum(row["correct"] for row in group),
            "recall_at_5": sum(row["recall_at_5"] for row in answerable)
            / len(answerable)
            if answerable
            else None,
            "mrr": sum(row["reciprocal_rank"] for row in answerable) / len(answerable)
            if answerable
            else None,
            "false_positives": sum(bool(row["false_positive"]) for row in group),
        }
    path = args.output / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for threat in ("other_user", "deleted", "revoked", "suppressed"):
        for channel in ("search", "read_user", "read_assistant", "evidence", "cursor"):
            row = negative(base, threat, channel)
            report["safety"].append(row)
            print(threat, channel, row["passed"], flush=True)
            path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    if not all(row["passed"] for row in report["safety"]):
        raise SystemExit(1)
    print(json.dumps(report["by_category"], ensure_ascii=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
