from types import SimpleNamespace

import pytest
from test_model_connection import FORM, LOCAL, URL, browser, make_app, probe_reply
from test_purpose_task_recovery import CONFIRMABLE
from test_trusted_writes import candidate

from app import invoice_write
from app.agent_models import PersistentAgentModels
from app.errors import AppError
from app.identity import Identity, create_session
from app.model import HttpModel
from app.model_connection import ModelConnection
from app.turns import accept_turn, read_turn
from app.values import uid


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


def test_ag01_restarted_worker_does_not_replace_a_missing_page_connection(db, admin, monkeypatch):
    async def probe(self, payload):
        assert payload["tools"][0]["function"]["name"] == "connection_probe"
        return probe_reply()

    monkeypatch.setattr(HttpModel, "complete", probe)
    auth = Identity("U001")
    app = make_app(db)
    with browser(app) as client:
        activated = client.post(URL + "/test", json=FORM, headers={"Origin": LOCAL}).json()
        session = str(create_session(db, auth, "C001")["session_id"])
        old = accept_turn(db, auth, session, uid(), "请写一句问候。")
        assert old["default_model_version"] == activated["version"]
    # A second process has a working .env connection but lacks this page key.
    restarted = make_app(db)
    with browser(restarted) as client:
        metadata = client.get(URL).json()
        assert metadata["model_id"] == FORM["model_id"]
        assert metadata["version"] == activated["version"]
        assert not metadata["configured"] and metadata["env_available"]
        assert restarted.state.runtime.claim() is None
        failed = read_turn(db, auth, old["turn_id"])
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "DEFAULT_MODEL_VERSION_UNAVAILABLE"
        assert failed["error"]["details"]["model_profile_version"] == activated["version"]
        assert not failed["runs"]
        env = client.post(URL + "/test", json={}, headers={"Origin": LOCAL}).json()
        assert env["version"] > activated["version"] and env["source"] == "env"
        legacy = accept_turn(db, auth, session, uid(), "请写另一句问候。")
        admin.execute(
            "UPDATE turns SET default_model_version=NULL WHERE turn_id=%s", (legacy["turn_id"],)
        )
        assert restarted.state.runtime.claim() is None
        assert read_turn(db, auth, legacy["turn_id"])["error"]["code"] == "DEFAULT_MODEL_UNBOUND"
    rows = admin.execute("SELECT * FROM default_model_versions ORDER BY version").fetchall()
    assert [row["source"] for row in rows] == ["env", "page", "env"]
    assert FORM["api_key"] not in str(rows) and "env-fixture-key" not in str(rows)


@pytest.mark.asyncio
async def test_ag01_known_confirmation_remains_a_model_free_control_after_restart(db, monkeypatch):
    from app.trace import read_run

    async def probe(self, payload):
        assert payload["tools"][0]["function"]["name"] == "connection_probe"
        return probe_reply()

    monkeypatch.setattr(HttpModel, "complete", probe)
    auth = Identity("U001")
    app = make_app(db)
    with browser(app) as client:
        version = client.post(URL + "/test", json=FORM, headers={"Origin": LOCAL}).json()["version"]
        session = str(create_session(db, auth, "C001")["session_id"])
        accept_turn(db, auth, session, uid(), CONFIRMABLE)
        runtime = app.state.runtime
        ex = runtime.claim()
        plan = invoice_write.prepare(db, auth, **candidate(ex, CONFIRMABLE))
        runtime.fail(ex, AppError("RUN_LIMIT_EXCEEDED", "测试结束原轮。", 503))
        accepted = invoice_write.confirm(
            db,
            auth,
            plan["plan_id"],
            expected_revision=plan["revision"],
            intent_hash=plan["intent_hash"],
            request_id=uid(),
        )
    restarted = make_app(db)
    with browser(restarted):
        runtime = restarted.state.runtime
        control = runtime.claim()
        assert control.turn_id == accepted["turn_id"]
        await runtime.run(control)
        assert read_turn(db, auth, control.turn_id)["status"] == "completed"
        trace = read_run(db, auth, control.run_id)
        assert trace["model_binding"] == {
            "profile_id": "default",
            "version": version,
            "model_id": FORM["model_id"],
            "control_only": True,
        }
