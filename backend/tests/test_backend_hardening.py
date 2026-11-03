"""Authorization, session revocation and cross-process admission contracts."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from test_runtime import ScriptedModel, model_settings

from app import identity, turns
from app.api import create_app
from app.errors import AppError
from app.execution import guard
from app.identity import Identity, create_session
from app.runtime import Runtime
from app.values import uid

AUTH = Identity("U001")


@pytest.mark.parametrize("demo_mode", [True, False])
def test_production_cannot_start_with_simulation_identity(settings, demo_mode):
    with pytest.raises(AppError) as error:
        create_app(
            settings.model_copy(update={"deployment_mode": "production", "demo_mode": demo_mode})
        )
    assert error.value.code == "PRODUCTION_AUTH_REQUIRED"


def test_logout_revokes_cookie_and_switching_user_revokes_previous_token(db, settings):
    with TestClient(
        create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        assert client.post("/api/v1/demo/login", json={"user_id": "U001"}).status_code == 200
        old = dict(client.cookies)
        assert client.get("/api/v1/me").headers["cache-control"] == "no-store"
        assert client.post("/api/v1/demo/login", json={"user_id": "U002"}).status_code == 200
        switched = dict(client.cookies)
        client.cookies.clear()
        client.cookies.update(old)
        assert client.get("/api/v1/me").status_code == 401
        client.cookies.clear()
        client.cookies.update(switched)
        assert client.post("/api/v1/auth/logout").status_code == 204
        client.cookies.update(switched)
        assert client.get("/api/v1/me").status_code == 401


def test_logout_all_and_disabled_account_are_server_enforced(db, admin, execution_factory):
    a = identity.login(db, "U001")
    b = identity.login(db, "U001")
    auth = identity.authenticate(db, a)
    identity.logout_all(db, auth)
    for token in (a, b):
        with pytest.raises(AppError):
            identity.authenticate(db, token)
    token = identity.login(db, "U001")
    ex = execution_factory("当前任务")
    admin.execute("UPDATE users SET is_active=false WHERE user_id='U001'")
    with pytest.raises(AppError):
        identity.authenticate(db, token)
    with pytest.raises(AppError), db.transaction() as conn:
        guard(conn, ex)
    with pytest.raises(AppError):
        identity.login(db, "U001")


def test_sse_rechecks_revocation_before_sending_new_private_events(
    db, settings, admin, execution_factory, monkeypatch
):
    ex = execution_factory("事件流隔离测试")
    original = identity.assert_login_active
    reads = 0

    def revoke_after_first_check(conn, auth):
        nonlocal reads
        original(conn, auth)
        reads += 1
        if reads == 1:
            admin.execute(
                "UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s",
                (auth.token_hash,),
            )
            admin.execute(
                "INSERT INTO run_events(turn_id,event_seq,run_id,kind,payload) VALUES(%s,1,%s,'private_probe','{\"secret\":\"AFTER_REVOKE_SECRET\"}')",
                (ex.turn_id, ex.run_id),
            )

    monkeypatch.setattr(identity, "assert_login_active", revoke_after_first_check)
    with TestClient(
        create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        response = client.get(f"/api/v1/turns/{ex.turn_id}/events")
        assert "access_revoked" in response.text
        assert "AFTER_REVOKE_SECRET" not in response.text


def test_revoked_scope_hides_recent_archived_pending_and_mutation_responses(
    db, settings, admin, execution_factory
):
    ex = execution_factory("乙企业SECRET_TITLE_381", company="C002")
    admin.execute(
        'UPDATE turns SET status=\'waiting_user\',pending_clarification=\'{"state":"waiting_user","summary":"SECRET_TITLE_381"}\' WHERE turn_id=%s',
        (ex.turn_id,),
    )
    admin.execute(
        "UPDATE user_company_grants SET can_read=false,can_update=false,can_upload_knowledge=false,can_import_invoices=false WHERE user_id='U001' AND company_id='C002'"
    )
    with TestClient(
        create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        for path in (
            "/sessions",
            "/sessions/history",
            "/sessions/history?archived=true",
            "/pending-clarifications",
        ):
            response = client.get("/api/v1" + path)
            assert response.status_code == 200
            assert "SECRET_TITLE_381" not in response.text
        assert client.get(f"/api/v1/turns/{ex.turn_id}").status_code in (403, 404)
        assert (
            client.post(
                f"/api/v1/sessions/{ex.session_id}/organization",
                json={"action": "delete", "expected_revision": 1},
            ).status_code
            == 404
        )


@pytest.mark.parametrize("same_user", [True, False])
def test_concurrent_admission_cannot_overfill_queue_and_replay_still_works(
    db, settings, admin, same_user
):
    config = settings.model_copy(update={"max_pending_per_user": 1, "max_pending_global": 1})
    session = str(create_session(db, AUTH, "C001")["session_id"])
    second_auth = AUTH if same_user else Identity("U002")
    second_session = (
        session if same_user else str(create_session(db, second_auth, "C001")["session_id"])
    )
    barrier = Barrier(2)

    def enqueue(item):
        index, request_id = item
        actor, session_id = (AUTH, session) if index == 0 else (second_auth, second_session)
        barrier.wait(timeout=10)
        try:
            return turns.accept_turn(db, actor, session_id, request_id, "甲企业查询", limits=config)
        except AppError as error:
            return error

    ids = [uid(), uid()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, enumerate(ids)))
    accepted = [r for r in results if isinstance(r, dict)]
    assert len(accepted) == 1
    assert [r.status for r in results if isinstance(r, AppError)] == [429]
    replay = turns.accept_turn(
        db,
        Identity(accepted[0]["user_id"]),
        accepted[0]["session_id"],
        accepted[0]["client_request_id"],
        "甲企业查询",
        limits=config,
    )
    assert replay["turn_id"] == accepted[0]["turn_id"]
    assert admin.execute("SELECT count(*) n FROM turns").fetchone()["n"] == 1


def test_rate_limit_returns_429_and_retry_after(db, settings, admin):
    config = settings.model_copy(update={"requests_per_minute": 1})
    with TestClient(
        create_app(config, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        session = client.post("/api/v1/sessions", json={"selected_company_id": "C001"}).json()[
            "session_id"
        ]
        path = f"/api/v1/sessions/{session}/turns"
        assert (
            client.post(path, json={"client_request_id": uid(), "text": "甲企业请求一"}).status_code
            == 202
        )
        admin.execute("UPDATE turns SET status='completed'")
        response = client.post(path, json={"client_request_id": uid(), "text": "甲企业请求二"})
        assert response.status_code == 429 and response.headers["retry-after"] == "5"


def test_worker_global_cap_and_oldest_user_queue_order(db, settings, admin):
    b = str(create_session(db, Identity("U002"), "C001")["session_id"])
    a = str(create_session(db, AUTH, "C001")["session_id"])
    old = turns.accept_turn(db, Identity("U002"), b, uid(), "甲企业较早排队")
    turns.accept_turn(db, AUTH, a, uid(), "甲企业后来排队")
    admin.execute(
        "UPDATE sources SET created_at=clock_timestamp()-interval '1 minute' WHERE turn_id=%s",
        (old["turn_id"],),
    )
    config = model_settings(settings).model_copy(update={"max_active_runs": 1})
    first = Runtime(db, config, ScriptedModel([])).claim()
    assert first.auth.user_id == "U002"
    assert Runtime(db, config, ScriptedModel([])).claim() is None


def test_oversized_json_is_rejected_before_any_write(db, settings, admin):
    config = settings.model_copy(update={"json_max_bytes": 65536})
    with TestClient(
        create_app(config, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        response = client.post(
            "/api/v1/knowledge/text",
            json={
                "company_id": "C001",
                "title": "too big",
                "content": "a" * 70000,
                "request_id": uid(),
            },
        )
        assert response.status_code == 413 and response.json()["code"] == "REQUEST_TOO_LARGE"
        assert admin.execute("SELECT count(*) n FROM knowledge_documents").fetchone()["n"] == 0


def test_input_dependency_scope_also_protects_session_title(db, settings, admin, execution_factory):
    from test_insights import context

    from app.values import jsonb

    ex = execution_factory("DEPENDENCY_PRIVATE_TITLE")
    context_id, _ = context(db, ex)
    admin.execute(
        "UPDATE context_snapshots SET manifest=manifest || %s WHERE context_id=%s",
        (jsonb({"heads": {"C002": {}}}), context_id),
    )
    admin.execute(
        "UPDATE user_company_grants SET can_read=false,can_update=false,can_upload_knowledge=false,can_import_invoices=false WHERE user_id='U001' AND company_id='C002'"
    )
    assert not turns.list_sessions(db, AUTH)
    with pytest.raises(AppError):
        turns.session_messages(db, AUTH, ex.session_id)


def test_retrieval_slots_fail_fast_and_are_released_after_failure(db, settings):
    from app.knowledge_retrieval import KnowledgeRetrieval

    config = settings.model_copy(update={"max_retrieval_requests": 1})
    service = KnowledgeRetrieval(db, config, None)
    assert service.search_slots.acquire(blocking=False)
    try:
        with pytest.raises(AppError) as error:
            service.search(AUTH, company_id="C001", query="test")
        assert error.value.status == 429
    finally:
        service.search_slots.release()
    for _ in range(2):
        with pytest.raises(AppError) as error:
            service.search(AUTH, company_id="C001", query="")
        assert error.value.code == "INVALID_ARGUMENT"


def test_pool_query_and_lock_waits_are_bounded(db):
    with db.transaction(readonly=True) as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "15s"
        assert conn.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "3s"


def test_revoked_queued_private_source_does_not_block_other_users(db, settings, admin):
    a = str(create_session(db, AUTH, "C002")["session_id"])
    bad = turns.accept_turn(db, AUTH, a, uid(), "继续")
    b = str(create_session(db, Identity("U002"), "C001")["session_id"])
    good = turns.accept_turn(db, Identity("U002"), b, uid(), "甲企业查询")
    admin.execute(
        "UPDATE user_company_grants SET can_read=false,can_update=false,can_upload_knowledge=false,can_import_invoices=false WHERE user_id='U001' AND company_id='C002'"
    )
    runtime = Runtime(db, model_settings(settings), ScriptedModel([]))
    assert runtime.claim() is None
    assert (
        admin.execute("SELECT status FROM turns WHERE turn_id=%s", (bad["turn_id"],)).fetchone()[
            "status"
        ]
        == "interrupted"
    )
    next_work = runtime.claim()
    assert next_work and next_work.turn_id == good["turn_id"]
