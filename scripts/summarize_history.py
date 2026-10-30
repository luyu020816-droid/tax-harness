"""Postprocess recorded, frozen rankings; no model calls or threshold refitting."""

import argparse
import json
import math
import statistics
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.retrieval_metrics import ndcg
from app.values import digest
from evaluate_retrieval import write
from psycopg.conninfo import make_conninfo


def main(args):
    assert not args.output.exists(), "Use a new output artifact"
    root = args.evaluation.resolve()
    original = json.loads((root / "report.json").read_text(encoding="utf-8"))
    sources = json.loads((root / "source-identities.json").read_text(encoding="utf-8"))
    suite = json.loads((root / "frozen-suite.json").read_text(encoding="utf-8"))
    name = (
        "tax_harness_retrieval_"
        + ("dev" if original["split"] == "development" else "holdout")
        + "_v1_"
        + digest(str(root))[:8]
    )
    db = Database(make_conninfo(Settings().database_url, dbname=name))
    db.open()
    try:
        with db.transaction(readonly=True) as conn:
            passage_sources = {
                str(r["passage_id"]): str(r["source_id"])
                for r in conn.execute(
                    "SELECT passage_id,source_id FROM history_passages"
                ).fetchall()
            }
    finally:
        db.close()
    annotations = {sources[r["id"]]: r for r in suite["sources"]}
    measurements = []
    latency = {"baseline": [], "selected": []}
    tokens = []
    for q in suite["questions"]:
        for stage in ("baseline", "selected"):
            saved = json.loads(
                (root / (q["id"] + "-" + stage + ".json")).read_text(encoding="utf-8")
            )
            row = {
                "id": q["id"],
                "stage": stage,
                "family": q["family"],
                "answerable": q["answerable"],
            }
            if q["answerable"]:
                gold = sources[q["source"]]
                channels = saved["trace"]["coverage"]["candidate_channels"]
                channels = {
                    **channels,
                    "reranked": [c["chunk_id"] for c in saved["trace"]["candidates"]],
                }
                for channel, ids in channels.items():
                    ranked = list(dict.fromkeys(passage_sources.get(str(i), str(i)) for i in ids))
                    row[channel + "_recall20"] = int(gold in ranked[:20])
                    row[channel + "_ndcg10"] = ndcg(ranked, {gold: 3})
            row["role_and_locator_valid"] = all(
                e["role"] == annotations[e["source_id"]]["role"]
                and annotations[e["source_id"]]["content"][
                    e["locator"]["char_start"] : e["locator"]["char_end"]
                ]
                == e["excerpt"]
                and digest(e["excerpt"]) == e["excerpt_hash"]
                for e in saved["bundle"]["evidence"]
            )
            latency[stage].append(saved["trace"]["timings"]["total_ms"])
            if stage == "selected":
                tokens.append(saved["trace"]["coverage"]["evidence_tokens"])
            measurements.append(row)
    positive = [r for r in measurements if r["stage"] == "selected" and r["answerable"]]
    metrics = {
        key: statistics.mean(r[key] for r in positive)
        for key in positive[0]
        if key.endswith(("_recall20", "_ndcg10"))
    }
    timings = {
        k: {
            "p50_ms": statistics.median(v),
            "p95_ms": sorted(v)[math.ceil(0.95 * len(v)) - 1],
            "max_ms": max(v),
        }
        for k, v in latency.items()
    }
    write(
        args.output,
        {
            "kind": "frozen_history_rank_postprocessing",
            "original_report_sha256": digest(original),
            "metrics": metrics,
            "role_and_locator_valid": all(r["role_and_locator_valid"] for r in measurements),
            "timings": timings,
            "evidence_tokens": {"median": statistics.median(tokens), "max": max(tokens)},
            "measurements": measurements,
            "note": "Metrics recomputed from persisted rankings and original excerpts, without new inference or policy changes. Total timing is recorded before citation inserts/transaction commit; cached replay is not production end-to-end latency. Human semantic acceptance remains pending.",
        },
    )
    print(json.dumps({"metrics": metrics, "timings": timings}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--evaluation", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    main(p.parse_args())
