import pytest
from fastapi.testclient import TestClient
from test_insights import context
from test_runtime import ScriptedModel, model_settings

from app.api import create_app
from app.context_preview import actual_usage
from app.errors import AppError
from app.identity import Identity, create_session
from app.values import jsonb


def test_no_call_is_zero_and_sessions_and_users_are_isolated(db, settings, execution_factory):
    settings = model_settings(settings).model_copy(update={"model_id": "test-model"})
    empty = str(create_session(db, Identity("U001"), "C001")["session_id"])
    ex = execution_factory("测试实际模型用量")
    context(db, ex, {"prompt_tokens": 12000, "completion_tokens": 50})
    assert actual_usage(db, settings, Identity("U001"), empty)["input_tokens"] == 0
    measured = actual_usage(db, settings, Identity("U001"), ex.session_id)
    assert measured["status"] == "measured"
    assert measured["input_tokens"] == 12000
    assert measured["effective_window"] == 64000
    with pytest.raises(AppError) as error:
        actual_usage(db, settings, Identity("U002"), ex.session_id)
    assert error.value.status == 404


def test_latest_real_input_not_sum_excludes_compaction_and_missing_is_unknown(
    db, settings, execution_factory
):
    settings = model_settings(settings).model_copy(update={"model_id": "test-model"})
    ex = execution_factory("计量隔离")
    context(db, ex, {"prompt_tokens": 7000, "completion_tokens": 10})
    context(db, ex, {"prompt_tokens": 9000, "completion_tokens": 20})
    context(db, ex, {"prompt_tokens": 40000, "completion_tokens": 30}, kind="compaction")
    result = actual_usage(db, settings, ex.auth, ex.session_id)
    assert result["input_tokens"] == 9000
    context(db, ex)
    result = actual_usage(db, settings, ex.auth, ex.session_id)
    assert result["status"] == "unknown" and result["input_tokens"] is None


def test_invalid_usage_and_changed_model_never_fabricate_percentage(
    db, settings, execution_factory
):
    ex = execution_factory("检查缺失数据")
    cid, _ = context(db, ex, {"prompt_tokens": True, "completion_tokens": 1})
    settings = model_settings(settings).model_copy(update={"model_id": "test-model"})
    assert actual_usage(db, settings, ex.auth, ex.session_id)["status"] == "unknown"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE context_snapshots SET usage=%s WHERE context_id=%s",
            (jsonb({"prompt_tokens": 2500}), cid),
        )
    changed = actual_usage(
        db, settings.model_copy(update={"model_id": "another-model"}), ex.auth, ex.session_id
    )
    assert changed["input_tokens"] == 2500
    assert changed["effective_window"] is None


def test_empty_session_usage_api(db, settings):
    with TestClient(
        create_app(model_settings(settings), db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        assert client.post("/api/v1/demo/login", json={"user_id": "U001"}).status_code == 200
        session = client.post("/api/v1/sessions", json={"selected_company_id": "C001"}).json()[
            "session_id"
        ]
        response = client.get(f"/api/v1/sessions/{session}/context-usage")
        assert response.status_code == 200
        assert response.json()["status"] == "no_calls"
        assert response.json()["input_tokens"] == 0
