"""Shared isolation and artifacts for the four official advanced challenges."""

import json
from pathlib import Path

import psycopg
from app.cli import initialize
from app.db import Database
from app.values import digest, safe_json
from psycopg import sql
from psycopg.conninfo import make_conninfo


def write(path, value):
    Path(path).write_text(
        json.dumps(safe_json(value), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def database(settings, directory):
    name = "tax_harness_bonus_" + digest(str(directory.resolve()))[:12]
    with psycopg.connect(
        make_conninfo(settings.admin_database_url, dbname="postgres"), autocommit=True
    ) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            raise RuntimeError(
                "Output/database already exists; choose a new directory. No database reset."
            )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings = settings.model_copy(
        update={
            "database_url": make_conninfo(settings.database_url, dbname=name),
            "admin_database_url": make_conninfo(settings.admin_database_url, dbname=name),
        }
    )
    initialize(settings)
    db = Database(settings.database_url)
    db.open()
    return settings, db
