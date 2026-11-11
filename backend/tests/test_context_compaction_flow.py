import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_runtime import ScriptedModel, final, model_settings

from app import compaction_requests, turns
from app.context import assemble
from app.context_preview import preview
from app.errors import AppError
from app.identity import Identity, create_session
from app.runtime import Runtime
from app.values import uid


@pytest.mark.asyncio
async def test_compaction_preserves_history_and_fences_failures(db, settings):
    settings = model_settings(settings).model_copy(update={"input_budget": None})
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])
    runtime = Runtime(
        db, settings, ScriptedModel([final("已记录。") for _ in range(7)], auto_summary=True)
    )
    request_id = uid()
    with ThreadPoolExecutor(max_workers=2) as pool:
        both = list(
            pool.map(
                lambda _: compaction_requests.accept(db, auth, session, request_id, settings),
                range(2),
            )
        )
    assert both[0]["turn_id"] == both[1]["turn_id"]
    await runtime.run(runtime.claim())
    assert (
        compaction_requests.read(db, auth, session, both[0]["turn_id"])["result"]["outcome"]
        == "no_content"
    )
    for n in range(7):
        turn = turns.accept_turn(db, auth, session, uid(), f"第{n}轮：" + "保留项目背景。" * 100)
        await runtime.run(runtime.claim())
        assert turns.read_turn(db, auth, turn["turn_id"])["status"] == "completed"
    before = preview(db, settings, auth, session, "继续。")
    task = compaction_requests.accept(db, auth, session, uid(), settings)
    await runtime.run(runtime.claim())
    result = compaction_requests.read(db, auth, session, task["turn_id"])
    assert result["result"]["outcome"] == "succeeded"
    assert (
        preview(db, settings, auth, session, "继续。")["input_estimate"] < before["input_estimate"]
    )
    assert len(turns.session_messages(db, auth, session)["turns"]) == 7
    with db.transaction(readonly=True) as conn:
        progress = conn.execute(
            "SELECT contiguous_seq FROM source_progress WHERE user_id=%s AND purpose='memory'",
            (auth.user_id,),
        ).fetchone()["contiguous_seq"]
        assert progress == task["source_seq"]
    with pytest.raises(AppError):
        compaction_requests.read(db, Identity("U002"), session, task["turn_id"])

    class BadSummary:
        mode = "test"

        async def complete(self, payload):
            data = json.loads(payload["messages"][1]["content"])
            return {
                "message": {
                    "content": json.dumps(
                        {"summary": "🧾" * 2000, "covered_sources": data["source_ids"]}
                    )
                }
            }

    for n in range(2):
        runtime.model = ScriptedModel([final("收到")])
        turn = turns.accept_turn(db, auth, session, uid(), f"后续第{n}轮")
        await runtime.run(runtime.claim())
        assert turns.read_turn(db, auth, turn["turn_id"])["status"] == "completed"
    runtime.model = BadSummary()
    task = compaction_requests.accept(db, auth, session, uid(), settings)
    await runtime.run(runtime.claim())
    failed = compaction_requests.read(db, auth, session, task["turn_id"])
    assert failed["status"] == "failed" and failed["error"]["code"] == "COMPACTION_NO_GAIN"
    assert preview(db, settings, auth, session, "")["summary_id"] == result["result"]["summary_id"]


@pytest.mark.asyncio
async def test_pressure_starts_automatic_compaction(db, settings):
    settings = model_settings(settings).model_copy(update={"input_budget": None})
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])
    runtime = Runtime(db, settings, ScriptedModel([final("已记录。") for _ in range(7)]))
    for n in range(7):
        turn = turns.accept_turn(db, auth, session, uid(), f"第{n}轮：" + "保留项目背景。" * 100)
        await runtime.run(runtime.claim())
        assert turns.read_turn(db, auth, turn["turn_id"])["status"] == "completed"
    turn = turns.accept_turn(db, auth, session, uid(), "继续讨论。")
    execution = runtime.claim()
    _, manifest, _ = assemble(db, settings, execution, [], proactive_compact=False)
    runtime.settings = settings.model_copy(update={"input_budget": manifest["token_count"] + 500})
    runtime.model = ScriptedModel([final("继续讨论。")], auto_summary=True)
    await runtime.run(execution)
    assert turns.read_turn(db, auth, turn["turn_id"])["status"] == "completed"
    with db.transaction(readonly=True) as conn:
        assert conn.execute(
            "SELECT 1 FROM run_events WHERE turn_id=%s AND kind='context_compacted'",
            (turn["turn_id"],),
        ).fetchone()
