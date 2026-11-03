"""Backup, migrate only 010, verify existing business fields. Never imports data."""

import argparse
import json
import os
import subprocess
from pathlib import Path

import psycopg
from app.cli import _grant_runtime
from app.config import ROOT, Settings
from app.values import digest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

TABLES = (
    "invoices",
    "bank_entries",
    "rule_versions",
    "preferences",
    "operations",
    "tasks",
    "users",
    "user_company_grants",
)


def business(conn):
    return {
        t: digest(
            sorted(
                digest(r)
                for r in conn.execute(
                    sql.SQL("SELECT * FROM {}").format(sql.Identifier(t))
                ).fetchall()
            )
        )
        for t in TABLES
    }


def run(output):
    settings = Settings()
    output.mkdir(parents=True, exist_ok=False)
    info = conninfo_to_dict(settings.admin_database_url)
    binaries = ROOT / ".tools/pgsql-knowledge/bin"
    if not (binaries / "pg_dump.exe").exists():
        binaries = ROOT / ".tools/pgsql/bin"
    archive = output / "before-upgrade.dump"
    subprocess.run(
        [
            str(binaries / "pg_dump.exe"),
            "-h",
            info.get("host", "127.0.0.1"),
            "-p",
            info.get("port", "5432"),
            "-U",
            info["user"],
            "-d",
            info["dbname"],
            "-Fc",
            "-f",
            str(archive),
        ],
        env={**os.environ, "PGPASSWORD": info["password"]},
        check=True,
        capture_output=True,
        timeout=120,
    )
    subprocess.run(
        [str(binaries / "pg_restore.exe"), "--list", str(archive)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        before = business(conn)
        versions = {
            r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        if not {1, 2, 3, 4, 5, 7, 8} <= versions:
            raise RuntimeError("Existing base schema is incompatible; no initialization attempted")
        if 10 not in versions:
            conn.execute(
                (ROOT / "backend/migrations/010_run_insights.sql").read_text(encoding="utf-8")
            )
        _grant_runtime(conn, settings)
        after = business(conn)
        if before != after:
            raise RuntimeError("Business fingerprint changed; migration transaction rolled back")
    report = {
        "migration": 10,
        "business_unchanged": True,
        "business_hashes": after,
        "archive_bytes": archive.stat().st_size,
        "archive_directory_verified": True,
        "full_restore_tested": False,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"migration": 10, "business_unchanged": True, "archive_directory_verified": True}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output.resolve())
