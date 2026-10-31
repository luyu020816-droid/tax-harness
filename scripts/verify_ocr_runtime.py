"""Real PDF -> OCR -> leased indexing -> BGE -> citation and failure closure."""

import argparse
import asyncio
import json
from pathlib import Path

from app.config import ROOT, Settings
from app.errors import AppError
from app.identity import Identity
from app.knowledge import Knowledge
from app.knowledge_jobs import KnowledgeJobs
from app.knowledge_retrieval import KnowledgeRetrieval
from app.retrieval_engine import LocalRetrieval
from app.values import digest, uid
from evaluate_retrieval import database, write


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    settings, db = database(Settings(), "development", args.output)
    engine = LocalRetrieval(settings.knowledge_model_directory)
    store = Knowledge(db, settings)
    jobs = KnowledgeJobs(db, settings, engine)
    auth = Identity("U001")
    retrieval = KnowledgeRetrieval(db, settings, engine)

    async def upload(name, document=None):
        revision = store.get(auth, document)["revision"] if document else None
        row = store.upload(
            auth,
            name=name,
            content=(args.fixtures / name).read_bytes(),
            company_id="C001",
            title="合成资料对外使用与留存制度",
            request_id=uid(),
            document_id=document,
            expected_revision=revision,
        )
        for _ in range(1500):
            status = store.job(auth, row["job_id"])
            if status["state"] in ("done", "failed", "blocked", "cancelled"):
                write(args.output / (name + ".job.json"), status)
                return row, status
            await asyncio.sleep(0.5)
        raise AssertionError("OCR job exceeded the bounded deadline")

    records = []
    await jobs.start()
    try:
        good, state = await upload("mixed.pdf")
        assert state["state"] == "done", state
        with db.transaction(readonly=True) as conn:
            blocks = conn.execute(
                "SELECT * FROM knowledge_blocks WHERE version_id=%s ORDER BY ordinal",
                (good["version_id"],),
            ).fetchall()
        write(args.output / "blocks.json", blocks)
        all_text = "\n".join(b["content"] for b in blocks)
        for expected in [
            "不得外发内部资料",
            "经书面批准",
            "脱敏汇总表",
            "不允许公开客户手机号",
            "保留期限(年)",
            "不得提前销毁",
        ]:
            assert expected in all_text, expected
        assert {b["locator"]["page"] for b in blocks} == {1, 2, 3}
        assert any(b["locator"].get("extraction") == "native" for b in blocks)
        assert any(
            b["locator"].get("extraction") == "ocr" and b["locator"].get("bbox") for b in blocks
        )
        page_two = [b for b in blocks if b["locator"]["page"] == 2]
        columns = [
            b
            for b in page_two
            if b["locator"].get("reading_order") == "two_columns"
            and 200 < b["locator"]["bbox"][1] < 400
        ]
        assert columns, "Two-column order was not resolved"
        left = [i for i, b in enumerate(columns) if b["locator"]["bbox"][2] < 300]
        right = [i for i, b in enumerate(columns) if b["locator"]["bbox"][0] > 300]
        assert left and right and max(left) < min(right), "Columns interleaved"
        answer = retrieval.search(
            auth,
            company_id="C001",
            query="内部资料对外提供需要什么批准，允许哪些例外，能公开客户手机号吗？",
            document_ids=[good["document_id"]],
        )
        write(args.output / "retrieval.json", answer)
        assert answer["evidence"], answer
        text = "\n".join(e["excerpt"] for e in answer["evidence"])
        for expected in ["不得外发内部资料", "脱敏汇总表", "不允许公开客户手机号"]:
            assert expected in text, expected
        for evidence in answer["evidence"]:
            assert retrieval.read(auth, evidence["citation_id"])["excerpt"] == evidence["excerpt"]
        records.append({"id": "mixed_ocr_retrieval_citation", "pass": True})
        print("mixed OCR / table / columns / cross-page / citation PASS", flush=True)
        for name, code in [
            ("low.pdf", "OCR_LOW_QUALITY"),
            ("missing.pdf", "DOCUMENT_PARSE_FAILED"),
            ("corrupt.pdf", "DOCUMENT_PARSE_FAILED"),
        ]:
            row, status = await upload(name, good["document_id"])
            assert status["state"] in ("failed", "blocked"), status
            assert status["error"]["code"] == code, status
            assert store.get(auth, good["document_id"])["active_version_id"] == good["version_id"]
            records.append({"id": name, "pass": True, "code": code})
            print(name, "failed safely; old version preserved", flush=True)
        try:
            store.get(Identity("U002"), good["document_id"])
            raise AssertionError("Private OCR document leaked")
        except AppError as error:
            assert error.code in ("ACCESS_DENIED", "RESOURCE_NOT_FOUND")
        records.append({"id": "private_scope", "pass": True})
        current = store.get(auth, good["document_id"])
        store.change(
            auth,
            good["document_id"],
            action="disable",
            expected_revision=current["revision"],
            request_id=uid(),
        )
        try:
            retrieval.read(auth, answer["evidence"][0]["citation_id"])
            raise AssertionError("Disabled OCR evidence revived")
        except AppError as error:
            assert error.code == "SOURCE_INVALIDATED"
        records.append({"id": "disable_invalidates_citation", "pass": True})
        write(
            args.output / "report.json",
            {
                "records": records,
                "passed": len(records),
                "code_fingerprint": digest(
                    {
                        p.name: p.read_text(encoding="utf-8")
                        for p in sorted((ROOT / "backend/app").glob("*.py"))
                    }
                ),
                "profile_id": engine.profile()[0],
                "ocr_model_lock_sha256": digest(
                    json.loads((ROOT / "backend/ocr-models.lock.json").read_text(encoding="utf-8"))
                ),
                "human_ocr_fidelity_review": "pending",
                "scope": "Implementer-created public synthetic PDF fixtures; not arbitrary scan accuracy.",
            },
        )
        print("OCR runtime closure PASS", flush=True)
    finally:
        await jobs.stop()
        engine.close()
        db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--fixtures", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(run(p.parse_args()))
