from contextlib import contextmanager

import psycopg
import pytest

from app import invoice_write, mutations
from app.errors import AppError
from app.identity import Identity
from app.tasks import change_task
from app.values import uid

AUTH = Identity("U001")


def request(db, source, **overrides):
    args = {
        "company_id": "C001",
        "invoice_id": "F002",
        "patch": {"purpose": "办公资料整理"},
        "expected_revision": 1,
        "source_id": str(source["source_id"]),
        "source_refs": [{"source_id": str(source["source_id"]), "quote": source["content"]}],
        **overrides,
    }
    plan = invoice_write.prepare(
        db, Identity(source["user_id"]), **{k: v for k, v in args.items() if k != "operation_id"}
    )
    return {**args, "plan_id": plan["plan_id"]}


def test_B08_B09_write_and_stale_revision_retry(db, admin, source_factory):
    source = source_factory("把甲企业F002用途改为办公资料整理")
    first = mutations.update_invoice(db, AUTH, **request(db, source))
    replay = mutations.update_invoice(db, AUTH, **request(db, source))
    assert first["commit_state"] == replay["commit_state"] == "committed"
    assert first["verification_state"] == "verified"

    assert replay["replayed"] is True
    assert str(first["operation_id"]) == str(replay["operation_id"])
    assert admin.execute(
        "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
    ).fetchone() == {"purpose": "办公资料整理", "revision": 2}
    assert (
        admin.execute(
            "SELECT purpose FROM invoices WHERE company_id='C002' AND invoice_id='F002'"
        ).fetchone()["purpose"]
        is None
    )
    assert admin.execute("SELECT count(*) AS n FROM operations").fetchone()["n"] == 1


def test_B07_target_id_cannot_disagree_with_user(db, source_factory):
    source = source_factory("甲企业F003用途补为办公资料整理")
    with pytest.raises(AppError) as error:
        mutations.update_invoice(db, AUTH, **request(db, source))
    assert error.value.code == "NEEDS_CLARIFICATION"


def test_B10_rank_cannot_bypass_displayed_view(db, source_factory):
    source = source_factory("甲企业第一张用途补为办公资料整理")
    with pytest.raises(AppError) as error:
        mutations.update_invoice(db, AUTH, **request(db, source))
    assert error.value.code == "NEEDS_CLARIFICATION"


def test_B09_intent_frozen_independently_of_patch(db, source_factory):
    source = source_factory("甲企业F002用途办公资料整理，备注月末核对")
    mutations.update_invoice(
        db, AUTH, **request(db, source, patch={"purpose": "办公资料整理", "note": "月末核对"})
    )
    with pytest.raises(AppError) as exc:
        mutations.update_invoice(
            db, AUTH, **request(db, source, patch={"note": "月末核对"}, operation_id=uid())
        )
    assert exc.value.code == "INTENT_CONFLICT"


def test_B12_same_invoice_two_fields(db, admin, source_factory):
    source = source_factory("把甲企业F002用途补成办公资料整理，备注月末核对")
    result = mutations.update_invoice(
        db, AUTH, **request(db, source, patch={"purpose": "办公资料整理", "note": "月末核对"})
    )
    assert result["saved_values"] == {"purpose": "办公资料整理", "note": "月末核对"}
    assert (
        admin.execute("SELECT count(*) AS n FROM operations WHERE state='committed'").fetchone()[
            "n"
        ]
        == 1
    )


def test_B07_B12_reject_permissions_and_unattributed_values(db, source_factory):
    source = source_factory("查看甲企业F002")
    with pytest.raises(AppError):
        mutations.update_invoice(db, AUTH, **request(db, source))
    with pytest.raises(AppError):
        mutations.update_invoice(db, AUTH, **request(db, source, patch={"amount": "1.00"}))
    readonly = source_factory("把甲企业F002用途改为办公资料整理", user="U002")
    with pytest.raises(AppError) as exc:
        mutations.update_invoice(db, Identity("U002"), **request(db, readonly))
    assert exc.value.code == "ACCESS_DENIED"


def test_B09_same_value_is_no_change(db, admin, source_factory):
    first = source_factory("甲企业F002用途改为办公资料整理")
    mutations.update_invoice(db, AUTH, **request(db, first))
    new = source_factory("再把甲企业F002用途设为办公资料整理")
    result = mutations.update_invoice(db, AUTH, **request(db, new, expected_revision=2))
    assert result["commit_state"] == "no_change"
    assert (
        admin.execute(
            "SELECT revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()["revision"]
        == 2
    )


def test_F22_business_and_task_rollback_together(db, admin, source_factory, monkeypatch):
    change_task(
        db,
        AUTH,
        company_id="C001",
        action="start",
        request_id=uid(),
        intent="complete_purposes",
        period="2026-09",
    )
    source = source_factory("把甲企业F002用途改为办公资料整理")

    def fail(*args):
        raise AppError("TEST_FAULT", "injected task persistence failure")

    monkeypatch.setattr(mutations, "_revision", fail)
    with pytest.raises(AppError):
        mutations.update_invoice(db, AUTH, **request(db, source))
    assert admin.execute(
        "SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
    ).fetchone() == {"purpose": None, "revision": 1}
    assert (
        admin.execute("SELECT data_revision FROM company_heads WHERE company_id='C001'").fetchone()[
            "data_revision"
        ]
        == 1
    )


def test_F24_committed_but_readback_unavailable(db, admin, source_factory, monkeypatch):
    source = source_factory("把甲企业F002用途改为办公资料整理")
    original = db.transaction

    @contextmanager
    def fail_read(*, readonly=False):
        if readonly:
            raise psycopg.OperationalError("injected read-back failure")
        with original() as conn:
            yield conn

    monkeypatch.setattr(db, "transaction", fail_read)
    result = mutations.update_invoice(db, AUTH, **request(db, source))
    assert result["commit_state"] == "committed"
    assert result["verification_state"] == "pending"
    assert (
        admin.execute(
            "SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()["purpose"]
        == "办公资料整理"
    )


def test_B11_later_change_is_not_falsely_verified(db, source_factory, admin):
    source = source_factory("把甲企业F002用途改为办公资料整理")
    result = mutations.update_invoice(db, AUTH, **request(db, source))
    admin.execute(
        "UPDATE invoices SET purpose='新的明确用途',revision=revision+1 WHERE company_id='C001' AND invoice_id='F002'"
    )
    admin.execute("UPDATE company_heads SET data_revision=data_revision+1 WHERE company_id='C001'")
    replay = mutations.update_invoice(db, AUTH, **request(db, source))
    assert replay["verification_state"] == "changed_since_commit"
    assert replay["current_values"]["purpose"] == "新的明确用途"
    assert str(result["operation_id"]) == str(replay["operation_id"])
