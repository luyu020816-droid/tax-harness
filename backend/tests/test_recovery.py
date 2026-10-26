from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app import recovery
from app.api import create_app
from app.errors import AppError
from app.identity import Identity
from app.preferences import change_preference
from app.values import uid


def test_F30_consistent_postgres_backup_restore(db, settings, admin, tmp_path):
    dsn = make_conninfo(settings.admin_database_url, dbname="tax_harness_test")
    backup = recovery.backup(dsn, tmp_path / "backup")
    recovery.checkpoint(dsn, tmp_path / "receipt.json")
    db.close()
    admin.close()
    result = recovery.restore(dsn, tmp_path / "backup", tmp_path / "receipt.json", settings)
    assert result["fingerprint"] == backup["fingerprint"]
    assert not recovery.in_maintenance(dsn)


def test_F31_old_backup_cannot_revive_deleted_memory(db, settings, admin, tmp_path):
    dsn = make_conninfo(settings.admin_database_url, dbname="tax_harness_test")
    auth = Identity("U001")
    preference = change_preference(
        db, auth, action="remember", key="response_style", value="简洁", request_id=uid()
    )
    recovery.backup(dsn, tmp_path / "older")
    change_preference(
        db,
        auth,
        action="delete",
        target_id=str(preference["preference_id"]),
        expected_revision=preference["revision"],
        request_id=uid(),
    )
    recovery.checkpoint(dsn, tmp_path / "latest-receipt.json")
    db.close()
    admin.close()
    try:
        with pytest.raises(AppError) as error:
            recovery.restore(dsn, tmp_path / "older", tmp_path / "latest-receipt.json", settings)
        assert error.value.code == "RECOVERY_POINT_BEHIND"
        assert recovery.in_maintenance(dsn)
        with pytest.raises(AppError):
            recovery.checkpoint(dsn, tmp_path / "untrusted-new-reference.json")
        test_settings = settings.model_copy(update={"database_url": settings.test_database_url})
        with TestClient(create_app(test_settings, start_workers=False)) as client:
            assert client.get("/api/v1/preferences").status_code == 503
            assert client.get("/api/v1/ready").json()["maintenance"] is True
    finally:
        # Isolated test database only. Production has no automatic bypass of this gate.
        recovery.maintenance_file(dsn).unlink(missing_ok=True)


def test_F31_old_checkpoint_cannot_hide_later_delete(db, settings, admin, tmp_path):
    dsn = make_conninfo(settings.admin_database_url, dbname="tax_harness_test")
    auth = Identity("U001")
    pref = change_preference(
        db, auth, action="remember", key="response_style", value="简洁", request_id=uid()
    )
    recovery.backup(dsn, tmp_path / "old")
    recovery.checkpoint(dsn, tmp_path / "old-receipt.json")
    change_preference(
        db,
        auth,
        action="delete",
        target_id=str(pref["preference_id"]),
        expected_revision=pref["revision"],
        request_id=uid(),
    )
    db.close()
    admin.close()
    try:
        with pytest.raises(AppError) as error:
            recovery.restore(dsn, tmp_path / "old", tmp_path / "old-receipt.json", settings)
        assert error.value.code == "RECOVERY_CHECKPOINT_STALE"
        with pytest.raises(AppError):
            recovery.resume(dsn, tmp_path / "old-receipt.json")
        result = recovery.resume(dsn, Path(error.value.details["checkpoint_path"]))
        assert result["state"] == "verified_and_resumed"
        assert not recovery.in_maintenance(dsn)
    finally:
        recovery.maintenance_file(dsn).unlink(missing_ok=True)
