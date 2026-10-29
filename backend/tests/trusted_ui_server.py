"""Isolated browser fixture for the real confirmation API, with a scripted model."""

import json

import psycopg
import uvicorn
from psycopg.conninfo import make_conninfo
from test_runtime import ScriptedModel, final, model_settings, tool

from app.api import create_app
from app.cli import initialize
from app.config import Settings


def prepare(payload):
    state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n")[1])
    row = json.loads(payload["messages"][-1]["content"])["data"]["rows"][0]
    return tool(
        "prepare_invoice_write",
        {
            "company_id": "C001",
            "invoice_id": "F002",
            "patch": {"purpose": "办公耗材采购"},
            "expected_revision": row["revision"],
            "source_refs": [
                {
                    "source_id": state["current_source"]["source_id"],
                    "quote": "请安排把甲企业F002用途填写为办公耗材采购。",
                }
            ],
        },
    )


def confirmation(payload):
    plan = json.loads(payload["messages"][-1]["content"])["data"]
    return {
        "role": "assistant",
        "content": json.dumps(
            {
                "blocks": [{"type": "confirmation", "plan_id": plan["plan_id"]}],
                "status": "waiting_user",
                "clarification": {
                    "intent": "update_invoice",
                    "company_id": "C001",
                    "missing_fields": ["write_confirmation"],
                },
            }
        ),
    }


if __name__ == "__main__":
    config = Settings()
    database = "tax_harness_trusted_ui"
    with psycopg.connect(
        make_conninfo(config.admin_database_url, dbname="postgres"), autocommit=True
    ) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            conn.execute("CREATE DATABASE tax_harness_trusted_ui")
    config = model_settings(config).model_copy(
        update={
            "database_url": make_conninfo(config.database_url, dbname=database),
            "admin_database_url": make_conninfo(config.admin_database_url, dbname=database),
        }
    )
    initialize(config)
    with psycopg.connect(config.admin_database_url, autocommit=True) as conn:
        if conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
        ).fetchone()[0]:
            raise SystemExit("Stop the trusted UI fixture before restarting.")
        conn.execute("TRUNCATE users,companies,import_batches,rule_versions CASCADE")
    initialize(config)
    model = ScriptedModel(
        [
            tool("query_month", {"company_id": "C001", "period": "2026-09"}),
            final("查询完成"),
            tool("find_invoices", {"company_id": "C001", "ids": ["F002"]}),
            prepare,
            confirmation,
        ]
    )
    uvicorn.run(create_app(config, model=model), host="127.0.0.1", port=8106)
