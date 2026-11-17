from types import SimpleNamespace

import pytest

from app.agent_models import PersistentAgentModels
from app.errors import AppError
from app.identity import Identity
from app.model_connection import ModelConnection


def complete(coroutine):
    with pytest.raises(StopIteration) as result:
        coroutine.send(None)
    return result.value.value


def test_model_metadata_versions_survive_restart_but_keys_do_not(db, monkeypatch):
    async def probe(self, form):
        if form["model"] == "failure":
            raise AppError("MODEL_TEST_FAILED", "测试失败。", 422)
        self.current = (
            SimpleNamespace(
                model_configured=True,
                model_id=form["model"],
                model_base_url="https://provider.example/v1",
                model_api_key=form["key"],
            ),
            object(),
        )

    monkeypatch.setattr(ModelConnection, "test_and_enable", probe)
    monkeypatch.setattr(
        ModelConnection,
        "status",
        lambda self: {"configured": True, "model_id": self.current[0].model_id},
    )
    auth = Identity("U001")
    service = PersistentAgentModels(db, SimpleNamespace(model_configured=False))
    form = {"model": "model-a", "key": "fixture-secret"}
    first = complete(service.test_and_enable(auth, "a", "模型 A", 0, form))
    assert first["version"] == 1 and "fixture-secret" not in str(first)
    bound = service.resolve("a", 1)
    complete(service.test_and_enable(auth, "b", "模型 B", 0, {**form, "model": "model-b"}))
    with pytest.raises(AppError, match="测试失败"):
        complete(service.test_and_enable(auth, "a", "模型 A", 1, {**form, "model": "failure"}))
    assert service.resolve("a", 1) == bound
    with db.transaction(readonly=True) as conn:
        stored = conn.execute("SELECT * FROM agent_model_profiles WHERE profile_id='a'").fetchone()
        events = conn.execute(
            "SELECT action,revision FROM agent_capability_events WHERE kind='model' "
            "AND capability_id='a' ORDER BY revision"
        ).fetchall()
    assert stored["version"] == 1 and "fixture-secret" not in str(stored)
    assert events == [{"action": "publish", "revision": 1}]
    restarted = PersistentAgentModels(db, SimpleNamespace(model_configured=False))
    assert not next(p for p in restarted.list_profiles(auth) if p["profile_id"] == "a")[
        "configured"
    ]
    with pytest.raises(AppError, match="不可用"):
        restarted.resolve("a", 1)
    second = complete(restarted.test_and_enable(auth, "a", "模型 A", 1, form))
    assert second["version"] == 2
    with pytest.raises(AppError, match="不可用"):
        restarted.resolve("a", 1)
    with pytest.raises(AppError, match="没有 Agent"):
        complete(restarted.test_and_enable(Identity("U002"), "a", "模型 A", 2, form))
