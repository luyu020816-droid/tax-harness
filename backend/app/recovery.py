"""Consistent PostgreSQL backups and a fail-closed restoration gate.

The external checkpoint is a recovery reference, not a replica or a promise of
zero data loss. Restoring an older image cannot silently enable old memories.
"""

import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from app.config import ROOT
from app.errors import AppError
from app.values import canonical, digest, safe_json, uid


def maintenance_file(dsn: str) -> Path:
    name = conninfo_to_dict(dsn).get("dbname", "unconfigured") if dsn else "unconfigured"
    return ROOT / ".local/maintenance" / f"{digest(name)[:16]}.json"


def in_maintenance(dsn: str) -> bool:
    return maintenance_file(dsn).exists()


def _write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(canonical(data), encoding="utf-8")
    temporary.replace(path)


def _snapshot(conn) -> dict:
    vector_type = conn.execute(
        "SELECT oid FROM pg_type WHERE typname='vector' AND typnamespace='public'::regnamespace"
    ).fetchone()
    tables = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    ).fetchall()
    result = {}
    for table in tables:
        name = table["tablename"]
        cursor = conn.execute(sql.SQL("SELECT * FROM public.{}").format(sql.Identifier(name)))
        vector_columns = {
            c.name for c in cursor.description if vector_type and c.type_code == vector_type["oid"]
        }
        rows = cursor.fetchall()
        hashes = sorted(
            digest(
                safe_json(
                    {
                        key: {
                            "binary_sha256": hashlib.sha256(value).hexdigest(),
                            "bytes": len(value),
                        }
                        if isinstance(value, (bytes, memoryview))
                        else _vector_fingerprint(value)
                        if key in vector_columns and value is not None
                        else value
                        for key, value in row.items()
                    }
                )
            )
            for row in rows
        )
        result[name] = {"rows": len(rows), "hash": digest(hashes)}
    return {"tables": result, "fingerprint": digest(result)}


def _vector_fingerprint(value):
    values = json.loads(value) if isinstance(value, str) else list(value)
    if not all(math.isfinite(v) for v in values):
        raise AppError("RECOVERY_VECTOR_INVALID", "向量包含非有限数，不能生成完整恢复回执。", 503)
    normalized = [0.0 if v == 0 else float(v) for v in values]
    packed = struct.pack("!" + str(len(normalized)) + "f", *normalized)
    return {"dimension": len(values), "float32_sha256": hashlib.sha256(packed).hexdigest()}


def checkpoint(dsn: str, path: Path) -> dict:
    if in_maintenance(dsn):
        raise AppError(
            "RECOVERY_REFERENCE_UNCONFIRMED", "维护状态不能生成新的最新回执来覆盖待核对状态。", 409
        )
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        result = {
            "database": conninfo_to_dict(dsn)["dbname"],
            "captured_at": datetime.now(UTC).isoformat(),
            **_snapshot(conn),
        }
    _write(path, result)
    return result


def _pg_command(name, dsn, *args):
    prepared = ROOT / ".tools/pgsql-knowledge"
    binaries = (
        prepared if (prepared / "pgvector-installation.json").is_file() else ROOT / ".tools/pgsql"
    )
    binary = binaries / "bin" / f"{name}.exe"
    command = str(binary) if binary.exists() else shutil.which(name)
    if not command:
        raise AppError("RUNTIME_REQUIRED", f"未找到 {name}，请安装 PostgreSQL 16 客户端。", 503)
    config = conninfo_to_dict(dsn)
    env = {**os.environ, "PGPASSWORD": config.get("password", ""), "PGCONNECT_TIMEOUT": "5"}
    result = subprocess.run(
        [
            command,
            "--host",
            config.get("host", "127.0.0.1"),
            "--port",
            config.get("port", "5432"),
            "--username",
            config["user"],
            "--dbname",
            config["dbname"],
            *args,
        ],
        env=env,
        capture_output=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        # CLI error strings may contain connection details. Keep them out of public output.
        raise AppError(
            "RECOVERY_COMMAND_FAILED",
            f"{name} 未完成，维护模式保持开启。",
            503,
            details={"exit_code": result.returncode},
        )


def backup(dsn: str, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot_id = conn.execute("SELECT pg_export_snapshot() AS id").fetchone()["id"]
        point = conn.execute(
            "SELECT transaction_timestamp() AS at,pg_current_wal_lsn()::text AS lsn"
        ).fetchone()
        manifest = {
            "backup_id": uid(),
            "database": conninfo_to_dict(dsn)["dbname"],
            "recovery_point": safe_json(point),
            **_snapshot(conn),
        }
        _pg_command(
            "pg_dump",
            dsn,
            "--format=custom",
            "--no-owner",
            "--no-acl",
            f"--snapshot={snapshot_id}",
            "--file",
            str(directory / "database.dump"),
        )
    manifest.update(
        completed_at=datetime.now(UTC).isoformat(),
        dump_sha256=hashlib.sha256((directory / "database.dump").read_bytes()).hexdigest(),
    )
    _write(directory / "manifest.json", manifest)
    return manifest


def restore(dsn: str, directory: Path, expected_checkpoint: Path, settings) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    expected = json.loads(expected_checkpoint.read_text(encoding="utf-8"))
    database = conninfo_to_dict(dsn)["dbname"]
    if manifest["database"] != database or expected["database"] != database:
        raise AppError("RECOVERY_SCOPE_MISMATCH", "备份、回执和目标数据库必须相同。", 409)
    if (
        hashlib.sha256((directory / "database.dump").read_bytes()).hexdigest()
        != manifest["dump_sha256"]
    ):
        raise AppError("BACKUP_CORRUPTED", "备份内容校验失败，未执行恢复。", 409)
    gate = maintenance_file(dsn)
    prior = json.loads(gate.read_text(encoding="utf-8")) if gate.exists() else None
    if prior and prior["expected_fingerprint"] != expected["fingerprint"]:
        raise AppError(
            "RECOVERY_REFERENCE_UNCONFIRMED",
            "回执与维护时锁定的最新状态不同，不能更换成较旧回执。",
            409,
        )
    _write(
        gate,
        {
            "state": "maintenance",
            "backup_id": manifest["backup_id"],
            "expected_fingerprint": expected["fingerprint"],
            "reason": "restoration_in_progress",
        },
    )
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        active = conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
        ).fetchone()["n"]
        if active:
            if not prior:
                gate.unlink()
            raise AppError(
                "STOP_SERVER_REQUIRED",
                "请先停止此项目的服务和测试连接再恢复；未执行数据库恢复。",
                409,
            )
        if not prior:
            current = {
                "database": database,
                "captured_at": datetime.now(UTC).isoformat(),
                **_snapshot(conn),
            }
            if current["fingerprint"] != expected["fingerprint"]:
                reference = ROOT / ".local/receipts" / f"pre-restore-{uid()}.json"
                _write(reference, current)
                _write(
                    gate,
                    {
                        "state": "maintenance",
                        "backup_id": manifest["backup_id"],
                        "expected_fingerprint": current["fingerprint"],
                        "reason": "checkpoint_is_stale",
                    },
                )
                raise AppError(
                    "RECOVERY_CHECKPOINT_STALE",
                    "提供的回执落后于现有数据库，未覆盖数据；已保存当前回执并保持维护核对。",
                    409,
                    details={"checkpoint_path": str(reference)},
                )
    _pg_command(
        "pg_restore",
        dsn,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--exit-on-error",
        "--single-transaction",
        str(directory / "database.dump"),
    )
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        from app.cli import _grant_runtime

        _grant_runtime(conn, settings)
        if (
            conn.execute("SELECT profile FROM dataset_metadata WHERE singleton_id=1").fetchone()[
                "profile"
            ]
            == "extended"
            and settings.ingest_database_url
        ):
            from app.cli import _grant_ingest

            _grant_ingest(conn, settings)
        actual = _snapshot(conn)
    if actual["fingerprint"] != manifest["fingerprint"]:
        raise AppError("RESTORE_INCOMPLETE", "恢复结果不匹配备份恢复点，保持维护模式。", 503)
    if actual["fingerprint"] != expected["fingerprint"]:
        _write(
            gate,
            {
                "state": "maintenance",
                "backup_id": manifest["backup_id"],
                "expected_fingerprint": expected["fingerprint"],
                "reason": "later_receipts_missing",
            },
        )
        raise AppError(
            "RECOVERY_POINT_BEHIND",
            "该备份缺少较新的回执、更正或删除，保持维护模式；补齐后才可开放。",
            409,
        )
    gate.unlink()
    return {
        "state": "restored",
        "backup_id": manifest["backup_id"],
        "recovery_point": manifest["recovery_point"],
        "fingerprint": actual["fingerprint"],
    }


def resume(dsn: str, expected_checkpoint: Path) -> dict:
    gate = maintenance_file(dsn)
    if not gate.exists():
        raise AppError("INVALID_ARGUMENT", "数据库当前不在维护状态。", 409)
    expected = json.loads(expected_checkpoint.read_text(encoding="utf-8"))
    stored = json.loads(gate.read_text(encoding="utf-8"))
    if (
        expected.get("database") != conninfo_to_dict(dsn)["dbname"]
        or expected.get("fingerprint") != stored["expected_fingerprint"]
    ):
        raise AppError("RECOVERY_REFERENCE_UNCONFIRMED", "回执不匹配维护时锁定的最新状态。", 409)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
        ).fetchone()["n"]:
            raise AppError("STOP_SERVER_REQUIRED", "先停止应用和测试连接，再核对开放。", 409)
        if _snapshot(conn)["fingerprint"] != expected["fingerprint"]:
            raise AppError("RECOVERY_POINT_BEHIND", "当前内容仍未匹配最新回执，保持维护状态。", 409)
    gate.unlink()
    return {"state": "verified_and_resumed", "fingerprint": expected["fingerprint"]}
