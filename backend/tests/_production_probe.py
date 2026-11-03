"""Explicitly invoked audit probes; reproductions are not security acceptance tests.

Run pytest on this file explicitly. Never sends model requests or touches main DB.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from test_knowledge import knowledge as knowledge_fixture
from test_knowledge import uploaded
from test_runtime import ScriptedModel, model_settings
from test_trusted_writes import TEXT, candidate

from app import invoice_write
from app.api import create_app
from app.errors import AppError
from app.identity import Identity, create_session
from app.runtime import Runtime
from app.turns import accept_turn, read_turn
from app.values import uid


def evidence(value):
    print("PRODUCTION_PROBE " + json.dumps(value, ensure_ascii=True), flush=True)


@pytest.fixture
def indexed_knowledge(db, settings, admin):
    return knowledge_fixture.__wrapped__(db, settings, admin)


def test_reproduce_demo_login_requires_no_identity_proof(db, settings):
    with TestClient(
        create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        response = client.post("/api/v1/demo/login", json={"user_id": "U001"})
        assert response.status_code == 200
        assert client.get("/api/v1/me").json()["user_id"] == "U001"
        evidence(
            {
                "case": "demo_login_without_credentials",
                "reproduced": True,
                "production_acceptance": "blocked",
            }
        )


def test_cross_user_private_resources_are_denied(
    db, settings, indexed_knowledge, execution_factory
):
    ex = execution_factory(TEXT)
    plan = invoice_write.prepare(db, ex.auth, **candidate(ex))
    document = uploaded(indexed_knowledge)
    paths = [
        f"/sessions/{ex.session_id}/messages",
        f"/turns/{ex.turn_id}",
        f"/sources/{ex.source_id}",
        f"/write-plans/{plan['plan_id']}",
        f"/runs/{ex.run_id}",
        f"/insights/requests/{ex.turn_id}",
        f"/insights/requests/{ex.turn_id}/export",
        f"/knowledge/documents/{document['document_id']}",
        f"/knowledge/documents/{document['document_id']}/file",
    ]
    statuses = []
    with TestClient(
        create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U002"})
        for path in paths:
            response = client.get("/api/v1" + path)
            assert response.status_code in (403, 404, 409), (path, response.status_code)
            statuses.append(response.status_code)
    evidence(
        {
            "case": "cross_user_private_resource_matrix",
            "checked": len(paths),
            "all_denied": True,
            "statuses": statuses,
        }
    )


def test_revoked_company_title_is_hidden(db, settings, admin, execution_factory):
    marker = "REVOKED_TITLE_" + uid()
    ex = execution_factory("乙企业内部标题 " + marker, company="C002")
    admin.execute(
        "UPDATE user_company_grants SET can_read=false,can_update=false,can_upload_knowledge=false,can_import_invoices=false WHERE user_id='U001' AND company_id='C002'"
    )
    with pytest.raises(AppError):
        read_turn(db, Identity("U001"), ex.turn_id)
    with TestClient(
        create_app(settings, db=db, model=ScriptedModel([]), start_workers=False)
    ) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        recent = client.get("/api/v1/sessions")
        full = client.get("/api/v1/sessions/history")
        assert recent.status_code == 200 and full.status_code == 200
        assert marker not in recent.text and marker not in full.text
    evidence(
        {
            "case": "revoked_company_title",
            "detail_denied": True,
            "recent_title_visible": False,
            "full_history_title_visible": False,
            "production_acceptance": "title_boundary_verified",
        }
    )


@pytest.mark.parametrize("attempt", range(3))
def test_two_authorized_users_same_invoice_have_one_winner(db, admin, execution_factory, attempt):
    admin.execute("INSERT INTO users(user_id,display_name) VALUES('U003','Concurrent test user')")
    admin.execute(
        "INSERT INTO user_company_grants(user_id,company_id,can_read,can_update) VALUES('U003','C001',true,true)"
    )
    a = execution_factory(TEXT)
    text_b = "把甲企业F002用途填写为办公资料整理。"
    b = execution_factory(text_b, user="U003")
    args_b = {**candidate(b, text_b), "patch": {"purpose": "办公资料整理"}}
    plans = [
        invoice_write.prepare(db, a.auth, **candidate(a)),
        invoice_write.prepare(db, b.auth, **args_b),
    ]
    assert all(p["status"] == "authorized" for p in plans)
    barrier = Barrier(2)

    def commit(pair):
        ex, plan = pair
        barrier.wait(timeout=10)
        try:
            result = invoice_write.commit(db, ex.auth, plan["plan_id"], execution=ex)
            return {"ok": True, "verification": result["verification_state"]}
        except AppError as error:
            return {"ok": False, "code": error.code}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, [(a, plans[0]), (b, plans[1])]))
    row = admin.execute(
        "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
    ).fetchone()
    count = admin.execute("SELECT count(*) n FROM operations WHERE state='committed'").fetchone()[
        "n"
    ]
    assert sum(r["ok"] for r in results) == 1, results
    assert count == 1 and row["revision"] == 2
    evidence(
        {
            "case": "concurrent_two_users_same_invoice",
            "attempt": attempt,
            "results": results,
            "committed_operations": count,
            "final_revision": row["revision"],
        }
    )


def test_worker_claim_serializes_one_user_but_allows_different_users(db, settings):
    a = create_session(db, Identity("U001"), "C001")
    b = create_session(db, Identity("U002"), "C001")
    for n in range(2):
        accept_turn(db, Identity("U001"), str(a["session_id"]), uid(), f"甲企业查询排队测试 {n}")
    accept_turn(db, Identity("U002"), str(b["session_id"]), uid(), "甲企业独立用户排队测试")
    runtimes = [Runtime(db, model_settings(settings), ScriptedModel([])) for _ in range(2)]
    first = runtimes[0].claim()
    second = runtimes[1].claim()
    assert first and second and {first.auth.user_id, second.auth.user_id} == {"U001", "U002"}
    assert runtimes[0].claim() is None
    evidence(
        {
            "case": "worker_claim_isolation",
            "different_users_claimed": True,
            "same_user_second_work_not_claimed": True,
        }
    )
