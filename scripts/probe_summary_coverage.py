"""Reproduce a semantic limitation, not a summary-quality passing score.

Uses a deliberately incomplete injected summary in a fresh isolated database.
No live model or private text. Exit 0 means the limitation was reproduced.
"""

import argparse
import asyncio
import json
from pathlib import Path

from app.compaction import compact
from app.config import Settings
from app.identity import Identity, create_session
from app.runtime import Runtime
from app.sources import add_source, assess, next_sequence
from app.turns import accept_turn
from app.values import uid
from evaluate_retrieval import database, write


class LossySummary:
    mode = "injected-lossy-summary-no-live-model"

    async def complete(self, payload):
        labels = json.loads(payload["messages"][-1]["content"])["source_ids"]
        return {
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "summary": "用户提供了项目背景。",
                        "covered_sources": labels,
                    },
                    ensure_ascii=False,
                ),
            },
            "usage": None,
            "finish_reason": "stop",
        }


async def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    settings, db = database(Settings(), "development", args.output)
    auth = Identity("U001")
    runtime = Runtime(db, settings, LossySummary())
    try:
        original = "甲企业样品只允许放在西侧柜，不得外发。柜号为Q7421。"
        with db.transaction() as conn:
            source = add_source(
                conn, auth, original, source_seq=next_sequence(conn, auth), companies=["C001"]
            )
            assess(conn, str(source["source_id"]), "no_change")
        session = create_session(db, auth, "C001")
        accept_turn(db, auth, str(session["session_id"]), uid(), "继续讨论项目背景。")
        execution = runtime.claim()
        groups = [
            {
                "messages": [{"role": "user", "content": original}],
                "sources": [{"source_id": str(source["source_id"]), "revision": 1}],
            }
        ]
        published = await compact(db, settings, LossySummary(), execution, groups, 1)
        with db.transaction(readonly=True) as conn:
            summary = conn.execute(
                "SELECT content,covered_sources FROM summaries WHERE user_id=%s AND status='active'",
                (auth.user_id,),
            ).fetchone()
            raw = conn.execute(
                "SELECT content FROM sources WHERE source_id=%s", (source["source_id"],)
            ).fetchone()["content"]
        observation = {
            "kind": "negative_control_not_quality_pass",
            "summary_published": published,
            "source_labels_retained": len(summary["covered_sources"]) == 1,
            "critical_fact_absent_from_summary": "Q7421" not in summary["content"]
            and "不得外发" not in summary["content"],
            "original_text_preserved": raw == original,
        }
        observation["limitation_reproduced"] = all(
            observation[k]
            for k in (
                "summary_published",
                "source_labels_retained",
                "critical_fact_absent_from_summary",
                "original_text_preserved",
            )
        )
        write(args.output / "observation.json", observation)
        assert observation["limitation_reproduced"], observation
        print(
            "LIMITATION REPRODUCED: source labels do not prove semantic coverage; original text remains intact."
        )
    finally:
        runtime.tools.retrieval.engine.close()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
