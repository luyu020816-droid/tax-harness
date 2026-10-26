import argparse
import json
import secrets
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app.config import ROOT, Settings
from app.errors import AppError
from app.importer import import_samples
from app.values import safe_json


def configure(root: Path = ROOT) -> None:
    path = root / ".env"
    if path.exists():
        print("Existing .env kept.")
        return
    admin_password = secrets.token_urlsafe(24)
    runtime_password = secrets.token_urlsafe(24)
    text = (
        f"POSTGRES_PASSWORD={admin_password}\n"
        f"ADMIN_DATABASE_URL=postgresql://tax_admin:{quote(admin_password)}@127.0.0.1:54337/tax_harness\n"
        f"DATABASE_URL=postgresql://tax_app:{quote(runtime_password)}@127.0.0.1:54337/tax_harness\n"
        f"TEST_DATABASE_URL=postgresql://tax_app:{quote(runtime_password)}@127.0.0.1:54337/tax_harness_test\n"
        "MODEL_BASE_URL=\nMODEL_ID=\nMODEL_API_KEY=\nMODEL_CONTEXT_LIMIT=0\n"
        "MODEL_EXTRA_BODY={}\nTOKEN_COUNTER=conservative_utf8\nOUTPUT_RESERVE=2048\n"
        "INPUT_BUDGET=16000\nMAX_MODEL_STEPS=8\nMAX_TOOL_CALLS=16\nTURN_TIMEOUT=180\nDEMO_MODE=true\n"
    )
    path.write_text(text, encoding="utf-8")
    example = text
    for secret in (admin_password, runtime_password):
        example = example.replace(secret, "replace-with-local-password")
    (root / ".env.example").write_text(example, encoding="utf-8")
    print("Local database configuration generated; no model key configured.")


def _grant_runtime(conn, settings: Settings) -> None:
    details = conninfo_to_dict(settings.database_url)
    user = details["user"]
    password = details["password"]
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (user,)).fetchone():
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(user), sql.Literal(password)
            )
        )
    role = sql.Identifier(user)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role))
    tables = [
        "login_sessions",
        "sessions",
        "turns",
        "sources",
        "source_scopes",
        "work_items",
        "source_assessments",
        "source_progress",
        "runs",
        "run_events",
        "context_snapshots",
        "tool_executions",
        "tasks",
        "task_slots",
        "task_revisions",
        "preferences",
        "preference_revisions",
        "memory_dependencies",
        "summaries",
        "history_index",
        "memory_change_ops",
        "result_views",
        "operations",
    ]
    conn.execute(
        sql.SQL("GRANT INSERT,UPDATE ON {} TO {}").format(
            sql.SQL(",").join(map(sql.Identifier, tables)), role
        )
    )
    conn.execute(sql.SQL("GRANT DELETE ON history_index TO {}").format(role))
    conn.execute(sql.SQL("GRANT UPDATE(next_source_seq,memory_epoch) ON users TO {}").format(role))
    conn.execute(sql.SQL("GRANT UPDATE(data_revision) ON company_heads TO {}").format(role))
    conn.execute(sql.SQL("GRANT UPDATE(purpose,note,revision) ON invoices TO {}").format(role))


def initialize(settings: Settings, *, test=False) -> dict:
    admin = settings.admin_database_url
    if not admin:
        raise AppError("CONFIGURATION_REQUIRED", "先运行 configure 生成本地数据库配置。")
    database = "tax_harness_test" if test else conninfo_to_dict(admin)["dbname"]
    if test:
        with psycopg.connect(make_conninfo(admin, dbname="postgres"), autocommit=True) as conn:
            if not conn.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (database,)
            ).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    with psycopg.connect(make_conninfo(admin, dbname=database), row_factory=dict_row) as conn:
        exists = conn.execute("SELECT to_regclass('public.schema_migrations') AS t").fetchone()["t"]
        applied = (
            {
                row["version"]
                for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            if exists
            else set()
        )
        for migration in sorted((ROOT / "backend/migrations").glob("[0-9]*.sql")):
            if int(migration.name.split("_", 1)[0]) not in applied:
                conn.execute(migration.read_text(encoding="utf-8"))
        materials = ROOT / "docs/赛题二：财税Harness初赛材料包-v1.1.0"
        result = import_samples(
            conn, materials / "02_示例数据.xlsx", materials / "03_业务规则.docx"
        )
        _grant_runtime(conn, settings)
        return {"database": database, "import": result}


def rules_command(args, settings: Settings):
    from app import rules

    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        if args.rule_action == "list":
            return conn.execute(
                "SELECT r.*,array_agg(s.company_id ORDER BY s.company_id) AS companies FROM rule_versions r JOIN rule_scopes s USING(rule_version_id) GROUP BY r.rule_version_id ORDER BY r.rule_code,r.business_version"
            ).fetchall()
        if args.rule_action == "show":
            return conn.execute(
                "SELECT * FROM rule_versions WHERE rule_version_id=%s", (args.version_id,)
            ).fetchone()
        if args.rule_action in ("create-version", "import"):
            return rules.create_version(
                conn, json.loads(Path(args.file).read_text(encoding="utf-8"))
            )
        if args.rule_action == "activate":
            return rules.activate(conn, args.version_id, args.replaces)
        return rules.disable(conn, args.version_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tax Harness local administration")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("configure")
    init = subs.add_parser("init")
    init.add_argument("--test", action="store_true")
    r = subs.add_parser("rules").add_subparsers(dest="rule_action", required=True)
    r.add_parser("list")
    for name in ("show", "disable", "activate"):
        action = r.add_parser(name)
        action.add_argument("version_id")
        if name == "activate":
            action.add_argument("--replaces")
    for name in ("create-version", "import"):
        r.add_parser(name).add_argument("file")
    args = parser.parse_args()
    if args.command == "configure":
        configure()
        return
    try:
        result = (
            initialize(Settings(), test=args.test)
            if args.command == "init"
            else rules_command(args, Settings())
        )
        print(json.dumps(safe_json(result), ensure_ascii=False, indent=2))
    except AppError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
