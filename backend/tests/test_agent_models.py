from types import SimpleNamespace

import pytest

from app.agent_models import AgentModels
from app.errors import AppError
from app.model_connection import ModelConnection


def complete(coroutine):
    # The fake connection completes without I/O or yielding to an event loop.
    with pytest.raises(StopIteration) as result:
        coroutine.send(None)
    return result.value.value


def test_model_profiles_are_independent_and_failed_update_keeps_previous(monkeypatch):
    async def test(self, form):
        if form == "bad":
            raise AppError("MODEL_TEST_FAILED", "测试失败。", 422)
        self.current = (SimpleNamespace(model_configured=True, model_id=form), object())
        self.test_status = "success"

    def status(self):
        return {
            "configured": True,
            "model_id": self.current[0].model_id,
            "test_status": self.test_status,
        }

    monkeypatch.setattr(ModelConnection, "test_and_enable", test)
    monkeypatch.setattr(ModelConnection, "status", status)
    models = AgentModels(SimpleNamespace(model_configured=False))
    assert complete(models.test_and_enable("a", 0, "first"))["version"] == 1
    complete(models.test_and_enable("b", 0, "second"))
    bound = models.resolve("a", 1)
    with pytest.raises(AppError, match="测试失败"):
        complete(models.test_and_enable("a", 1, "bad"))
    assert models.resolve("a", 1) == bound
    assert models.resolve("b", 1)[0].model_id == "second"
    complete(models.test_and_enable("a", 1, "new"))
    assert bound[0].model_id == "first"
    assert models.resolve("a", 2)[0].model_id == "new"
    assert models.resolve("a", 1) == bound


def test_model_profile_version_conflict_and_restart_do_not_fall_back(monkeypatch):
    async def test(self, form):
        self.current = (SimpleNamespace(model_configured=True, model_id=form), object())

    monkeypatch.setattr(ModelConnection, "test_and_enable", test)
    monkeypatch.setattr(ModelConnection, "status", lambda self: {"configured": True})
    models = AgentModels(SimpleNamespace(model_configured=False))
    complete(models.test_and_enable("model_a", 0, "first"))
    with pytest.raises(AppError, match="已变化"):
        complete(models.test_and_enable("model_a", 0, "second"))
    with pytest.raises(AppError, match="不可用"):
        AgentModels(SimpleNamespace(model_configured=False)).resolve("model_a", 1)
