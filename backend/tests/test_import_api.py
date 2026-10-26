import pytest
from fastapi.testclient import TestClient
from test_invoice_imports import invoice, workbook

from app.api import create_app
from app.errors import AppError
from app.identity import Identity, create_session
from app.values import uid


def test_competition_and_extension_login_cookies_are_isolated(
    db, settings, extended, extended_settings
):
    with (
        TestClient(create_app(settings, db=db, start_workers=False)) as official,
        TestClient(
            create_app(
                extended_settings, db=extended.db, ingest_db=extended.ingest_db, start_workers=False
            )
        ) as extra,
    ):
        a = official.post("/api/v1/demo/login", json={"user_id": "U001"})
        b = extra.post("/api/v1/demo/login", json={"user_id": "U002"})
        assert set(a.cookies) != set(b.cookies)
        official.cookies.update(b.cookies)
        extra.cookies.update(a.cookies)
        assert official.get("/api/v1/me").json()["user_id"] == "U001"
        assert extra.get("/api/v1/me").json()["user_id"] == "U002"


def test_IMP20_http_upload_preview_confirm(extended, extended_settings):
    with TestClient(
        create_app(
            extended_settings, db=extended.db, ingest_db=extended.ingest_db, start_workers=False
        )
    ) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        assert client.get("/api/v1/me").json()["model_ready"] is False
        assert client.get("/api/v1/me").json()["dataset_profile"] == "extended"
        session = client.post("/api/v1/sessions", json={"selected_company_id": "C001"}).json()[
            "session_id"
        ]
        request = uid()
        uploaded = client.post(
            "/api/v1/uploads/invoices",
            data={"company_id": "C001", "session_id": session, "client_request_id": request},
            files={
                "file": (
                    "new.xlsx",
                    workbook([invoice()]),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        upload = uploaded.json()
        assert "content" not in upload
        batch = client.post(
            "/api/v1/invoice-imports",
            json={
                "upload_id": upload["upload_id"],
                "session_id": session,
                "client_request_id": uid(),
            },
        ).json()
        preview = client.post(
            f"/api/v1/invoice-imports/{batch['batch_id']}/preview",
            json={
                "session_id": session,
                "expected_revision": batch["revision"],
                "client_request_id": uid(),
            },
        ).json()
        assert preview["status"] == "preview_ready", preview
        read = client.get(f"/api/v1/invoice-imports/{batch['batch_id']}").json()
        assert "confirmation_token" not in read and "confirmation_token_hash" not in read
        result = client.post(
            f"/api/v1/invoice-imports/{batch['batch_id']}/confirm",
            json={
                "session_id": session,
                "expected_revision": preview["revision"],
                "preview_hash": preview["preview_hash"],
                "confirmation_token": preview["confirmation_token"],
                "client_request_id": uid(),
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["receipt"]["inserted_count"] == 1
        assert len(client.get("/api/v1/invoice-imports").json()["batches"]) == 1
        client.post("/api/v1/demo/login", json={"user_id": "U002"})
        assert client.get(f"/api/v1/invoice-imports/{batch['batch_id']}").status_code == 404
        assert client.get("/api/v1/invoice-imports").json()["batches"] == []


def test_IMP08_IMP10_upload_acl_origin_and_actual_body_limit(extended, extended_settings):
    with TestClient(
        create_app(
            extended_settings, db=extended.db, ingest_db=extended.ingest_db, start_workers=False
        )
    ) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U002"})
        session = str(create_session(extended.db, Identity("U002"), "C001")["session_id"])
        data = {"company_id": "C001", "session_id": session, "client_request_id": uid()}
        assert (
            client.post(
                "/api/v1/uploads/invoices",
                data=data,
                files={"file": ("new.xlsx", workbook([invoice()]))},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/uploads/invoices",
                data=data,
                files={"file": ("new.xlsx", workbook([invoice()]))},
                headers={"Origin": "https://other.example"},
            ).status_code
            == 403
        )
        response = client.post(
            "/api/v1/uploads/invoices",
            content=b"x" * (5 * 1024 * 1024 + 65537),
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 413


def test_IMP09_competition_mode_rejects_http_import(db, settings):
    with TestClient(create_app(settings, db=db, start_workers=False)) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        session = client.post("/api/v1/sessions", json={"selected_company_id": "C001"}).json()[
            "session_id"
        ]
        response = client.post(
            "/api/v1/uploads/invoices",
            data={"company_id": "C001", "session_id": session, "client_request_id": uid()},
            files={"file": ("new.xlsx", workbook([invoice()]))},
        )
        assert response.status_code == 403 and response.json()["code"] == "IMPORT_DISABLED"


def test_IMP14_cross_session_or_company_confirmation_rejected(extended):
    from test_invoice_imports import confirm, prepare

    session, preview = prepare(extended, [invoice()])
    other = str(create_session(extended.db, Identity("U001"), "C002")["session_id"])
    with pytest.raises(AppError):
        confirm(extended, other, preview)
