import json

import pytest
from test_compaction import closed_group
from test_runtime import model_settings

from app.compaction import compact
from app.context import assemble, validate
from app.errors import AppError


@pytest.mark.asyncio
@pytest.mark.parametrize("changes_text", [False, True])
async def test_only_known_prior_label_omission_gets_immutable_text_repair(
    db, source_factory, execution_factory, settings, admin, changes_text
):
    calls = []

    class Model:
        async def complete(self, payload):
            data = json.loads(payload["messages"][1]["content"])
            calls.append(data)
            if "required_sources" in data:
                summary = "被改动的正文" if changes_text else data["candidate_summary"]
                ids = data["required_sources"]
            else:
                summary = "累计导航，禁止猜测用途。"
                ids = [ref["source_id"] for g in data["closed_turns"] for ref in g["sources"]]
            return {
                "message": {"content": json.dumps({"summary": summary, "covered_sources": ids})}
            }

    model = Model()
    first = closed_group(db, source_factory("最早记录"))
    ex = execution_factory("继续")
    await compact(db, model_settings(settings), model, ex, [first], 1)
    old = admin.execute("SELECT summary_id FROM summaries WHERE status='active'").fetchone()[
        "summary_id"
    ]
    second = closed_group(db, source_factory("后来记录"))
    if changes_text:
        with pytest.raises(AppError) as error:
            await compact(db, model_settings(settings), model, ex, [second], 2)
        assert error.value.code == "SUMMARY_INVALID"
        assert (
            admin.execute("SELECT summary_id FROM summaries WHERE status='active'").fetchone()[
                "summary_id"
            ]
            == old
        )
    else:
        assert await compact(db, model_settings(settings), model, ex, [second], 2)
        current = admin.execute("SELECT * FROM summaries WHERE status='active'").fetchone()
        assert {r["source_id"] for r in current["covered_sources"]} == {
            first["sources"][0]["source_id"],
            second["sources"][0]["source_id"],
        }
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_summary_sources_are_checked_again_before_final_publish(
    db, source_factory, execution_factory, settings, admin
):
    from test_compaction import summary_message
    from test_runtime import ScriptedModel

    source = source_factory("未在最近窗口中的历史来源")
    group = closed_group(db, source)
    execution = execution_factory("继续")
    await compact(
        db,
        model_settings(settings),
        ScriptedModel([summary_message(source)]),
        execution,
        [group],
        1,
    )
    payload, manifest, _ = assemble(db, model_settings(settings), execution, [])
    assert manifest["summary_id"]
    admin.execute(
        "update sources set revision=revision+1 where source_id=%s", (source["source_id"],)
    )
    with pytest.raises(AppError) as error:
        validate(db, execution, payload, manifest)
    assert error.value.code == "SOURCE_INVALIDATED"
    _, fresh, _ = assemble(db, model_settings(settings), execution, [])
    assert fresh["summary_id"] is None


@pytest.mark.asyncio
async def test_oversized_history_is_compacted_in_bounded_batches(
    db, source_factory, execution_factory, settings, admin
):
    groups = [
        closed_group(db, source_factory(f"记录{i}：" + "合成资料，不授权修改。" * 120))
        for i in range(12)
    ]
    calls = []

    class Model:
        async def complete(self, payload):
            from app.context import estimate_tokens

            assert estimate_tokens(payload) <= model_settings(settings).context_budget
            calls.append(payload)
            data = json.loads(payload["messages"][1]["content"])
            return {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "有来源的合成导航摘要，未执行修改。",
                            "covered_sources": data["source_ids"],
                        }
                    )
                }
            }

    assert await compact(
        db, model_settings(settings), Model(), execution_factory("继续"), groups, 1
    )
    assert len(calls) > 1
    row = admin.execute("select * from summaries where status='active'").fetchone()
    assert len(row["covered_sources"]) == 12
    assert all("合成资料" in p["messages"][1]["content"] for p in calls)


@pytest.mark.asyncio
async def test_new_summary_merges_existing_covered_sources(
    db, source_factory, execution_factory, settings, admin
):
    class Model:
        async def complete(self, payload):
            data = json.loads(payload["messages"][1]["content"])
            return {
                "message": {
                    "content": json.dumps(
                        {"summary": "累计历史导航", "covered_sources": data["source_ids"]}
                    )
                }
            }

    first = closed_group(db, source_factory("最初的必要记录"))
    execution = execution_factory("继续")
    assert await compact(db, model_settings(settings), Model(), execution, [first], 1)
    second = closed_group(db, source_factory("后来补充的必要记录"))
    assert await compact(db, model_settings(settings), Model(), execution, [second], 2)
    row = admin.execute("select * from summaries where status='active'").fetchone()
    assert {r["source_id"] for r in row["covered_sources"]} == {
        first["sources"][0]["source_id"],
        second["sources"][0]["source_id"],
    }


@pytest.mark.asyncio
async def test_later_batch_failure_never_publishes_partial_summary(
    db, source_factory, execution_factory, settings, admin
):
    groups = [closed_group(db, source_factory("合成记录。" * 500)) for _ in range(12)]
    calls = 0

    class Model:
        async def complete(self, payload):
            nonlocal calls
            calls += 1
            data = json.loads(payload["messages"][1]["content"])
            return {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "候选导航",
                            "covered_sources": data["source_ids"] if calls == 1 else [],
                        }
                    )
                }
            }

    with pytest.raises(AppError) as error:
        await compact(db, model_settings(settings), Model(), execution_factory("继续"), groups, 1)
    assert error.value.code == "SUMMARY_INVALID"
    assert calls == 2
    assert admin.execute("select count(*) n from summaries").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_single_oversized_group_is_not_silently_truncated(
    db, source_factory, execution_factory, settings
):
    group = closed_group(db, source_factory("完整不可拆组。" * 5000))

    class Model:
        async def complete(self, payload):
            raise AssertionError("Oversized request must not reach the model")

    with pytest.raises(AppError) as error:
        await compact(db, model_settings(settings), Model(), execution_factory("继续"), [group], 1)
    assert error.value.code == "CONTEXT_CAPACITY_EXCEEDED"


@pytest.mark.asyncio
async def test_compaction_and_agent_share_one_model_call_budget(db, settings, monkeypatch):
    from app import compaction as summaries
    from app import context as contexts
    from app.identity import Identity, create_session
    from app.runtime import Runtime
    from app.turns import accept_turn, read_turn
    from app.values import uid

    settings = model_settings(settings).model_copy(update={"max_model_steps": 2})
    auth = Identity("U001")
    session = create_session(db, auth, "C001")
    accepted = accept_turn(db, auth, str(session["session_id"]), uid(), "仅查询，不修改。")
    actual = contexts.assemble

    def force_compaction(*args, **kwargs):
        payload, manifest, _ = actual(*args, **kwargs)
        return payload, manifest, [{}]

    monkeypatch.setattr(contexts, "assemble", force_compaction)

    async def spend_summary_budget(db, settings, model, execution, groups, step):
        await model.complete({})
        await model.complete({})
        return True

    monkeypatch.setattr(summaries, "compact", spend_summary_budget)

    class Model:
        mode = "test"
        calls = 0

        async def complete(self, payload):
            self.calls += 1
            return {"message": {"content": "{}"}}

    model = Model()
    runtime = Runtime(db, settings, model)
    execution = runtime.claim()
    await runtime.run(execution)
    result = read_turn(db, auth, accepted["turn_id"])
    assert model.calls == 2
    assert result["error"]["code"] == "RUN_LIMIT_EXCEEDED"
