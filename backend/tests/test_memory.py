import pytest

from app.business import check_purposes
from app.errors import AppError
from app.identity import Identity, create_session
from app.memory import load_core, search_history
from app.preferences import change_preference, list_preferences
from app.sources import add_source, next_sequence
from app.tasks import apply_result, change_task, list_tasks
from app.values import uid

AUTH = Identity("U001")


def test_M01_M02_one_task_and_explicit_replacement(db):
    first = change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        request_id=uid(),
        intent="complete_purposes",
        period="2026-09",
    )
    with pytest.raises(AppError) as exc:
        change_task(
            db,
            AUTH,
            company_id="C001",
            action="start",
            request_id=uid(),
            intent="query_month",
            period="2026-08",
        )
    assert exc.value.code == "NEEDS_CLARIFICATION"
    new = change_task(
        db,
        AUTH,
        company_id="C001",
        action="replace",
        target_id=str(first["task_id"]),
        expected_revision=first["revision"],
        request_id=uid(),
        intent="query_month",
        period="2026-08",
    )
    assert str(first["task_id"]) != str(new["task_id"])
    assert sum(t["status"] in ("active", "waiting_user") for t in list_tasks(db, AUTH)) == 1


@pytest.mark.parametrize("action", ["cancel", "delete"])
def test_M03_unknown_month_can_end(db, action):
    task = change_task(
        db, AUTH, company_id="C001", action="start", request_id=uid(), intent="query_month"
    )
    ended = change_task(
        db,
        AUTH,
        company_id="C001",
        action=action,
        target_id=str(task["task_id"]),
        expected_revision=task["revision"],
        request_id=uid(),
    )
    assert ended["period"] is None
    assert ended["status"] == ("cancelled" if action == "cancel" else "deleted")


def test_M04_new_session_reads_structured_task(db):
    task = change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        request_id=uid(),
        intent="complete_purposes",
        period="2026-09",
    )
    session = create_session(db, AUTH, "C001")
    core = load_core(db, AUTH, str(session["session_id"]))
    assert core["task"]["task_id"] == str(task["task_id"])
    assert core["task"]["period"] == "2026-09"


def test_M05_M07_preference_revision_delete_and_recreate(db):
    first = change_preference(
        db,
        AUTH,
        action="remember",
        scope="@user",
        key="response_style",
        value="简洁",
        request_id=uid(),
    )
    updated = change_preference(
        db,
        AUTH,
        action="update",
        target_id=str(first["preference_id"]),
        expected_revision=1,
        value="详细",
        request_id=uid(),
    )
    assert updated["value"] == "详细"
    deleted = change_preference(
        db,
        AUTH,
        action="delete",
        target_id=str(first["preference_id"]),
        expected_revision=2,
        request_id=uid(),
    )
    assert list_preferences(db, AUTH) == []
    new = change_preference(
        db,
        AUTH,
        action="remember",
        key="response_style",
        expected_revision=deleted["revision"],
        value="适中",
        request_id=uid(),
    )
    assert new["value"] == "适中" and new["generation"] > deleted["generation"]


def test_M05_company_preference_overrides_global(db):
    change_preference(
        db, AUTH, action="remember", key="response_style", value="简洁", request_id=uid()
    )
    change_preference(
        db,
        AUTH,
        action="remember",
        scope="C001",
        key="response_style",
        value="详细",
        request_id=uid(),
    )
    one = create_session(db, AUTH, "C001")
    two = create_session(db, AUTH, "C002")
    assert load_core(db, AUTH, str(one["session_id"]))["preferences"][0]["value"] == "详细"
    assert load_core(db, AUTH, str(two["session_id"]))["preferences"][0]["value"] == "简洁"


def test_F12_F33_old_source_cannot_recreate_deleted_task(db, execution_factory):
    execution = execution_factory("继续甲企业9月用途检查")
    refs = [{"source_id": execution.source_id, "quote": "继续甲企业9月用途检查"}]
    task = change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        intent="complete_purposes",
        period="2026-09",
        request_id=uid(),
        source_refs=refs,
        execution=execution,
    )
    deleted = change_task(
        db,
        AUTH,
        company_id="C001",
        action="delete",
        target_id=str(task["task_id"]),
        expected_revision=task["revision"],
        request_id=uid(),
    )
    execution.expected_epoch = deleted["memory_epoch"]
    with pytest.raises(AppError) as exc:
        change_task(
            db,
            AUTH,
            company_id="C001",
            action="start",
            intent="complete_purposes",
            period="2026-09",
            request_id=uid(),
            source_refs=refs,
            execution=execution,
        )
    assert exc.value.code == "SOURCE_INVALIDATED"


def test_M08_private_memory_and_wrong_scope_id(db):
    pref = change_preference(
        db,
        AUTH,
        action="remember",
        scope="C001",
        key="response_style",
        value="简洁",
        request_id=uid(),
    )
    assert list_preferences(db, Identity("U002"), "C001") == []
    with pytest.raises(AppError):
        change_preference(
            db,
            Identity("U002"),
            action="delete",
            scope="C001",
            target_id=str(pref["preference_id"]),
            expected_revision=1,
            request_id=uid(),
        )
    with pytest.raises(AppError):
        change_preference(
            db,
            AUTH,
            action="delete",
            scope="C002",
            target_id=str(pref["preference_id"]),
            expected_revision=1,
            request_id=uid(),
        )


def test_M10_M11_search_old_sources_not_recent_window(db, source_factory):
    target = source_factory("甲企业当时要求先列缺项")
    for n in range(30):
        source_factory(f"甲企业无关记录{n}")
    found = search_history(db, AUTH, company_id="C001", query="先列缺项")
    assert found["matches"][0]["source_id"] == str(target["source_id"])


def test_M05_deleting_one_fact_preserves_other_fact_from_same_source(db, execution_factory):
    execution = execution_factory("以后回答简洁，同时金额保留两位小数")
    refs = [{"source_id": execution.source_id, "quote": "以后回答简洁，同时金额保留两位小数"}]
    first = change_preference(
        db,
        AUTH,
        action="remember",
        key="response_style",
        value="简洁",
        request_id=uid(),
        source_refs=refs,
        execution=execution,
    )
    execution.expected_epoch = first["memory_epoch"]
    second = change_preference(
        db,
        AUTH,
        action="remember",
        key="money_format",
        value="金额保留两位小数",
        request_id=uid(),
        source_refs=refs,
        execution=execution,
    )
    change_preference(
        db,
        AUTH,
        action="delete",
        target_id=str(first["preference_id"]),
        expected_revision=first["revision"],
        request_id=uid(),
    )
    core = load_core(db, AUTH, execution.session_id)
    assert [(p["stable_key"], p["value"]) for p in core["preferences"]] == [
        ("money_format", second["value"])
    ]
    assert search_history(db, AUTH, company_id="C001", query="回答简洁")["not_found"]


def test_F25_stale_zero_missing_cannot_complete_task(db, admin):
    task = change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        request_id=uid(),
        intent="complete_purposes",
        period="2026-09",
    )
    admin.execute(
        "UPDATE invoices SET purpose='已明确' WHERE company_id='C001' AND invoice_date>='2026-09-01'"
    )
    admin.execute("UPDATE company_heads SET data_revision=data_revision+1 WHERE company_id='C001'")
    result = check_purposes(db, AUTH, "C001", "2026-09")
    with db.transaction() as conn:
        source = add_source(
            conn,
            AUTH,
            result,
            role="tool",
            kind="tool_result",
            source_seq=next_sequence(conn, AUTH),
            companies=["C001"],
        )
    admin.execute("UPDATE invoices SET purpose=NULL WHERE company_id='C001' AND invoice_id='F002'")
    admin.execute("UPDATE company_heads SET data_revision=data_revision+1 WHERE company_id='C001'")
    with pytest.raises(AppError) as exc:
        apply_result(db, AUTH, task, result, str(source["source_id"]), source["source_seq"])
    assert exc.value.code == "STALE_CONTEXT"
    assert list_tasks(db, AUTH)[0]["status"] == "active"
