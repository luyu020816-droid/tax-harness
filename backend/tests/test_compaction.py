import json

import pytest
from test_runtime import ScriptedModel, model_settings

from app.compaction import compact
from app.context import assemble, validate_tool_pairs
from app.errors import AppError
from app.preferences import change_preference
from app.sources import assess


def closed_group(db, source):
    with db.transaction() as conn:
        assess(conn, str(source["source_id"]), "no_change")
    return {
        "turn_id": "historical",
        "sources": [{"source_id": str(source["source_id"]), "revision": 1}],
        "messages": [
            {"role": "user", "content": source["content"]},
            {"role": "assistant", "content": "收到。"},
        ],
    }


def summary_message(source):
    return {
        "role": "assistant",
        "content": json.dumps(
            {
                "summary": "用户曾咨询甲企业资料；没有授权修改。",
                "covered_sources": [str(source["source_id"])],
            },
            ensure_ascii=False,
        ),
    }


@pytest.mark.asyncio
async def test_M11_summary_is_derived_and_original_remains(
    db, settings, admin, source_factory, execution_factory
):
    source = source_factory("咨询甲企业资料，不修改任何字段")
    group = closed_group(db, source)
    execution = execution_factory("继续查询")
    assert await compact(
        db,
        model_settings(settings),
        ScriptedModel([summary_message(source)]),
        execution,
        [group],
        1,
    )
    assert (
        admin.execute(
            "SELECT count(*) AS n FROM sources WHERE source_id=%s", (source["source_id"],)
        ).fetchone()["n"]
        == 1
    )
    payload, manifest, _ = assemble(db, model_settings(settings), execution, [])
    assert manifest["summary_id"]
    assert "历史导航摘要" in payload["messages"][1]["content"]


@pytest.mark.asyncio
async def test_F17_invalid_summary_does_not_replace_previous(
    db, settings, admin, source_factory, execution_factory
):
    source = source_factory("查询甲企业资料")
    group = closed_group(db, source)
    execution = execution_factory("继续")
    good = ScriptedModel([summary_message(source)])
    await compact(db, model_settings(settings), good, execution, [group], 1)
    previous = admin.execute("SELECT summary_id FROM summaries WHERE status='active'").fetchone()[
        "summary_id"
    ]
    with pytest.raises(AppError, match="摘要格式"):
        await compact(
            db,
            model_settings(settings),
            ScriptedModel([{"content": '{"summary":"凭空编造","covered_sources":[]}'}]),
            execution,
            [group],
            2,
        )
    assert (
        admin.execute("SELECT summary_id FROM summaries WHERE status='active'").fetchone()[
            "summary_id"
        ]
        == previous
    )


@pytest.mark.asyncio
async def test_F16_unprocessed_source_blocks_compaction(
    db, settings, admin, source_factory, execution_factory
):
    source = source_factory("尚未处理的明确偏好")
    execution = execution_factory("继续")
    group = {
        "sources": [{"source_id": str(source["source_id"]), "revision": 1}],
        "messages": [{"role": "user", "content": source["content"]}],
    }
    model = ScriptedModel([])
    with pytest.raises(AppError) as error:
        await compact(db, model_settings(settings), model, execution, [group], 1)
    assert error.value.code == "SOURCE_PROCESSING_PENDING"
    assert not model.requests
    assert admin.execute("SELECT count(*) AS n FROM summaries").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_F34_epoch_change_rejects_inflight_summary(
    db, settings, admin, source_factory, execution_factory
):
    source = source_factory("此前资料")
    group = closed_group(db, source)
    execution = execution_factory("继续")

    def change_during_request(payload):
        from app.identity import Identity
        from app.values import uid

        change_preference(
            db,
            Identity("U001"),
            action="remember",
            key="reply_style",
            value="详细",
            request_id=uid(),
        )
        return summary_message(source)

    with pytest.raises(AppError):
        await compact(
            db,
            model_settings(settings),
            ScriptedModel([change_during_request]),
            execution,
            [group],
            1,
        )
    assert admin.execute("SELECT count(*) AS n FROM summaries").fetchone()["n"] == 0


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "tool", "tool_call_id": "orphan"}],
        [{"role": "assistant", "tool_calls": [{"id": "a"}]}],
        [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "a"}]}],
    ],
)
def test_F18_tool_groups_cannot_be_split(messages):
    with pytest.raises(AppError) as error:
        validate_tool_pairs(messages)
    assert error.value.code == "CONTEXT_PROTOCOL_INVALID"
