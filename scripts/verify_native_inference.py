"""Exercise actual local model switches; retries count as failures, not silent success."""

import argparse
import json
import platform
import time
from pathlib import Path

from app.config import Settings
from app.retrieval_engine import LocalRetrieval


def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    engine = LocalRetrieval(Settings().knowledge_model_directory)
    rows = []
    try:
        for cycle in range(args.cycles):
            started = time.monotonic()
            text = f"第{cycle}次核验：此前演示使用备用电脑，必须关闭消息弹窗。"
            before = engine.process.restarts if engine.process else 0
            engine.embed([text])
            engine.rerank(
                "此前演示准备要求是什么？",
                [text]
                + [
                    f"第{i}份档案：" + "茶水间的门在走廊右侧。" * (i + 1)
                    for i in range(args.passages - 1)
                ],
            )
            row = {
                "cycle": cycle,
                "seconds": round(time.monotonic() - started, 3),
                "restarts": engine.process.restarts - before,
            }
            rows.append(row)
            print(json.dumps(row), flush=True)
            (args.output / "report.json").write_text(
                json.dumps(
                    {
                        "rows": rows,
                        "cycles": len(rows),
                        "restarts": sum(r["restarts"] for r in rows),
                        "profile_id": engine.profile()[0],
                        "platform": platform.platform(),
                        "python": platform.python_version(),
                        "note": "Actual inference, no vector/score cache. Zero observed faults is not a root-cause fix or a production SLA.",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        engine.close()
    if any(r["restarts"] for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=8)
    parser.add_argument("--passages", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
