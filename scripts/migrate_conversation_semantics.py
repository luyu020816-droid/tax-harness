"""Offline, backed-up upgrade; never initializes or reimports a dataset."""

import argparse
import json
from pathlib import Path

import psycopg
from app.cli import _grant_ingest, _grant_runtime
from app.config import ROOT, Settings
from app.recovery import _pg_command, _snapshot, maintenance_file
from app.values import uid
from migrate_manual_knowledge import snapshot
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row


def run(output):
    config = Settings()
    output.mkdir(parents=True, exist_ok=False)
    maintenance = maintenance_file(config.database_url)
    maintenance.parent.mkdir(parents=True, exist_ok=True)
    with maintenance.open("x", encoding="utf-8") as stream:
        json.dump(
            {"kind": "conversation_semantic_upgrade", "output": str(output)}, stream
        )
    with psycopg.connect(
        config.admin_database_url, row_factory=dict_row, connect_timeout=5
    ) as conn:
        if conn.execute(
            "SELECT 1 FROM turns WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone():
            raise RuntimeError("Complete/stop active turns before upgrading")
        if conn.execute(
            "SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                "Stop other connections to this database before upgrading"
            )
        tables = [
            r["tablename"]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'schema_migrations' ORDER BY tablename"
            ).fetchall()
        ]
        columns = {
            table: [
                r["column_name"]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                    (table,),
                )
            ]
            for table in tables
        }
        before = snapshot(conn, columns)
        full_before = _snapshot(conn)
    archive = output / "before-semantic-upgrade.dump"
    _pg_command(
        "pg_dump",
        config.admin_database_url,
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--file",
        str(archive),
    )
    _pg_command("pg_restore", config.admin_database_url, "--list", str(archive))
    restore_name = "tax_harness_restore_" + uid().replace("-", "")[:12]
    with psycopg.connect(
        make_conninfo(config.admin_database_url, dbname="postgres"),
        autocommit=True,
        connect_timeout=5,
    ) as conn:
        if conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (restore_name,)
        ).fetchone():
            raise RuntimeError("Restore target already exists; no replacement allowed")
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restore_name)))
    restore_dsn = make_conninfo(config.admin_database_url, dbname=restore_name)
    _pg_command("pg_restore", restore_dsn, "--exit-on-error", str(archive))
    with psycopg.connect(restore_dsn, row_factory=dict_row, connect_timeout=5) as conn:
        restored = _snapshot(conn)
    if restored != full_before:
        raise RuntimeError(
            "Backup restoration fingerprint differs; original database not migrated"
        )
    with psycopg.connect(
        config.admin_database_url, row_factory=dict_row, connect_timeout=5
    ) as conn:
        if conn.execute(
            "SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                "Connections appeared after backup; no migration performed"
            )
        versions = {
            r["version"] for r in conn.execute("SELECT version FROM schema_migrations")
        }
        if 14 not in versions:
            raise RuntimeError(
                "Conversation migration 014 required; no initialization attempted"
            )
        if 15 not in versions:
            conn.execute(
                (ROOT / "backend/migrations/015_conversation_semantics.sql").read_text(
                    encoding="utf-8"
                )
            )
        _grant_runtime(conn, config)
        if config.ingest_database_url:
            _grant_ingest(conn, config)
        after = snapshot(conn, columns)
        if before != after:
            raise RuntimeError(
                "Existing protected columns changed; migration rolled back"
            )
    report = {
        "schema": 15,
        "original_fields_unchanged": True,
        "archive_bytes": archive.stat().st_size,
        "archive_directory_verified": True,
        "full_restore_tested": True,
        "restore_database": restore_name,
        "restored_tables": len(restored["tables"]),
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    maintenance.unlink()
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output.resolve())
