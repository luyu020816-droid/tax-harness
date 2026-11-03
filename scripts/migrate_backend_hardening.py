"""Backup and incrementally apply security metadata without reimporting data."""

import argparse
import json
from pathlib import Path

import psycopg
from app.cli import _grant_runtime
from app.config import ROOT, Settings
from app.recovery import _pg_command
from migrate_manual_knowledge import TABLES, snapshot
from psycopg.rows import dict_row


def run(output):
    settings = Settings()
    output.mkdir(parents=True, exist_ok=False)
    protected = (
        *TABLES,
        "users",
        "user_company_grants",
        "login_sessions",
        "sessions",
        "turns",
        "sources",
        "memory_change_ops",
    )
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        if conn.execute(
            "SELECT 1 FROM turns WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                "Active requests exist; complete/stop them before upgrade"
            )
        columns = {
            table: [
                r["column_name"]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                    (table,),
                ).fetchall()
            ]
            for table in protected
        }
        before = snapshot(conn, columns)
    archive = output / "before-backend-hardening.dump"
    _pg_command(
        "pg_dump",
        settings.admin_database_url,
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--file",
        str(archive),
    )
    _pg_command("pg_restore", settings.admin_database_url, "--list", str(archive))
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        versions = {
            r["version"] for r in conn.execute("SELECT version FROM schema_migrations")
        }
        if 11 not in versions:
            raise RuntimeError("Migration 011 required; no initialization attempted")
        if 12 not in versions:
            conn.execute(
                (ROOT / "backend/migrations/012_backend_hardening.sql").read_text(
                    encoding="utf-8"
                )
            )
        _grant_runtime(conn, settings)
        after = snapshot(conn, columns)
        if before != after:
            raise RuntimeError(
                "Protected fields changed; upgrade transaction rolled back"
            )
    report = {
        "migration": 12,
        "protected_fields_unchanged": True,
        "archive_directory_verified": True,
        "full_restore_tested": False,
        "archive_bytes": archive.stat().st_size,
        "protected_hashes": after,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "protected_hashes"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output.resolve())
