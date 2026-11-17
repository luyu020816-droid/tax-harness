"""Upgrade a disposable pre-Agent database with preserved representative records."""

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.cli import initialize
from app.config import ROOT
from app.importer import import_samples
from app.values import uid


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
