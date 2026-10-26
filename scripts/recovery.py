"""Offline administrative entry point. Never accepts passwords as arguments."""

import argparse
import json
from pathlib import Path

from app import recovery
from app.config import Settings
from app.errors import AppError
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup", "checkpoint", "restore", "resume"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--expect-checkpoint", type=Path)
    args = parser.parse_args()
    settings = Settings()
    dsn = settings.admin_database_url
    if args.test:
        target = conninfo_to_dict(settings.test_database_url).get("dbname")
        if target not in ("tax_harness_test", "tax_harness_extended_test"):
            parser.error("--test requires an explicitly allowed isolated test database")
        dsn = make_conninfo(settings.admin_database_url, dbname=target)
    try:
        if args.action == "backup":
            result = recovery.backup(dsn, args.path)
        elif args.action == "checkpoint":
            result = recovery.checkpoint(dsn, args.path)
        elif args.action == "resume":
            result = recovery.resume(dsn, args.path)
        else:
            if not args.expect_checkpoint:
                parser.error(
                    "restore requires --expect-checkpoint from an independent current-state capture"
                )
            result = recovery.restore(dsn, args.path, args.expect_checkpoint, settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except AppError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
