"""Upgrade disposable legacy databases while preserving representative records."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from test_runtime import ScriptedModel, model_settings
from test_workbook_query import COLUMNS, workbook

from app import invoice_write, tasks, turns
from app.cli import initialize
from app.config import ROOT, Settings
from app.context_scope import find_summary, summary_key
from app.db import Database
from app.errors import AppError
from app.identity import Identity, create_session
from app.importer import import_samples
from app.knowledge import Knowledge
from app.knowledge_jobs import KnowledgeJobs
from app.knowledge_strategy import Strategy, publish, read_in
from app.runtime import Runtime
from app.values import jsonb, uid
from app.workbook_queries import dispatch, read_result

AUTH = Identity("U001")
MIGRATIONS = ROOT / "backend/migrations"
# Historical DDL from e58b8bf, independent of the repair migration.
LEGACY_B = {
    24: """
        CREATE TABLE knowledge_strategies (
            company_id text PRIMARY KEY REFERENCES companies ON DELETE CASCADE,
            revision integer NOT NULL CHECK (revision > 0)
        );
        CREATE TABLE knowledge_strategy_versions (
            company_id text NOT NULL REFERENCES knowledge_strategies ON DELETE CASCADE,
            version integer NOT NULL CHECK (version > 0),
            config jsonb NOT NULL,
            actor_id text NOT NULL REFERENCES users,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(company_id,version)
        );
        INSERT INTO schema_migrations(version) VALUES(24);
    """,
    25: """
        ALTER TABLE summaries ADD COLUMN agent_scope text NOT NULL DEFAULT '';
        CREATE INDEX summaries_agent_scope ON summaries
            (user_id,session_id,scope,agent_scope,memory_epoch,generation DESC);
        INSERT INTO schema_migrations(version) VALUES(25);
    """,
}


@pytest.fixture
def semifinal_database():
    settings = Settings(_env_file=ROOT / ".env")
    assert conninfo_to_dict(settings.test_database_url).get("dbname") == "tax_harness_test"
    name = "semifinal_migration_test_" + uid().replace("-", "")
    admin_url = make_conninfo(settings.admin_database_url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield settings.model_copy(
                update={
                    "admin_database_url": make_conninfo(admin_url, dbname=name),
                    "database_url": make_conninfo(settings.test_database_url, dbname=name),
                    "ingest_database_url": "",
                }
            )
        finally:
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _install_legacy(settings, family, through):
    if not through:
        return
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        vector = conn.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name='vector'"
        ).fetchone()
        for migration in sorted(MIGRATIONS.glob("[0-9]*.sql")):
            version = int(migration.name.split("_", 1)[0])
            if version > through:
                continue
            if version not in (6, 9, 15) or vector:
                script = LEGACY_B.get(version) if family == "B" else None
                conn.execute(script or migration.read_text(encoding="utf-8"))
        materials = ROOT / "docs/赛题二：财税Harness初赛材料包-v1.1.0"
        import_samples(conn, materials / "02_示例数据.xlsx", materials / "03_业务规则.docx")
        conn.execute(
            "INSERT INTO agent_config_managers(user_id) VALUES('U001') ON CONFLICT DO NOTHING"
        )
        if family == "B":
            conn.execute("INSERT INTO knowledge_strategies VALUES('C001',1)")
            conn.execute(
                "INSERT INTO knowledge_strategy_versions(company_id,version,config,actor_id) "
                "VALUES('C001',1,%s,'U001')",
                (jsonb({"max_groups": 3}),),
            )
        conn.execute(
            "UPDATE invoices SET note='保留原备注' WHERE company_id='C001' AND invoice_id='F002'"
        )


def _workbook_result(db, settings):
    file = Knowledge(db, settings).upload(
        AUTH,
        name="报销.xlsx",
        title="报销",
        company_id="C001",
        content=workbook(),
        request_id=uid(),
    )
    jobs = KnowledgeJobs(db, settings, None)
    jobs.run(jobs.claim())
    session_id = str(create_session(db, AUTH, "C001")["session_id"])
    turn = turns.accept_turn(
        db,
        AUTH,
        session_id,
        uid(),
        "2026年10月实付报销合计多少",
        knowledge_upload_ids=[file["file_id"]],
    )
    ex = Runtime(db, model_settings(settings), ScriptedModel([])).claim()
    assert ex.turn_id == turn["turn_id"]
    return dispatch(
        db,
        ex,
        "query_workbook",
        {
            "document_id": file["document_id"],
            "version_id": file["version_id"],
            "sheet": "报销明细",
            "columns": COLUMNS,
            "date_from": "2026-10-01",
            "date_to_exclusive": "2026-11-01",
        },
    )["result_id"]


def _purpose_plan(db):
    task = tasks.change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        request_id=uid(),
        intent="complete_purposes",
        period="2026-09",
    )
    session_id = str(create_session(db, AUTH, "C001")["session_id"])
    text = "请安排把甲企业F002用途填写为办公耗材采购。"
    turn = turns.accept_turn(db, AUTH, session_id, uid(), text)
    with db.transaction(readonly=True) as conn:
        source_id = str(
            conn.execute(
                "SELECT source_id FROM sources WHERE turn_id=%s AND role='user'", (turn["turn_id"],)
            ).fetchone()["source_id"]
        )
    plan = invoice_write.prepare(
        db,
        AUTH,
        company_id="C001",
        invoice_id="F002",
        patch={"purpose": "办公耗材采购"},
        expected_revision=1,
        source_id=source_id,
        source_refs=[{"source_id": source_id, "quote": text}],
    )
    assert plan["status"] == "needs_confirmation"
    return task, plan


_SUMMARY_BINDING = {
    "agent_id": "旧助手",
    "agent_version": 1,
    "snapshot": {"knowledge_mode": "none"},
}


def _scoped_summary(db):
    execution = SimpleNamespace(
        auth=AUTH, session_id=str(create_session(db, AUTH, "C001")["session_id"])
    )
    with db.transaction() as conn:
        key = summary_key(conn, execution, binding=_SUMMARY_BINDING)
        conn.execute(
            "INSERT INTO summaries(summary_id,user_id,session_id,scope,agent_scope,covered_sources,"
            "memory_epoch,content,status,generation) "
            "SELECT %s,user_id,%s,'C001',%s,'[]',memory_epoch,'带范围的摘要','active',1 "
            "FROM users WHERE user_id='U001'",
            (uid(), execution.session_id, key),
        )
    return execution


@pytest.mark.parametrize(
    "family,through", [("A", 25), ("A", 24), ("B", 25), ("B", 24), ("empty", 0)]
)
def test_int01_databases_upgrade_and_run_all_four_business_paths(
    semifinal_database, family, through
):
    settings = semifinal_database
    _install_legacy(settings, family, through)
    old_db = Database(settings.admin_database_url)
    old_db.open()
    try:
        assert not old_db.ready()
        old_workbook = _workbook_result(old_db, settings) if family == "A" else None
        old_plan = _purpose_plan(old_db) if (family, through) == ("A", 25) else None
        old_summary = _scoped_summary(old_db) if (family, through) == ("B", 25) else None
    finally:
        old_db.close()

    initialize(settings)
    initialize(settings)
    db = Database(settings.database_url)
    db.open()
    try:
        assert db.ready()
        with db.transaction(readonly=True) as conn:
            initial = read_in(conn, AUTH, "C001")
            if family == "B":
                assert initial["config"]["max_groups"] == 3
            if through:
                invoice = conn.execute(
                    "SELECT note,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
                ).fetchone()
                assert invoice == {"note": "保留原备注", "revision": 1}
        execution = old_summary or _scoped_summary(db)
        with db.transaction(readonly=True) as conn:
            epoch = conn.execute("SELECT memory_epoch FROM users WHERE user_id='U001'").fetchone()
            assert (
                find_summary(
                    conn, execution, "C001", epoch["memory_epoch"], binding=_SUMMARY_BINDING
                )["content"]
                == "带范围的摘要"
            )
        assert (
            publish(db, AUTH, "C001", initial["revision"], Strategy(max_groups=4))["config"][
                "max_groups"
            ]
            == 4
        )
        result_id = old_workbook or _workbook_result(db, settings)
        with db.transaction(readonly=True) as conn:
            assert read_result(conn, AUTH, result_id)["total"] == "617.00"

        task, plan = old_plan or _purpose_plan(db)
        tasks.change_task(
            db,
            AUTH,
            company_id="C001",
            action="cancel",
            request_id=uid(),
            target_id=str(task["task_id"]),
            expected_revision=task["revision"],
        )
        assert invoice_write.get(db, AUTH, plan["plan_id"])["status"] == "revoked"
        with db.transaction(readonly=True) as conn:
            assert not conn.execute("SELECT 1 FROM operations").fetchone()
    finally:
        db.close()


def test_int01_serializes_initialization_and_rejects_incompatible_structure(semifinal_database):
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(initialize, semifinal_database)
        second = workers.submit(initialize, semifinal_database)
        results = [first.result()["import"], second.result()["import"]]
        assert sum(bool(result.get("already_imported")) for result in results) == 1

    with psycopg.connect(semifinal_database.admin_database_url) as conn:
        conn.execute("DROP INDEX summaries_agent_scope")
        conn.execute("CREATE INDEX summaries_agent_scope ON summaries(user_id)")
    db = Database(semifinal_database.database_url)
    db.open()
    try:
        assert not db.ready()
        with pytest.raises(AppError, match="summaries_agent_scope") as error:
            initialize(semifinal_database)
        assert error.value.code == "SCHEMA_INCOMPATIBLE"
    finally:
        db.close()


def test_int01_failed_upgrade_preserves_legacy_schema(semifinal_database):
    _install_legacy(semifinal_database, "A", 25)
    with psycopg.connect(semifinal_database.admin_database_url) as conn:
        conn.execute("ALTER TABLE summaries ADD COLUMN agent_scope integer")
    with pytest.raises(AppError, match="summaries.agent_scope"):
        initialize(semifinal_database)
    with psycopg.connect(semifinal_database.admin_database_url) as conn:
        assert not conn.execute("SELECT 1 FROM schema_migrations WHERE version=26").fetchone()
        assert conn.execute("SELECT to_regclass('public.knowledge_strategies')").fetchone() == (
            None,
        )
        assert conn.execute(
            "SELECT note FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone() == ("保留原备注",)


def test_pre_agent_sample_database_upgrade_preserves_history_and_business_data(settings):
    name = "agent_migration_test_" + uid().replace("-", "")
    admin_url = make_conninfo(settings.admin_database_url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            old_url = make_conninfo(settings.admin_database_url, dbname=name)
            session_id, turn_id = uid(), uid()
            with psycopg.connect(old_url, row_factory=dict_row) as conn:
                for migration in sorted((ROOT / "backend/migrations").glob("[0-9]*.sql")):
                    number = int(migration.name.split("_", 1)[0])
                    if number >= 17:
                        break
                    if (
                        number in (6, 9, 15)
                        and not conn.execute(
                            "SELECT 1 FROM pg_available_extensions WHERE name='vector'"
                        ).fetchone()
                    ):
                        continue
                    conn.execute(migration.read_text(encoding="utf-8"))
                materials = ROOT / "docs/赛题二：财税Harness初赛材料包-v1.1.0"
                import_samples(conn, materials / "02_示例数据.xlsx", materials / "03_业务规则.docx")
                conn.execute(
                    "UPDATE invoices SET purpose=%s,revision=revision+1 WHERE company_id='C001' AND invoice_id='F002'",
                    ("迁移前已有用途",),
                )
                conn.execute(
                    "INSERT INTO sessions(session_id,user_id,selected_company_id) VALUES(%s,'U001','C001')",
                    (session_id,),
                )
                conn.execute(
                    "INSERT INTO turns(turn_id,session_id,user_id,client_request_id,request_hash,source_seq,text,status) VALUES(%s,%s,'U001','legacy-test','hash',1,'迁移前的对话','completed')",
                    (turn_id, session_id),
                )
            upgraded = settings.model_copy(
                update={
                    "admin_database_url": old_url,
                    "database_url": make_conninfo(settings.database_url, dbname=name),
                }
            )
            initialize(upgraded)
            with psycopg.connect(old_url, row_factory=dict_row) as conn:
                assert (
                    conn.execute(
                        "SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
                    ).fetchone()["purpose"]
                    == "迁移前已有用途"
                )
                assert (
                    conn.execute("SELECT text FROM turns WHERE turn_id=%s", (turn_id,)).fetchone()[
                        "text"
                    ]
                    == "迁移前的对话"
                )
                assert not conn.execute(
                    "SELECT 1 FROM agent_turn_bindings WHERE turn_id=%s", (turn_id,)
                ).fetchone()
                assert (
                    conn.execute(
                        "SELECT count(*) AS n FROM agent_profiles WHERE enabled"
                    ).fetchone()["n"]
                    == 2
                )
                assert conn.execute(
                    "SELECT 1 FROM agent_config_managers WHERE user_id='U001'"
                ).fetchone()
        finally:
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
