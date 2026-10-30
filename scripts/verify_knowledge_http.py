"""Real PDF -> persistent job -> local retrieval -> live model -> protected citation.

Uses synthetic data in the existing local preview; never resets business data.
"""

import argparse
import hashlib
import json
import time
from io import BytesIO
from urllib.parse import urlparse

import httpx
from app.config import ROOT, Settings
from app.db import Database
from app.values import digest, safe_json, uid
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 40 780 Td (Travel lodging expenses require prior approval from the department manager.) Tj 0 -20 Td (Urgent travel still requires approval. Never skip the approval process.) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def wait_for(client, path, accepted, limit=180):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        response = client.get(path)
        response.raise_for_status()
        data = response.json()
        if data.get("status", data.get("state")) in accepted:
            return data
        time.sleep(0.5)
    raise TimeoutError("The real HTTP workflow did not complete in time")


def main(base_url):
    if urlparse(base_url).hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("Only the local preview is supported")
    settings = Settings()
    content = pdf()
    with httpx.Client(base_url=base_url, timeout=180) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"}).raise_for_status()
        response = client.post(
            "/api/v1/knowledge/uploads",
            data={"company_id": "C001", "title": "文本PDF审批联调样例", "request_id": uid()},
            files={"file": ("approval.pdf", content, "application/pdf")},
        )
        assert response.status_code == 202, response.status_code
        created = response.json()
        job = wait_for(
            client, "/api/v1/knowledge/jobs/" + created["job_id"], {"done", "failed", "blocked"}
        )
        assert job["state"] == "done", job
        print("PDF indexed with real local embedding", flush=True)
        search = client.post(
            "/api/v1/retrieval/search",
            json={
                "company_id": "C001",
                "query": "紧急出差住宿可以省略审批吗？",
                "document_ids": [created["document_id"]],
            },
        ).json()
        assert search["retrieval_mode"] == "hybrid" and search["evidence"], search
        session = client.post("/api/v1/sessions", json={"selected_company_id": "C001"}).json()
        turn = client.post(
            "/api/v1/sessions/" + session["session_id"] + "/turns",
            json={
                "client_request_id": uid(),
                "text": "根据这份PDF，紧急出差的住宿费可以跳过事先批准吗？请引用原文依据。",
                "knowledge_upload_ids": [created["file_id"]],
            },
        ).json()
        result = wait_for(
            client,
            "/api/v1/turns/" + turn["turn_id"],
            {"completed", "waiting_user", "failed", "interrupted"},
            limit=180,
        )
        assert result["status"] == "completed", result
        cards = [c for c in result["final_cards"] if c["kind"] == "knowledge_quote"]
        assert cards, result
        evidence = client.get("/api/v1/evidence/" + cards[0]["citation_id"]).json()
        assert (
            evidence["document_id"] == created["document_id"]
            and evidence["locator"][0]["page"] == 1
        )
        assert "Never skip the approval process." in evidence["excerpt"]
        original = client.get("/api/v1/knowledge/documents/" + created["document_id"] + "/file")
        assert (
            original.status_code == 200
            and hashlib.sha256(original.content).digest() == hashlib.sha256(content).digest()
        )
        client.post("/api/v1/demo/login", json={"user_id": "U002"}).raise_for_status()
        assert (
            client.get("/api/v1/knowledge/documents/" + created["document_id"]).status_code == 404
        )
        database = Database(settings.database_url)
        database.open()
        try:
            with database.transaction(readonly=True) as conn:
                writes = conn.execute(
                    "SELECT count(*) AS n FROM operations o JOIN sources s ON o.source_id=s.source_id WHERE s.turn_id=%s",
                    (turn["turn_id"],),
                ).fetchone()["n"]
                tools = conn.execute(
                    "SELECT name,state FROM tool_executions WHERE run_id=ANY(SELECT run_id FROM runs WHERE turn_id=%s) ORDER BY tool_call_id",
                    (turn["turn_id"],),
                ).fetchall()
            assert writes == 0
        finally:
            database.close()
        record = {
            "kind": "real_http_pdf_and_model",
            "model_id": settings.model_id,
            "upload": created,
            "job": job,
            "search": search,
            "turn": result,
            "evidence": evidence,
            "tools": tools,
            "invoice_writes": writes,
            "code_fingerprint": digest(
                {
                    p.name: p.read_text(encoding="utf-8")
                    for p in sorted((ROOT / "backend/app").glob("*.py"))
                }
            ),
        }
        path = ROOT / ".local/evidence/knowledge-real-pdf-http.json"
        path.write_text(
            json.dumps(safe_json(record), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            "PASS real PDF + DeepSeek + page citation + original hash + no invoice writes",
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8105")
    main(parser.parse_args().base_url)
