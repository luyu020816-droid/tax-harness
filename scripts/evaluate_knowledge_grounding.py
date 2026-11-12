"""Run frozen synthetic claim-verification cases; never touches the demo database."""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.errors import AppError
from app.knowledge_grounding import Verdicts, validated_indices, verifier_payload
from app.model import HttpModel
from app.values import digest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evals/knowledge-grounding/holdout-v1.json"
OUTPUT = ROOT / ".local/knowledge-grounding-eval-v1.json"


async def main():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    settings = Settings()
    if not settings.model_configured:
        raise SystemExit("请先在本地配置模型；不要将密钥提交到仓库。")
    model = HttpModel(settings)
    semaphore = asyncio.Semaphore(3)
    citation = "00000000-0000-0000-0000-000000000001"

    async def evaluate(case):
        claims = [{"text": case["claim"], "evidence": {citation: case["evidence"]}}]
        payload = verifier_payload(settings, case["question"], claims)
        async with semaphore:
            started = time.monotonic()
            raw = None
            try:
                response = await asyncio.wait_for(model.complete(payload), timeout=15)
                raw = response["message"].get("content") or ""
                parsed = Verdicts.model_validate_json(raw)
                if (
                    len(parsed.verdicts) != 1
                    or parsed.verdicts[0].claim_index != 0
                    or parsed.verdicts[0].status
                    not in {"supported", "unsupported", "uncertain"}
                ):
                    raise ValueError("invalid verdict structure")
                accepted = 0 in validated_indices(claims, raw)
                status = "ok"
                usage = response.get("usage")
            except (
                AppError,
                TimeoutError,
                ValueError,
                KeyError,
                TypeError,
                OSError,
                RuntimeError,
            ) as exc:
                accepted, status, usage = False, type(exc).__name__, None
            return {
                "id": case["id"],
                "expected": case["expected"],
                "accepted": accepted,
                "status": status,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "usage": usage,
                "raw_verdict": raw,
            }

    results = await asyncio.gather(*(evaluate(case) for case in fixture["cases"]))
    report = {
        "fixture_hash": digest(fixture),
        "model": settings.model_id,
        "measured_at": datetime.now(UTC).isoformat(),
        "cases": results,
        "false_accepts": sum(
            r["expected"] == "unsupported" and r["accepted"] for r in results
        ),
        "false_rejections": sum(
            r["expected"] == "supported" and not r["accepted"] for r in results
        ),
        "unavailable": sum(r["status"] != "ok" for r in results),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False
        )
    )
    print(f"Report: {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
