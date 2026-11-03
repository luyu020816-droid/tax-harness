"""Explicit local admin import. No model calls, business writes or remote URLs."""

import argparse
import json
from pathlib import Path

import psycopg
from app.config import Settings
from app.insight_evaluations import import_report
from psycopg.rows import dict_row

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--company", action="append", default=[])
    args = parser.parse_args()
    if args.file.stat().st_size > 10 * 1024 * 1024:
        raise SystemExit("Report exceeds 10MiB")
    report = json.loads(args.file.read_text(encoding="utf-8"))
    with psycopg.connect(Settings().admin_database_url, row_factory=dict_row) as conn:
        result = import_report(conn, args.owner, args.company, report)
    print("Imported evaluation", result)
