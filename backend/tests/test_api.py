from fastapi.testclient import TestClient

from app.api import create_app
from app.values import uid


def test_B01_api_permissions_and_memory_management(db, settings):
    with TestClient(create_app(settings=settings, db=db, start_workers=False)) as client:
        assert client.get("/api/v1/me").status_code == 401
        assert client.post("/api/v1/demo/login", json={"user_id": "U001"}).status_code == 200
        session = client.post("/api/v1/sessions", json={"selected_company_id": "C001"}).json()
        assert session["user_id"] == "U001"
        pref = client.post(
            "/api/v1/preferences",
            json={"request_id": uid(), "scope": "C001", "key": "reply_style", "value": "简洁"},
        ).json()
        assert pref["value"] == "简洁"
        assert client.post("/api/v1/demo/login", json={"user_id": "U002"}).status_code == 200
        assert client.get(f"/api/v1/sessions/{session['session_id']}/messages").status_code == 404
        assert client.get("/api/v1/companies/C002/invoices").status_code == 403
        assert (
            client.request(
                "DELETE",
                f"/api/v1/preferences/{pref['preference_id']}",
                json={"request_id": uid(), "expected_revision": 1},
            ).status_code
            == 404
        )


def test_api_rejects_cross_origin_modifications(db, settings):
    with TestClient(create_app(settings=settings, db=db, start_workers=False)) as client:
        result = client.post(
            "/api/v1/demo/login",
            json={"user_id": "U001"},
            headers={"origin": "https://unrelated.example"},
        )
        assert result.status_code == 403
