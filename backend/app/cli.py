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
        "INGEST_DATABASE_URL=\nMODEL_EXTRA_BODY={}\nTOKEN_COUNTER=conservative_utf8\nOUTPUT_RESERVE=2048\n"
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
        "file_uploads",
        "invoice_import_batches",
        "invoice_import_rows",
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
        "history_jobs",
        "history_passages",
        "history_queries",
        "history_evidence",
        "memory_change_ops",
        "result_views",
        "operations",
        "scope_bindings",
        "write_plans",
        "knowledge_files",
        "knowledge_documents",
        "knowledge_versions",
        "knowledge_blocks",
        "knowledge_chunks",
        "retrieval_profiles",
        "retrieval_jobs",
        "retrieval_queries",
        "retrieval_evidence",
        "rule_knowledge_refs",
    ]
    conn.execute(
        sql.SQL("GRANT INSERT,UPDATE ON {} TO {}").format(
            sql.SQL(",").join(map(sql.Identifier, tables)), role
        )
    )
    conn.execute(sql.SQL("GRANT DELETE ON history_index TO {}").format(role))
    conn.execute(sql.SQL("GRANT DELETE ON history_passages TO {}").format(role))
    conn.execute(sql.SQL("GRANT DELETE ON invoice_import_rows TO {}").format(role))
    conn.execute(sql.SQL("REVOKE INSERT,UPDATE ON rule_knowledge_refs FROM {}").format(role))
    if conn.execute("SELECT to_regclass('public.retrieval_embeddings') AS t").fetchone()["t"]:
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON retrieval_embeddings TO {}").format(role))
    if conn.execute("SELECT to_regclass('public.history_embeddings') AS t").fetchone()["t"]:
        conn.execute(sql.SQL("GRANT INSERT,UPDATE,DELETE ON history_embeddings TO {}").format(role))
    conn.execute(sql.SQL("GRANT UPDATE(next_source_seq,memory_epoch) ON users TO {}").format(role))
    conn.execute(sql.SQL("GRANT UPDATE(data_revision) ON company_heads TO {}").format(role))
    conn.execute(sql.SQL("GRANT UPDATE(purpose,note,revision) ON invoices TO {}").format(role))


def _grant_ingest(conn, settings):
    if not settings.ingest_database_url:
        return
    if (
        conn.execute("SELECT profile FROM dataset_metadata WHERE singleton_id=1").fetchone()[
            "profile"
        ]
        != "extended"
    ):
        raise AppError("IMPORT_DISABLED", "专用入库角色只能在扩展库授权。", 403)
    details = conninfo_to_dict(settings.ingest_database_url)
    role = sql.Identifier(details["user"])
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (details["user"],)).fetchone():
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                role, sql.Literal(details["password"])
            )
        )
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    reads = [
        "dataset_metadata",
        "invoices",
        "users",
        "companies",
        "user_company_grants",
        "company_heads",
        "sessions",
        "file_uploads",
        "invoice_import_batches",
        "invoice_import_rows",
        "memory_change_ops",
        "sources",
        "source_assessments",
        "source_progress",
        "summaries",
    ]
    conn.execute(
        sql.SQL("GRANT SELECT ON {} TO {}").format(
            sql.SQL(",").join(map(sql.Identifier, reads)), role
        )
    )
    conn.execute(
        sql.SQL(
            "GRANT INSERT ON invoices,sources,source_scopes,source_assessments,source_progress,history_index,history_jobs,memory_change_ops TO {}"
        ).format(role)
    )
    conn.execute(
        sql.SQL(
            "GRANT UPDATE ON invoice_import_batches,source_assessments,source_progress,summaries TO {}"
        ).format(role)
    )
    conn.execute(sql.SQL("GRANT UPDATE(next_source_seq,memory_epoch) ON users TO {}").format(role))
    conn.execute(sql.SQL("GRANT UPDATE(data_revision) ON company_heads TO {}").format(role))
    conn.execute(sql.SQL("GRANT UPDATE ON memory_change_ops TO {}").format(role))


def initialize(settings: Settings, *, test=False, extended=False) -> dict:
    admin = settings.admin_database_url
    if not admin:
        raise AppError("CONFIGURATION_REQUIRED", "先运行 configure 生成本地数据库配置。")
    database = (
        conninfo_to_dict(settings.test_database_url)["dbname"]
        if test
        else conninfo_to_dict(admin)["dbname"]
    )
    if test and database not in ("tax_harness_test", "tax_harness_extended_test"):
        raise AppError("INVALID_ARGUMENT", "只允许初始化命名隔离测试库。")
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
            if (
                migration.name.startswith(("006_", "009_"))
                and not conn.execute(
                    "SELECT 1 FROM pg_available_extensions WHERE name='vector'"
                ).fetchone()
            ):
                continue
            if int(migration.name.split("_", 1)[0]) not in applied:
                conn.execute(migration.read_text(encoding="utf-8"))
        materials = ROOT / "docs/赛题二：财税Harness初赛材料包-v1.1.0"
        result = import_samples(
            conn, materials / "02_示例数据.xlsx", materials / "03_业务规则.docx"
        )
        if extended:
            if database not in ("tax_harness_extended", "tax_harness_extended_test"):
                raise AppError("IMPORT_DISABLED", "不能将比赛库改为扩展模式。", 403)
            conn.execute("UPDATE dataset_metadata SET profile='extended' WHERE singleton_id=1")
            conn.execute(
                "UPDATE user_company_grants SET can_import_invoices=true WHERE user_id='U001'"
            )
        _grant_runtime(conn, settings)
        if settings.ingest_database_url:
            _grant_ingest(conn, settings)
        return {"database": database, "import": result}


def initialize_extended(settings):
    from dotenv import dotenv_values

    path = ROOT / ".env.extended"
    if path.exists():
        raise AppError(
            "CONFIGURATION_EXISTS", ".env.extended 已存在，未覆盖配置或已有数据库。", 409
        )
    with psycopg.connect(
        make_conninfo(settings.admin_database_url, dbname="postgres"), autocommit=True
    ) as conn:
        if conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=ANY(%s)",
            (["tax_harness_extended", "tax_harness_extended_test"],),
        ).fetchone():
            raise AppError("DATABASE_EXISTS", "扩展库已存在，未执行覆盖或重置。", 409)
        conn.execute("CREATE DATABASE tax_harness_extended")
        conn.execute("CREATE DATABASE tax_harness_extended_test")
    app_password, ingest_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    connection = conninfo_to_dict(settings.admin_database_url)
    host, port = connection.get("host", "127.0.0.1"), connection.get("port", "54337")

    def url(user, password, database):
        return f"postgresql://{quote(user)}:{quote(password)}@{host}:{port}/{database}"

    app_url = url("tax_app_extended", app_password, "tax_harness_extended")
    ingest_url = url("tax_ingest", ingest_password, "tax_harness_extended")
    config = {
        k: v for k, v in dotenv_values(Settings.model_config["env_file"]).items() if v is not None
    }
    config.update(
        ADMIN_DATABASE_URL=make_conninfo(
            settings.admin_database_url, dbname="tax_harness_extended"
        ),
        DATABASE_URL=app_url,
        TEST_DATABASE_URL=url("tax_app_extended", app_password, "tax_harness_extended_test"),
        INGEST_DATABASE_URL=ingest_url,
    )
    extended_settings = settings.model_copy(
        update={
            "admin_database_url": config["ADMIN_DATABASE_URL"],
            "database_url": app_url,
            "test_database_url": config["TEST_DATABASE_URL"],
            "ingest_database_url": ingest_url,
        }
    )
    initialize(extended_settings, extended=True)
    initialize(extended_settings, test=True, extended=True)
    # Quote all values for dotenv without expanding or printing their contents.
    path.write_text(
        "".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}\n" for key, value in config.items()
        ),
        encoding="utf-8",
    )
    return {
        "database": "tax_harness_extended",
        "test_database": "tax_harness_extended_test",
        "configuration": str(path),
    }


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


def knowledge_command(args, settings):
    from app import knowledge_admin

    if args.knowledge_action == "add-version":
        from app.db import Database
        from app.identity import Identity
        from app.knowledge import Knowledge

        if args.file.stat().st_size > settings.knowledge_max_file_bytes:
            raise AppError("DOCUMENT_TOO_LARGE", "资料最多20 MiB。", 413)
        database = Database(settings.admin_database_url)
        database.open()
        try:
            with database.transaction(readonly=True) as conn:
                row = conn.execute(
                    "SELECT * FROM knowledge_documents WHERE document_id=%s", (args.document_id,)
                ).fetchone()
                if not row:
                    raise AppError("RESOURCE_NOT_FOUND", "企业资料不存在。", 404)
            return Knowledge(database, settings).upload(
                Identity(row["owner_id"]),
                name=args.file.name,
                content=args.file.read_bytes(),
                company_id=row["company_id"],
                title=row["title"],
                request_id=args.request_id,
                document_id=args.document_id,
                expected_revision=args.expected_revision,
                maintenance=True,
            )
        finally:
            database.close()
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        if args.knowledge_action == "list":
            return conn.execute(
                "SELECT * FROM knowledge_documents WHERE company_id=%s ORDER BY created_at DESC",
                (args.company,),
            ).fetchall()
        if args.knowledge_action == "show":
            return {
                "document": conn.execute(
                    "SELECT * FROM knowledge_documents WHERE document_id=%s", (args.document_id,)
                ).fetchone(),
                "blocks": conn.execute(
                    "SELECT b.*,v.edition FROM knowledge_blocks b JOIN knowledge_versions v USING(version_id) WHERE v.document_id=%s ORDER BY v.edition,b.ordinal",
                    (args.document_id,),
                ).fetchall(),
            }
        if args.knowledge_action == "publish":
            return knowledge_admin.publish_company(
                conn, args.document_id, args.version_id, args.expected_revision, args.request_id
            )
        if args.knowledge_action == "disable":
            return knowledge_admin.disable_company(
                conn, args.document_id, args.expected_revision, args.request_id
            )
        if args.knowledge_action == "link-rule":
            return knowledge_admin.link_rule(
                conn,
                args.rule_version_id,
                json.loads(args.file.read_text(encoding="utf-8")),
                args.request_id,
            )
        return knowledge_admin.revoke_read(conn, args.user_id, args.company_id, args.request_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tax Harness local administration")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("configure")
    subs.add_parser("init-extended")
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
    k = subs.add_parser("knowledge").add_subparsers(dest="knowledge_action", required=True)
    k.add_parser("list").add_argument("--company", required=True)
    k.add_parser("show").add_argument("document_id")
    for action_name in ("publish", "disable", "add-version"):
        command = k.add_parser(action_name)
        command.add_argument("document_id")
        command.add_argument("--expected-revision", type=int, required=True)
        command.add_argument("--request-id", required=True)
        if action_name == "publish":
            command.add_argument("--version-id", required=True)
        if action_name == "add-version":
            command.add_argument("--file", type=Path, required=True)
    link = k.add_parser("link-rule")
    link.add_argument("rule_version_id")
    link.add_argument("file", type=Path)
    link.add_argument("--request-id", required=True)
    revoke = k.add_parser("revoke-read")
    revoke.add_argument("user_id")
    revoke.add_argument("company_id")
    revoke.add_argument("--request-id", required=True)
    args = parser.parse_args()
    if args.command == "configure":
        configure()
        return
    try:
        if args.command == "init-extended":
            result = initialize_extended(Settings())
        elif args.command == "knowledge":
            result = knowledge_command(args, Settings())
        else:
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
