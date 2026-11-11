from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from test_runtime import ScriptedModel

from app import identity, turns
from app.api import create_app
from app.errors import AppError
from app.identity import Identity, create_session
from app.values import uid


def test_reuse_empty_session_only_and_keep_existing_history(db, settings):
    auth = Identity("U001")
    session = str(create_session(db, auth, "C001")["session_id"])
    with db.transaction(readonly=True) as conn:
        epoch = conn.execute(
            "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
        ).fetchone()["memory_epoch"]

    for company in ("C002", "C001", "C002"):
        changed = identity.switch_empty_session_company(db, auth, session, company)
        assert str(changed["session_id"]) == session
        assert changed["selected_company_id"] == company

    with db.transaction(readonly=True) as conn:
        assert (
            conn.execute(
                "SELECT memory_epoch FROM users WHERE user_id=%s", (auth.user_id,)
            ).fetchone()["memory_epoch"]
            == epoch
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM sessions WHERE user_id=%s", (auth.user_id,)
            ).fetchone()["n"]
            == 1
        )

    turns.accept_turn(db, auth, session, uid(), "乙企业的消息")
    with pytest.raises(AppError) as error:
        identity.switch_empty_session_company(db, auth, session, "C001")
    assert error.value.code == "SESSION_NOT_EMPTY"
    with db.transaction(readonly=True) as conn:
        assert (
            conn.execute(
                "SELECT selected_company_id FROM sessions WHERE session_id=%s", (session,)
            ).fetchone()["selected_company_id"]
            == "C002"
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM turns WHERE session_id=%s", (session,)
            ).fetchone()["n"]
            == 1
        )


def test_empty_switch_requires_session_ownership_and_company_permission(db, admin):
    session = str(create_session(db, Identity("U001"), "C001")["session_id"])
    with pytest.raises(AppError) as error:
        identity.switch_empty_session_company(db, Identity("U002"), session, "C001")
    assert error.value.status == 404
    admin.execute(
        "UPDATE user_company_grants SET can_read=false,can_update=false,can_upload_knowledge=false,can_import_invoices=false WHERE user_id='U001' AND company_id='C002'"
    )
    with pytest.raises(AppError) as error:
        identity.switch_empty_session_company(db, Identity("U001"), session, "C002")
    assert error.value.code == "ACCESS_DENIED"


def test_empty_switch_api_rejects_a_turn_accepted_by_another_tab(db, settings):
    with TestClient(
        create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        assert client.post("/api/v1/demo/login", json={"user_id": "U001"}).status_code == 200
        session = client.post("/api/v1/sessions", json={"selected_company_id": "C001"}).json()[
            "session_id"
        ]
        assert (
            client.post(
                f"/api/v1/sessions/{session}/turns",
                json={"client_request_id": uid(), "text": "已发送的消息"},
            ).status_code
            == 202
        )
        response = client.patch(
            f"/api/v1/sessions/{session}/empty-company", json={"selected_company_id": "C002"}
        )
        assert response.status_code == 409
        assert response.json()["code"] == "SESSION_NOT_EMPTY"


def test_stale_tab_cannot_send_into_switched_company(db, settings):
    with TestClient(
        create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        assert client.post("/api/v1/demo/login", json={"user_id": "U001"}).status_code == 200
        session = client.post("/api/v1/sessions", json={"selected_company_id": "C001"}).json()[
            "session_id"
        ]
        assert (
            client.patch(
                f"/api/v1/sessions/{session}/empty-company", json={"selected_company_id": "C002"}
            ).status_code
            == 200
        )
        response = client.post(
            f"/api/v1/sessions/{session}/turns",
            json={"client_request_id": uid(), "text": "旧页面消息", "expected_company_id": "C001"},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "SESSION_COMPANY_CHANGED"
        assert client.get(f"/api/v1/sessions/{session}/messages").json()["turns"] == []


def test_concurrent_send_and_empty_switch_cannot_move_a_message(db):
    auth = Identity("U001")
    for _ in range(5):
        session = str(create_session(db, auth, "C001")["session_id"])
        barrier = Barrier(2)

        def send(barrier=barrier, session=session):
            barrier.wait()
            try:
                turns.accept_turn(
                    db,
                    auth,
                    session,
                    uid(),
                    "甲企业消息",
                    expected_company_id="C001",
                    check_expected_company=True,
                )
                return "sent"
            except AppError as error:
                return error.code

        def switch(barrier=barrier, session=session):
            barrier.wait()
            try:
                identity.switch_empty_session_company(db, auth, session, "C002")
                return "switched"
            except AppError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            send_result = pool.submit(send)
            switch_result = pool.submit(switch)
            outcomes = (send_result.result(), switch_result.result())
        assert outcomes in (("sent", "SESSION_NOT_EMPTY"), ("SESSION_COMPANY_CHANGED", "switched"))
        with db.transaction(readonly=True) as conn:
            current = conn.execute(
                "SELECT selected_company_id FROM sessions WHERE session_id=%s", (session,)
            ).fetchone()["selected_company_id"]
            count = conn.execute(
                "SELECT count(*) AS n FROM turns WHERE session_id=%s", (session,)
            ).fetchone()["n"]
        assert (current, count) in (("C001", 1), ("C002", 0))
