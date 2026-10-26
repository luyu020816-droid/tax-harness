"""Explicit program-test server; isolated database, fixed tool script, no LLM claims."""

import json

import psycopg
import uvicorn
from psycopg.conninfo import make_conninfo
from test_runtime import ScriptedModel, final, model_settings, tool

from app.api import create_app
from app.cli import initialize
from app.config import Settings


def save_known_invoice(payload):
    state = json.loads(payload["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
    found = json.loads(payload["messages"][-1]["content"])["data"]
    invoice = next(row for row in found["rows"] if row["invoice_id"] == "F002")
    return tool(
        "update_invoice",
        {
            "company_id": "C001",
            "invoice_id": "F002",
            "expected_revision": invoice["revision"],
            "patch": {"purpose": "办公耗材采购"},
            "source_refs": [
                {
                    "source_id": state["current_source"]["source_id"],
                    "quote": "F002用途补为办公耗材采购",
                }
            ],
        },
    )


settings = Settings()
assert "tax_harness_test" in settings.test_database_url
with psycopg.connect(
    make_conninfo(settings.admin_database_url, dbname="tax_harness_test"), autocommit=True
) as conn:
    conn.execute("TRUNCATE users,companies,import_batches,rule_versions CASCADE")
initialize(settings, test=True)
settings = model_settings(settings).model_copy(update={"database_url": settings.test_database_url})
model = ScriptedModel(
    [
        tool("query_month", {"company_id": "C001", "period": "2026-09"}),
        final("已查询甲企业2026年9月资料，金额与统计范围见下方结果。"),
        tool("check_purposes", {"company_id": "C001", "period": "2026-09"}),
        final(
            "按适用规则检查，还有5张发票需要补充实际用途。请提供确认后的用途。",
            status="waiting_user",
            clarification={
                "intent": "complete_purposes",
                "company_id": "C001",
                "period": "2026-09",
                "missing_fields": ["purpose"],
                "summary": "等待用户提供实际用途",
            },
        ),
        tool("find_invoices", {"company_id": "C001", "ids": ["F002"]}),
        save_known_invoice,
        final("F002用途已保存并回读核验；重新检查后，还有4张需要补充。"),
    ]
)
if __name__ == "__main__":
    uvicorn.run(create_app(settings, model=model), host="127.0.0.1", port=8101)
