"""Small live-model acceptance over synthetic history in the isolated test DB.

Seeded history is a fixture, not a claim of 30 live-model conversations.
Never prints model inputs or credentials. Run only after isolated pytest exits.
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from app.cli import initialize
from app.config import Settings
from app.db import Database
from app.identity import Identity, create_session
from app.model import HttpModel
from app.runtime import Runtime
from app.sources import add_source, assess
from app.telemetry import code_fingerprint
from app.turns import accept_turn, read_turn
from app.values import safe_json, uid
from psycopg.conninfo import make_conninfo

AUTH = Identity("U001")


def seed(db, session, text, when=None):
    # Synthetic historical fixtures predate the current minute; real admission
    # limits remain unchanged for actual test questions and the demo service.
    when = when or datetime.now(UTC) - timedelta(hours=2)
    turn = accept_turn(db, AUTH, session, uid(), text)
    with db.transaction() as conn:
        original = conn.execute(
            "SELECT source_id FROM sources WHERE turn_id=%s AND role='user'",
            (turn["turn_id"],),
        ).fetchone()["source_id"]
        assess(conn, str(original), "no_change")
        reply = add_source(
            conn,
            AUTH,
            "已记录背景，未执行修改。",
            role="assistant",
            kind="assistant_final",
            turn_id=turn["turn_id"],
            source_seq=turn["source_seq"],
            companies=["C001"],
        )
        assess(conn, str(reply["source_id"]), "no_change")
        conn.execute(
            "UPDATE turns SET status='completed',final_message=%s,final_response_ref=%s WHERE turn_id=%s",
            (reply["content"], reply["source_id"], turn["turn_id"]),
        )
        conn.execute(
            "UPDATE work_items SET state='done' WHERE source_id=%s", (original,)
        )
    if when:
        config = Settings()
        with psycopg.connect(
            make_conninfo(config.admin_database_url, dbname="tax_harness_test"),
            connect_timeout=5,
        ) as conn:
            conn.execute(
                "UPDATE sources SET created_at=%s WHERE turn_id=%s",
                (when, turn["turn_id"]),
            )
            conn.execute(
                "UPDATE turns SET created_at=%s WHERE turn_id=%s",
                (when, turn["turn_id"]),
            )
    return str(original)


async def main(output):
    config = Settings()
    if not config.model_configured:
        raise RuntimeError("A configured real model is required")
    from psycopg.conninfo import conninfo_to_dict

    if conninfo_to_dict(config.test_database_url).get("dbname") != "tax_harness_test":
        raise RuntimeError("Only the isolated tax_harness_test DB is permitted")
    output.mkdir(parents=True, exist_ok=False)
    with psycopg.connect(
        make_conninfo(config.admin_database_url, dbname="tax_harness_test"),
        connect_timeout=5,
    ) as conn:
        conn.execute("TRUNCATE users,companies,import_batches,rule_versions CASCADE")
    initialize(config, test=True)
    config = config.model_copy(update={"database_url": config.test_database_url})
    db = Database(config.database_url)
    db.open()
    runtime = Runtime(db, config, HttpModel(config))
    checks = []

    async def run(
        name, session, question, expected_source=None, marker=None, expect_empty=False
    ):
        turn = accept_turn(db, AUTH, session, uid(), question)
        execution = runtime.claim()
        await runtime.run(execution)
        result = read_turn(db, AUTH, turn["turn_id"])
        cards = result.get("final_cards", [])
        with db.transaction(readonly=True) as conn:
            contexts = conn.execute(
                "SELECT c.manifest,c.usage,c.payload FROM context_snapshots c JOIN runs r USING(run_id) WHERE r.turn_id=%s ORDER BY c.model_step",
                (turn["turn_id"],),
            ).fetchall()
            events = conn.execute(
                "SELECT kind FROM run_events WHERE turn_id=%s", (turn["turn_id"],)
            ).fetchall()
        cited = (
            any(str(c.get("source_id")) == expected_source for c in cards)
            if expected_source
            else True
        )
        in_input = (
            any(
                marker in json.dumps(c["payload"], ensure_ascii=False) for c in contexts
            )
            if marker
            else True
        )
        empty = (
            (
                not any(c.get("kind") == "history_quote" for c in cards)
                and "未找到" in (result.get("final_message") or "")
            )
            if expect_empty
            else True
        )
        (output / f"{name}-evidence.json").write_text(
            json.dumps(
                safe_json(
                    {
                        "result": result,
                        "contexts": contexts,
                        "expected_source": expected_source,
                    }
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        checks.append(
            {
                "code_fingerprint": code_fingerprint(),
                "input_tokens": sum(
                    c["usage"].get("prompt_tokens", 0) for c in contexts if c["usage"]
                ),
                "output_tokens": sum(
                    c["usage"].get("completion_tokens", 0)
                    for c in contexts
                    if c["usage"]
                ),
                "usage_known": sum(bool(c["usage"]) for c in contexts),
                "max_estimated_input": max(
                    (c["manifest"].get("token_count", 0) for c in contexts), default=0
                ),
                "name": name,
                "turn_id": turn["turn_id"],
                "status": result["status"],
                "error": result.get("error"),
                "expected_source_cited": cited,
                "marker_in_actual_input": in_input,
                "no_false_quote": empty,
                "model_requests": len(contexts),
                "compaction_published": sum(
                    e["kind"] == "context_compacted" for e in events
                ),
                "passed": result["status"] == "completed"
                and cited
                and in_input
                and empty,
            }
        )
        (output / "report.json").write_text(
            json.dumps(
                {"model": config.model_id, "synthetic_history": True, "checks": checks},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(name, result["status"], "passed=" + str(checks[-1]["passed"]), flush=True)

    try:
        same = str(create_session(db, AUTH, "C001")["session_id"])
        early_source = seed(
            db,
            same,
            "甲企业资料整理方案唯一标记EARLY-724：先备份原件，再核对账本，不自动改发票。",
        )
        for n in range(29):
            seed(db, same, f"背景编号{n}，不修改业务数据。")
        await run(
            "same_session_30",
            same,
            "请回忆本会话最开始的资料整理方案，找出EARLY-724原话。",
            expected_source=early_source,
            marker="EARLY-724",
        )
        old = str(create_session(db, AUTH, "C001")["session_id"])
        yesterday = datetime.now(UTC) - timedelta(days=1)
        source = seed(
            db,
            old,
            "甲企业归档方案 CROSS-829：先备份，再检查，禁止直接改票。",
            yesterday,
        )
        new = str(create_session(db, AUTH, "C001")["session_id"])
        await run(
            "cross_session_yesterday",
            new,
            "查找昨天其他会话里CROSS-829归档方案，请逐字引用。",
            source,
        )
        month = str(create_session(db, AUTH, "C001")["session_id"])
        source = seed(
            db,
            month,
            "甲企业两个月前的档案柜号为TWO-MONTH-936，不授权修改。",
            datetime.now(UTC) - timedelta(days=62),
        )
        new = str(create_session(db, AUTH, "C001")["session_id"])
        await run(
            "two_month_history",
            new,
            "查找约两个月前聊天中的档案柜号TWO-MONTH-936，引用当时原话。",
            source,
        )
        long = str(create_session(db, AUTH, "C001")["session_id"])
        source = seed(
            db,
            long,
            "早期方案 COMPACT-947：柜号Q7421，只允许西侧柜，不得外发；F002未确认，不授权保存。",
        )
        for n in range(25):
            seed(
                db, long, f"背景记录{n}：" + "只讨论资料整理，当前未授权更改发票。" * 35
            )
        await run(
            "forced_compaction",
            long,
            "请回忆COMPACT-947的档案柜限制和未完成项，必要时查原文，逐字引用。",
            source,
        )
        new = str(create_session(db, AUTH, "C001")["session_id"])
        await run(
            "no_answer",
            new,
            "查找我以前提过的不存在标记MISSING-00000099，没有找到就说明，不能猜。",
            expect_empty=True,
        )
    finally:
        runtime.tools.retrieval.engine.close()
        db.close()
    if not all(c["passed"] for c in checks):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.output.resolve()))
