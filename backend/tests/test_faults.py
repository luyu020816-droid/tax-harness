import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from test_mutations import request
from test_runtime import ScriptedModel, final, model_settings, tool

from app.config import ROOT
from app.context import assemble
from app.db import Database
from app.errors import AppError
from app.execution import guard
from app.identity import Identity, create_session
from app.memory import load_core, search_history
from app.mutations import update_invoice
from app.preferences import change_preference
from app.runtime import Runtime
from app.sources import advance_progress, assess
from app.turns import accept_turn, read_turn
from app.values import uid

AUTH = Identity("U001")


@pytest.mark.asyncio
async def test_F04_reopen_pool_recovers_acknowledged_outbox(db, settings, tmp_path):
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, str(session["session_id"]), uid(), "查询甲企业2026年9月资料")
    receipt = tmp_path / "independent-receipt.json"
    receipt.write_text(
        json.dumps({"turn_id": turn["turn_id"], "text": turn["text"]}), encoding="utf-8"
    )
    db.close()
    restarted = Database(settings.test_database_url)
    restarted.open()
    try:
        runtime = Runtime(
            restarted,
            model_settings(settings),
            ScriptedModel(
                [
                    tool("query_month", {"company_id": "C001", "period": "2026-09"}),
                    final("结果见卡片。"),
                ]
            ),
        )
        execution = runtime.claim()
        assert execution.turn_id == json.loads(receipt.read_text())["turn_id"]
        await runtime.run(execution)
        result = read_turn(restarted, AUTH, execution.turn_id)
        assert result["status"] == "completed"
        assert result["final_cards"][0]["invoice_total"] == "9622.06"
    finally:
        restarted.close()


def test_F05_F06_expired_lease_cannot_duplicate_committed_write(db, settings, admin):
    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, str(session["session_id"]), uid(), "甲企业F002用途填办公资料整理")
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]))
    old = runtime.claim()
    with db.transaction(readonly=True) as conn:
        source = conn.execute(
            "SELECT * FROM sources WHERE source_id=%s", (old.source_id,)
        ).fetchone()
    first = update_invoice(db, AUTH, **request(source), execution=old)
    admin.execute(
        "UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second' WHERE work_id=%s",
        (old.work_id,),
    )
    new = Runtime(db, runtime.settings, ScriptedModel([])).claim()
    assert new.turn_id == turn["turn_id"] and new.fencing_token > old.fencing_token
    with pytest.raises(AppError), db.transaction() as conn:
        guard(conn, old)
    replay = update_invoice(db, AUTH, **request(source), execution=new)
    assert str(replay["operation_id"]) == str(first["operation_id"])
    assert replay["replayed"]
    assert (
        admin.execute(
            "SELECT revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()["revision"]
        == 2
    )


def test_F07_concurrent_preference_changes_have_one_winner(db):
    pref = change_preference(
        db, AUTH, action="remember", key="response_style", value="简洁", request_id=uid()
    )

    def update(value):
        try:
            change_preference(
                db,
                AUTH,
                action="update",
                target_id=str(pref["preference_id"]),
                expected_revision=pref["revision"],
                value=value,
                request_id=uid(),
            )
            return "success"
        except AppError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(update, ["详细说明", "分步说明"]))
    assert sorted(outcomes) == ["VERSION_CONFLICT", "success"]
    with db.transaction(readonly=True) as conn:
        rows = conn.execute(
            "SELECT revision FROM preferences WHERE user_id='U001' AND status='active'"
        ).fetchall()
    assert rows == [{"revision": 2}]


def test_F20_history_index_failure_does_not_erase_core(db, admin):
    change_preference(
        db, AUTH, action="remember", key="response_style", value="保留依据", request_id=uid()
    )
    session = create_session(db, AUTH, "C001")
    admin.execute("ALTER TABLE history_index RENAME TO history_index_unavailable")
    try:
        assert (
            load_core(db, AUTH, str(session["session_id"]))["preferences"][0]["value"] == "保留依据"
        )
        with pytest.raises(psycopg.errors.UndefinedTable):
            search_history(db, AUTH, company_id="C001", query="依据")
    finally:
        admin.execute("ALTER TABLE history_index_unavailable RENAME TO history_index")


def test_F21_rebuild_index_excludes_deleted_memory(
    db, admin, settings, source_factory, monkeypatch
):
    source_factory("独立可召回的业务背景")
    pref = change_preference(
        db, AUTH, action="remember", key="response_style", value="简洁回答", request_id=uid()
    )
    change_preference(
        db,
        AUTH,
        action="delete",
        target_id=str(pref["preference_id"]),
        expected_revision=pref["revision"],
        request_id=uid(),
    )
    admin.execute("DELETE FROM history_index")
    spec = importlib.util.spec_from_file_location(
        "rebuild_history", ROOT / "scripts/rebuild_history.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "Settings",
        lambda: settings.model_copy(
            update={
                "admin_database_url": make_conninfo(
                    settings.admin_database_url, dbname="tax_harness_test"
                )
            }
        ),
    )
    module.main()
    assert search_history(db, AUTH, company_id="C001", query="独立可召回")["matches"]
    assert search_history(db, AUTH, company_id="C001", query="简洁回答")["not_found"]


def test_F28_source_progress_keeps_earlier_gap(db, admin, source_factory):
    first = source_factory("尚未处理来源")
    second = source_factory("独立已处理来源")
    with db.transaction() as conn:
        assess(conn, str(second["source_id"]), "no_change")
        advance_progress(conn, AUTH)
    progress = admin.execute("SELECT * FROM source_progress WHERE user_id='U001'").fetchone()
    assert progress["contiguous_seq"] == 0 and first["source_seq"] in progress["unresolved_refs"]
    with db.transaction() as conn:
        assess(conn, str(first["source_id"]), "no_change")
        advance_progress(conn, AUTH)
    assert (
        admin.execute("SELECT contiguous_seq FROM source_progress WHERE user_id='U001'").fetchone()[
            "contiguous_seq"
        ]
        == second["source_seq"]
    )


def test_F32_changed_source_stops_before_model_input(db, settings, admin, execution_factory):
    execution = execution_factory("原始用户请求")
    admin.execute(
        "UPDATE sources SET content='非原始内容' WHERE source_id=%s", (execution.source_id,)
    )
    with pytest.raises(AppError) as error:
        assemble(db, model_settings(settings), execution, [])
    assert error.value.code == "STATE_SOURCE_UNAVAILABLE"
