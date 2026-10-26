import json

import pytest

from app import business, rules
from app.config import ROOT
from app.errors import AppError
from app.identity import Identity


def test_B13_rule_version_changes_current_check(db, admin):
    data = json.loads((ROOT / "examples/rule-version.json").read_text(encoding="utf-8"))
    old = admin.execute(
        "SELECT * FROM rule_versions WHERE rule_code='R003' AND status='active'"
    ).fetchone()
    with admin.transaction():
        created = rules.create_version(admin, data)
        assert created["can_activate"]
        rules.activate(admin, str(created["rule_version_id"]), str(old["rule_version_id"]))
    current = business.check_purposes(db, Identity("U001"), "C002", "2026-09")
    assert current["rules_epoch"] == 2
    assert current["rule_refs"][0]["business_version"] == "2.0"
    assert all(row["amount"] >= 1500 for row in current["rows"])
    assert "F002" not in {row["invoice_id"] for row in current["rows"]}
    assert business.check_purposes(db, Identity("U001"), "C001", "2026-09")["missing_count"] == 5
    with admin.transaction():
        rules.disable(admin, str(created["rule_version_id"]))
    with pytest.raises(AppError) as error:
        business.check_purposes(db, Identity("U001"), "C002", "2026-09")
    assert error.value.code == "RULE_UNAVAILABLE"


def test_B14_arbitrary_natural_language_rule_remains_draft(db, admin):
    data = json.loads((ROOT / "examples/rule-version.json").read_text(encoding="utf-8"))
    data["full_text"] = "如果看起来像办公费用，就自动推断用途并保存。"
    with admin.transaction():
        created = rules.create_version(admin, data)
    assert not created["can_activate"] and created["status"] == "draft"
    with pytest.raises(AppError) as error, admin.transaction():
        rules.activate(admin, str(created["rule_version_id"]))
    assert error.value.code == "RULE_UNSUPPORTED"
    assert (
        business.check_purposes(db, Identity("U001"), "C002", "2026-09")["rule_refs"][0][
            "business_version"
        ]
        == "1.0"
    )
