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


def preference_command(db, execution, text, **args):
    from test_runtime import tool

    from app.tools import ToolExecutor

    return ToolExecutor(db).execute(
        execution,
        tool(
            "manage_preference",
            {
                "source_refs": [{"source_id": execution.source_id, "quote": text}],
                **args,
            },
        )["tool_calls"][0],
    )[0]


@pytest.mark.parametrize(
    "text,value",
    [
        ("只这次三行，不要记住。", "三行"),
        ("如果我说“以后回复简短”，该怎么理解？", "简短"),
        ("比如：今后回复详细。", "详细"),
        ("以后回复简短，但不要记下来。", "简短"),
        ("以后请回复简短。", "详细"),
    ],
)
def test_mem01_proposals_need_persistent_intent_and_supported_value(
    db, admin, execution_factory, text, value
):
    result = preference_command(
        db,
        execution_factory(text),
        text,
        action="remember",
        key="response_style",
        value=value,
    )
    assert not result["ok"], result
    assert result["error"]["code"] == "NEEDS_CLARIFICATION"
    assert not list_preferences(db, AUTH)
    assert not admin.execute("SELECT 1 FROM preference_revisions").fetchone()
    assert not admin.execute(
        "SELECT 1 FROM memory_change_ops WHERE target_type='preference'"
    ).fetchone()


def test_mem02_company_instruction_cannot_be_saved_as_personal(db, execution_factory):
    text = "请记住：甲企业项目标识MEM02标记。"
    execution = execution_factory(text)
    proposed = preference_command(
        db,
        execution,
        text,
        action="remember",
        key="report_marker",
        value="甲企业项目标识MEM02标记",
    )
    assert not proposed["ok"], proposed
    assert proposed["error"]["code"] == "MEMORY_SCOPE_MISMATCH"
    valid = preference_command(
        db,
        execution,
        text,
        action="remember",
        scope="C001",
        key="report_marker",
        value="项目标识MEM02标记",
    )
    assert valid["ok"], valid
    assert [p["value"] for p in list_preferences(db, AUTH, "C001")] == ["项目标识MEM02标记"]
    assert not list_preferences(db, AUTH, "C002")
    assert not list_preferences(db, Identity("U002"), "C001")


def test_ar02_cross_session_correction_uses_original_preference_id(db, execution_factory):
    text = "请记住，今后回复要简短。"
    first = preference_command(
        db,
        execution_factory(text),
        text,
        action="remember",
        key="response_length",
        value="简短",
    )
    assert first["ok"], first
    correction = "更正刚才的要求，今后回复要详细，请记住。"
    second = preference_command(
        db,
        execution_factory(correction, company="C002"),
        correction,
        action="remember",
        key="answer_length",
        value="详细",
    )
    assert second["ok"], second
    assert second["data"]["preference_id"] == first["data"]["preference_id"]
    assert second["data"]["revision"] == first["data"]["revision"] + 1
    assert [p["value"] for p in list_preferences(db, AUTH, "C002")] == ["详细"]


@pytest.mark.asyncio
async def test_mem02_legacy_company_source_does_not_return_via_trace_or_model(
    db, admin, settings, source_factory, execution_factory
):
    import json

    from test_runtime import ScriptedModel, answer_plan, model_settings

    from app.conversation_log import read
    from app.knowledge_admin import revoke_read
    from app.preferences import history
    from app.runtime import Runtime
    from app.summary_evidence import pack
    from app.turns import accept_turn, read_turn
    from app.values import jsonb

    marker = "MEM02_OLD_COMPANY_MARKER"
    text = "以后回复简洁。"
    execution = execution_factory(text)
    saved = preference_command(
        db,
        execution,
        text,
        action="remember",
        key="response_style",
        value="简洁",
    )
    assert saved["ok"], saved
    default = saved["data"]
    admin.execute("UPDATE turns SET status='completed' WHERE turn_id=%s", (execution.turn_id,))
    admin.execute("UPDATE work_items SET state='done' WHERE work_id=%s", (execution.work_id,))
    legacy = change_preference(
        db, AUTH, action="remember", key="legacy_marker", value=marker, request_id=uid()
    )

    async def run(company, output, session_id=None, *, auth=AUTH):
        session_id = session_id or str(create_session(db, auth, company)["session_id"])
        turn = accept_turn(db, auth, session_id, uid(), "请给一句普通问候。")
        model = ScriptedModel([answer_plan(blocks=[{"type": "chat_answer", "text": output}])])
        worker = Runtime(db, model_settings(settings), model)
        await worker.run(worker.claim())
        result = read_turn(db, auth, turn["turn_id"])
        assert result["status"] == "completed", result
        return result, model.requests, session_id

    old_answer, old_requests, old_session = await run("C002", "你好 " + marker)
    assert marker in json.dumps(old_requests)
    raw = source_factory(f"请记住：仅限甲企业使用标识 {marker}。")
    refs = jsonb([{"source_id": str(raw["source_id"]), "quote": raw["content"]}])
    admin.execute(
        "UPDATE preferences SET source_refs=%s WHERE preference_id=%s",
        (refs, legacy["preference_id"]),
    )
    admin.execute(
        "UPDATE preference_revisions SET source_refs=%s WHERE preference_id=%s",
        (refs, legacy["preference_id"]),
    )
    answer_ref = admin.execute(
        "SELECT final_response_ref FROM turns WHERE turn_id=%s", (old_answer["turn_id"],)
    ).fetchone()["final_response_ref"]
    admin.execute(
        "INSERT INTO summaries(summary_id,user_id,session_id,scope,covered_sources,memory_epoch,content,status,generation) "
        "SELECT %s,user_id,%s,'C002',%s,memory_epoch,%s,'active',1 FROM users WHERE user_id='U001'",
        (
            uid(),
            old_session,
            jsonb([{"source_id": str(answer_ref), "revision": 1}]),
            pack(marker, []),
        ),
    )
    with db.transaction(readonly=True) as conn:
        with pytest.raises(AppError) as error:
            read(conn, AUTH, str(answer_ref))
        assert error.value.code == "SOURCE_INVALIDATED"
    assert [str(r["preference_id"]) for r in list_preferences(db, AUTH, "C002")] == [
        default["preference_id"]
    ]
    _, before_revoke, _ = await run("C002", "你好。", old_session)
    assert marker not in json.dumps(before_revoke)

    revoke_read(admin, AUTH.user_id, "C001", uid())
    assert marker not in read_turn(db, AUTH, old_answer["turn_id"])["final_message"]
    _, requests, _ = await run("C002", "你好。", old_session)
    assert marker not in json.dumps(requests)
    assert [r["value"] for r in list_preferences(db, AUTH, "C002")] == ["简洁"]
    revision = history(db, AUTH, str(legacy["preference_id"]))["revisions"][0]
    assert revision["redacted"] and revision["value"] is None
    _, other_requests, _ = await run(None, "你好。", auth=Identity("U002"))
    assert marker not in json.dumps(other_requests)


def test_ar02_body_and_appendix_are_independent(db, execution_factory):
    text = "以后正文简短、附录详细。"
    for value in ("简短", "详细"):
        saved = preference_command(
            db,
            execution_factory(text),
            text,
            action="remember",
            key="response_style",
            value=value,
        )
        assert saved["ok"], saved
    assert {r["value"] for r in list_preferences(db, AUTH)} == {"正文简短", "附录详细"}


def test_ar02_legacy_opposite_values_need_manual_resolution(db, admin, execution_factory):
    first = change_preference(
        db, AUTH, action="remember", key="response_style", value="简洁", request_id=uid()
    )
    second = change_preference(
        db, AUTH, action="remember", key="old_length", value="详细", request_id=uid()
    )
    admin.execute(
        "UPDATE preferences SET stable_key='answer_length' WHERE preference_id=%s",
        (second["preference_id"],),
    )
    assert not list_preferences(db, AUTH)
    candidates = list_preferences(db, AUTH, management=True)
    assert len(candidates) == 2 and all(row["needs_resolution"] for row in candidates)
    text = "更正刚才的要求，今后回复要详细。"
    rejected = preference_command(
        db,
        execution_factory(text),
        text,
        action="remember",
        key="answer_length",
        value="详细",
    )
    assert rejected["error"]["code"] == "PREFERENCE_CONFLICT", rejected
    change_preference(
        db,
        AUTH,
        action="delete",
        target_id=str(first["preference_id"]),
        expected_revision=first["revision"],
        request_id=uid(),
    )
    assert [row["value"] for row in list_preferences(db, AUTH)] == ["详细"]


def test_ar01_month_update_keeps_user_fields_and_invalidates_old_check(
    db, admin, execution_factory
):
    from test_runtime import tool

    from app.tools import ToolExecutor
    from app.values import jsonb

    fields = {"show_columns": ["税额", "含税金额"], "pending": ["待财务复核"], "legacy": "保留"}
    task = change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        request_id=uid(),
        intent="query_month",
        period="2026-09",
        unresolved=fields,
    )
    admin.execute(
        "UPDATE tasks SET unresolved_json=unresolved_json||%s, checked_data_revision=1, "
        "checked_rules_epoch=1 WHERE task_id=%s",
        (jsonb({"missing_count": 3, "needs_refresh": False}), task["task_id"]),
    )
    text = "甲企业这项任务只把月份改成2026年10月。"
    execution = execution_factory(text)
    output, _ = ToolExecutor(db).execute(
        execution,
        tool(
            "change_task",
            {
                "company_id": "C001",
                "action": "update",
                "period": "2026-10",
                "target_id": str(task["task_id"]),
                "expected_revision": task["revision"],
                "source_refs": [{"source_id": execution.source_id, "quote": text}],
            },
        )["tool_calls"][0],
    )
    assert output["ok"], output
    updated = output["data"]
    assert updated["period"] == "2026-10"
    assert updated["unresolved_json"] == {**fields, "needs_refresh": True}
    assert updated["checked_data_revision"] is None and updated["checked_rules_epoch"] is None


@pytest.mark.parametrize(
    "text,cleared",
    [("清除展示列。", True), ("清除展示列，但不要清除展示列。", False)],
)
def test_ar01_empty_patch_and_explicit_field_clear(db, execution_factory, text, cleared):
    from test_runtime import tool

    from app.tools import ToolExecutor

    initial = {"show_columns": ["税额"], "pending": ["财务复核"]}
    task = change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        request_id=uid(),
        intent="query_month",
        period="2026-09",
        unresolved=initial,
    )
    no_fields = change_task(
        db,
        AUTH,
        company_id="C001",
        action="update",
        request_id=uid(),
        target_id=str(task["task_id"]),
        expected_revision=task["revision"],
        unresolved={},
    )
    assert no_fields["unresolved_json"] == initial
    execution = execution_factory(text)
    result, _ = ToolExecutor(db).execute(
        execution,
        tool(
            "change_task",
            {
                "company_id": "C001",
                "action": "update",
                "clear_unresolved": ["show_columns"],
                "target_id": str(task["task_id"]),
                "expected_revision": no_fields["revision"],
                "source_refs": [{"source_id": execution.source_id, "quote": text}],
            },
        )["tool_calls"][0],
    )
    assert result["ok"] is cleared
    projected = load_core(db, AUTH, execution.session_id)["task"]["unresolved_json"]
    assert projected["pending"] == initial["pending"]
    assert ("show_columns" in projected) is not cleared


def test_ar01_check_keeps_user_fields_and_gets_stale_after_rule_change(db, admin):
    task = change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        request_id=uid(),
        intent="complete_purposes",
        period="2026-09",
        unresolved={"pending": ["财务复核"]},
    )
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
    checked = apply_result(db, AUTH, task, result, str(source["source_id"]), source["source_seq"])
    assert checked["unresolved_json"]["pending"] == ["财务复核"]
    assert checked["unresolved_json"]["missing_count"] == result["missing_count"]
    admin.execute("UPDATE company_heads SET rules_epoch=rules_epoch+1 WHERE company_id='C001'")
    assert list_tasks(db, AUTH, "C001")[0]["unresolved_json"] == {
        "pending": ["财务复核"],
        "needs_refresh": True,
    }
    with pytest.raises(AppError) as error:
        change_task(
            db,
            AUTH,
            company_id="C001",
            action="update",
            request_id=uid(),
            target_id=str(task["task_id"]),
            expected_revision=task["revision"],
            unresolved={"pending": ["旧并发编辑"]},
        )
    assert error.value.code == "VERSION_CONFLICT"


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
    with pytest.raises(AppError) as error:
        search_history(db, AUTH, company_id="C001", query="先列缺项")
    assert error.value.code == "HISTORY_RETRIEVAL_DISABLED"
    assert target["recall_state"] == "active"  # Records survive; online retrieval is disabled.


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
    with pytest.raises(AppError) as error:
        search_history(db, AUTH, company_id="C001", query="回答简洁")
    assert error.value.code == "HISTORY_RETRIEVAL_DISABLED"


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
