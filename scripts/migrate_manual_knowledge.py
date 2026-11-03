"""Back up and apply migration 011 without reimporting or deleting user data."""

import argparse
import hashlib
import json
from pathlib import Path

import psycopg
from app.cli import _grant_runtime
from app.config import ROOT, Settings
from app.recovery import _pg_command
from app.values import digest
from psycopg import sql
from psycopg.rows import dict_row

TABLES = (
    "invoices",
    "bank_entries",
    "rule_versions",
    "preferences",
    "tasks",
    "operations",
    "knowledge_files",
    "knowledge_documents",
    "knowledge_versions",
    "knowledge_blocks",
    "knowledge_chunks",
)


def snapshot(conn, columns):
    result = {}
    for table, names in columns.items():
        rows = conn.execute(
            sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(",").join(map(sql.Identifier, names)), sql.Identifier(table)
            )
        ).fetchall()
        # Binary originals are hashed, never printed/exported as text.
        result[table] = digest(
            sorted(
                digest(
                    {
                        k: hashlib.sha256(v).hexdigest()
                        if isinstance(v, (bytes, memoryview))
                        else v
                        for k, v in row.items()
                    }
                )
                for row in rows
            )
        )
    return result


def run(output):
    settings = Settings()
    output.mkdir(parents=True, exist_ok=False)
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        if conn.execute(
            "SELECT 1 FROM turns WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                "Active requests exist; stop/complete them before upgrading"
            )
        columns = {
            table: [
                r["column_name"]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                    (table,),
                ).fetchall()
            ]
            for table in TABLES
        }
        before = snapshot(conn, columns)
    archive = output / "before-manual-knowledge.dump"
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
        if 10 not in versions:
            raise RuntimeError("Migration 010 is required; no initialization attempted")
        if 11 not in versions:
            conn.execute(
                (
                    ROOT / "backend/migrations/011_manual_knowledge_sessions.sql"
                ).read_text(encoding="utf-8")
            )
        _grant_runtime(conn, settings)
        after = snapshot(conn, columns)
        if before != after:
            raise RuntimeError(
                "Protected business/document fields changed; rolling back"
            )
    report = {
        "migration": 11,
        "protected_fields_unchanged": True,
        "archive_directory_verified": True,
        "archive_bytes": archive.stat().st_size,
        "full_restore_tested": False,
        "protected_hashes": after,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "protected_hashes"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output.resolve())
