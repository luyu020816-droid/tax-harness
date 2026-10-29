from decimal import Decimal

import pytest
from test_compaction import closed_group, summary_message
from test_runtime import ScriptedModel, model_settings

from app import business, rules
from app.compaction import compact
from app.errors import AppError
from app.identity import Identity


@pytest.mark.asyncio
async def test_summary_preserves_provider_configuration(
    db, settings, source_factory, execution_factory
):
    source = source_factory("甲企业2026年9月尚有用途待补，不授权自动修改。")
    group = closed_group(db, source)
    config = model_settings(settings).model_copy(
        update={"model_extra_body": {"thinking": {"type": "disabled"}}}
    )
    model = ScriptedModel([summary_message(source)])
    assert await compact(db, config, model, execution_factory("继续查询"), [group], 1)
    assert model.requests[0]["thinking"] == {"type": "disabled"}
    assert model.requests[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in model.requests[0]


@pytest.mark.parametrize("key", ["messages", "model", "response_format", "tool_choice"])
@pytest.mark.asyncio
async def test_summary_rejects_override_of_protocol_fields(
    key, db, settings, source_factory, execution_factory
):
    source = source_factory("甲企业资料仅查询。")
    group = closed_group(db, source)
    config = model_settings(settings).model_copy(update={"model_extra_body": {key: "bad"}})
    model = ScriptedModel([])
    with pytest.raises(AppError) as error:
        await compact(db, config, model, execution_factory("继续查询"), [group], 1)
    assert error.value.code == "INVALID_ARGUMENT"
    assert not model.requests


@pytest.mark.parametrize(
    "code,topic,kind", [("R001", "amounts", "money_summary"), ("R004", "bank", "bank_read_only")]
)
def test_non_purpose_rule_version_can_be_maintained(code, topic, kind, db, admin):
    old = admin.execute(
        "SELECT * FROM rule_versions WHERE rule_code=%s AND status='active'", (code,)
    ).fetchone()
    condition = {"kind": kind}
    data = {
        "rule_code": code,
        "display_name": old["display_name"],
        "business_version": "1.1",
        "effective_from": "2026-09-01",
        "companies": ["C001", "C002"],
        "topic": topic,
        "condition_json": condition,
        "full_text": rules.template(condition),
    }
    with admin.transaction():
        created = rules.create_version(admin, data)
        assert created["topic"] == topic and created["can_activate"]
        rules.activate(admin, str(created["rule_version_id"]), str(old["rule_version_id"]))
    result = business.query_month(db, Identity("U001"), "C002", "2026-09")
    assert result["invoice_total"] == Decimal("12667.24")
    selected = next(r for r in result["rule_refs"] if r["rule_code"] == code)
    assert selected["business_version"] == "1.1"
    with admin.transaction():
        rules.disable(admin, str(created["rule_version_id"]))
    with pytest.raises(AppError) as error:
        business.query_month(db, Identity("U001"), "C002", "2026-09")
    assert error.value.code == "RULE_UNAVAILABLE"


def test_shared_rule_cannot_be_replaced_for_only_one_company(db, admin):
    old = admin.execute(
        "SELECT * FROM rule_versions WHERE rule_code='R001' AND status='active'"
    ).fetchone()
    condition = {"kind": "money_summary"}
    with admin.transaction():
        created = rules.create_version(
            admin,
            {
                "rule_code": "R001",
                "display_name": old["display_name"],
                "business_version": "1.1",
                "effective_from": "2026-09-01",
                "companies": ["C001"],
                "topic": "amounts",
                "condition_json": condition,
                "full_text": rules.template(condition),
            },
        )
    with pytest.raises(AppError) as error, admin.transaction():
        rules.activate(admin, str(created["rule_version_id"]), str(old["rule_version_id"]))
    assert error.value.code == "RULE_SCOPE_MISMATCH"
    assert business.query_month(db, Identity("U001"), "C002", "2026-09")["rules_epoch"] == 1
