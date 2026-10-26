"""Run a project-local PostgreSQL cluster without installing a Windows service."""

import argparse
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, timeout=40):
    return subprocess.run(
        command, check=True, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    config = dotenv_values(ROOT / ".env")
    admin = config.get("ADMIN_DATABASE_URL")
    if not admin:
        raise SystemExit("Run python -m app.cli configure first.")
    parsed = urlparse(admin)
    port = parsed.port or 54337
    binaries = ROOT / ".tools/pgsql/bin"
    data = ROOT / ".local/postgres/data"
    data.parent.mkdir(parents=True, exist_ok=True)
    ctl = str(binaries / "pg_ctl.exe")
    if not Path(ctl).exists():
        raise SystemExit(
            "Extract the official PostgreSQL 16 runtime into .tools/pgsql first."
        )
    if args.action != "start":
        run(
            [
                ctl,
                "-D",
                str(data),
                args.action,
                *(["-m", "fast"] if args.action == "stop" else []),
            ]
        )
        return
    if not (data / "PG_VERSION").exists():
        password_file = data.parent / "init-password.tmp"
        password_file.write_text(unquote(parsed.password or ""), encoding="utf-8")
        try:
            env = {**os.environ, "TZ": "UTC"}
            subprocess.run(
                [
                    str(binaries / "initdb.exe"),
                    "-D",
                    str(data),
                    "-U",
                    parsed.username,
                    f"--pwfile={password_file}",
                    "--auth-host=scram-sha-256",
                    "--auth-local=scram-sha-256",
                    "--encoding=UTF8",
                    "--locale=C",
                ],
                env=env,
                check=True,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        finally:
            password_file.unlink(missing_ok=True)
    status = subprocess.run(
        [ctl, "-D", str(data), "status"],
        capture_output=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if status.returncode:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                raise SystemExit(
                    f"Port {port} is occupied; no other server was stopped."
                )
        except (ConnectionRefusedError, TimeoutError, OSError):
            pass
        run(
            [
                ctl,
                "-D",
                str(data),
                "-l",
                str(data.parent / "server.log"),
                "-o",
                f"-p {port} -h 127.0.0.1",
                "-w",
                "start",
            ]
        )
    with psycopg.connect(
        make_conninfo(admin, dbname="postgres"), autocommit=True
    ) as conn:
        name = parsed.path.removeprefix("/")
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (name,)
        ).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    print(f"Project PostgreSQL is ready on 127.0.0.1:{port}.")


if __name__ == "__main__":
    main()
