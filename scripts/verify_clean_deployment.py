"""Source-only extraction, new Python environments and an independent PG cluster.

Public model/binary asset caches are explicitly imported and checked. No database
data directory, environment, node_modules or virtualenv is copied from development.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
from app.config import ROOT, Settings
from app.values import digest, uid
from evaluate_retrieval import write


def main(args):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    target = (
        args.prepared_directory.resolve() / "source" if args.prepared_directory else out / "source"
    )
    log = (out / "commands.log").open("wb")

    def run(command, *, cwd=target, timeout=1800, env=None):
        subprocess.run(
            [str(v) for v in command],
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    run([sys.executable, ROOT / "scripts/package_source.py", out / "source.zip"], cwd=ROOT)
    with zipfile.ZipFile(out / "source.zip") as archive:
        assert all(
            not n.startswith((".local/", ".tools/", ".venv/")) and Path(n).name != ".env"
            for n in archive.namelist()
        )
        archive.extractall(target)
    if not args.prepared_directory:
        assert not (target / ".env").exists()
        run([sys.executable, "-m", "venv", ".venv"])
    python = target / ".venv/Scripts/python.exe"
    if not args.prepared_directory:
        run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                "scripts/setup-knowledge.ps1",
                "-SkipModelDownload",
            ]
        )
    # Explicit offline asset import, with per-file transport fingerprints.
    assets = {}
    for relative in [".tools/pgsql-knowledge", ".local/retrieval-models", ".local/ocr-models"]:
        source = ROOT / relative
        destination = target / relative
        assert source.is_dir(), relative
        if not args.prepared_directory:
            shutil.copytree(source, destination, copy_function=os.link)
        hashes = {
            p.relative_to(source).as_posix(): hashlib.file_digest(
                p.open("rb"), "sha256"
            ).hexdigest()
            for p in sorted(source.rglob("*"))
            if p.is_file()
        }
        for name, checksum in hashes.items():
            with (destination / name).open("rb") as stream:
                assert hashlib.file_digest(stream, "sha256").hexdigest() == checksum
        assets[relative] = {
            "files": len(hashes),
            "sha256": digest(hashes),
            "transfer": "read-only public asset cache import via hardlinks",
        }
    write(out / "asset-import.json", assets)
    run([python, "-m", "app.cli", "configure"])
    envpath = target / ".env"
    text = envpath.read_text(encoding="utf-8").replace(":54337/", ":54339/")
    # Use the existing authorized model only through this private local file.
    s = Settings()
    for key, value in [
        ("MODEL_BASE_URL", s.model_base_url),
        ("MODEL_ID", s.model_id),
        ("MODEL_API_KEY", s.model_api_key),
        ("MODEL_EXTRA_BODY", s.model_extra_body),
        ("MODEL_CONTEXT_LIMIT", str(s.model_context_limit)),
    ]:
        text = "\n".join(
            key + "=" + json.dumps(value) if line.startswith(key + "=") else line
            for line in text.split("\n")
        )
    envpath.write_text(text, encoding="utf-8")
    run([python, "scripts/native_postgres.py", "start"])
    server = None
    try:
        run([python, "-m", "app.cli", "init"])
        run(["cmd", "/c", "npm", "ci"], cwd=target / "frontend")
        run(["cmd", "/c", "npm", "run", "build"], cwd=target / "frontend")
        run(
            [
                python,
                "-c",
                "from app.config import Settings; from app.retrieval_engine import LocalRetrieval; from app.ocr_status import options; s=Settings(); e=LocalRetrieval(s.knowledge_model_directory); print(e.profile()[0]); assert options(s,validate=True); e.close()",
            ]
        )

        def launch():
            return subprocess.Popen(
                [
                    str(python),
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8106",
                ],
                cwd=target,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

        server = launch()
        client = httpx.Client(base_url="http://127.0.0.1:8106", timeout=30)

        def ready():
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    r = client.get("/api/v1/ready")
                    if r.status_code == 200:
                        return r.json()
                except httpx.TransportError:
                    pass
                assert server.poll() is None, "Clean server exited"
                time.sleep(0.5)
            raise AssertionError("Clean startup timed out")

        initial = ready()
        write(out / "ready.json", initial)
        assert client.get("/").status_code == 200
        client.post("/api/v1/demo/login", json={"user_id": "U001"}).raise_for_status()
        response = client.post("/api/v1/sessions", json={"selected_company_id": "C001"})
        response.raise_for_status()
        session = response.json()
        turns = []
        for question in ["查询甲企业2026年9月发票金额合计", "把甲企业F002用途填为办公资料整理"]:
            accepted = client.post(
                "/api/v1/sessions/" + session["session_id"] + "/turns",
                json={"client_request_id": uid(), "text": question},
            )
            accepted.raise_for_status()
            turn_id = accepted.json()["turn_id"]
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                result = client.get("/api/v1/turns/" + turn_id).json()
                if result["status"] in ("completed", "failed", "interrupted", "waiting_user"):
                    break
                time.sleep(0.5)
            assert result["status"] == "completed", result
            turns.append(result)
        write(out / "turns.json", turns)
        assert any(c.get("invoice_total") == "9622.06" for c in turns[0]["final_cards"])
        # Ordinary restart and explicit migration must preserve saved purpose.
        server.terminate()
        server.wait(timeout=20)
        server = None
        run([python, "-m", "app.cli", "init"])
        run(
            [
                python,
                "-c",
                'from app.config import Settings;from app.db import Database;d=Database(Settings().database_url);d.open();\nwith d.transaction(readonly=True) as c:\n r=c.execute("SELECT purpose,revision FROM invoices WHERE company_id=\'C001\' AND invoice_id=\'F002\'").fetchone();assert r=={"purpose":"办公资料整理","revision":2},r\nd.close()',
            ]
        )
        server = launch()
        ready()
        run([python, ROOT / "scripts/verify_clean_runtime.py", "--output", out / "runtime.json"])
        write(
            out / "report.json",
            {
                "pass": True,
                "platform": "Windows x64 / Python 3.12 / native PostgreSQL 16",
                "source_archive_sha256": hashlib.sha256(
                    (out / "source.zip").read_bytes()
                ).hexdigest(),
                "assets": assets,
                "clean_directory": str(target),
                "fresh_venvs": 2,
                "resumed_bootstrap": str(args.prepared_directory)
                if args.prepared_directory
                else None,
                "database_port": 54339,
                "http_port": 8106,
                "real_model_turns": len(turns),
                "scope": "Clean directory on same Windows machine; public binary/weight cache imported; not another OS or Docker.",
            },
        )
        print("Clean deployment PASS", flush=True)
    finally:
        if server and server.poll() is None:
            server.terminate()
            server.wait(timeout=20)
        run([python, "scripts/native_postgres.py", "stop"], timeout=60)
        log.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--prepared-directory", type=Path)
    main(p.parse_args())
