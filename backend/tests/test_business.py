from decimal import Decimal

import psycopg
import pytest

from app import business
from app.errors import AppError
from app.identity import Identity, assert_access
from app.rules import get_rules

U1 = Identity("U001")
U2 = Identity("U002")


@pytest.mark.parametrize(
    "company,period,total,income,expense",
    [
        ("C001", "2026-08", "9046.35", "14850.75", "5720.99"),
        ("C001", "2026-09", "9622.06", "13000.50", "7180.74"),
        ("C002", "2026-08", "13667.05", "18800.25", "9341.10"),
        ("C002", "2026-09", "12667.24", "13700.75", "8087.24"),
    ],
)
def test_B03_exact_totals(db, company, period, total, income, expense):
    result = business.query_month(db, U1, company, period)
    assert result["invoice_total"] == Decimal(total)
    assert result["bank_income"] == Decimal(income)
    assert result["bank_expense"] == Decimal(expense)
    assert [
        result[k] for k in ("invoice_count", "bank_count", "income_count", "expense_count")
    ] == [10, 12, 4, 8]


def test_B04_no_records_is_not_permission_error(db):
    result = business.query_month(db, U2, "C001", "2026-07")
    assert result["has_records"] is False
    assert (
        result["invoice_total"]
        == result["bank_income"]
        == result["bank_expense"]
        == Decimal("0.00")
    )
    with pytest.raises(AppError, match="无权"):
        business.query_month(db, U2, "C002", "2026-09")
    with pytest.raises(AppError, match="月份"):
        business.query_month(db, U1, "C001", "September")


@pytest.mark.parametrize(
    "company,expected,version",
    [
        ("C001", {"F002", "F003", "F004", "F006", "F009"}, "2.0"),
        ("C002", {"F002", "F005", "F006"}, "1.0"),
    ],
)
def test_B05_B06_required_purposes(db, company, expected, version):
    result = business.check_purposes(db, U1, company, "2026-09")
    assert {r["invoice_id"] for r in result["rows"]} == expected
    assert result["missing_count"] == len(expected)
    assert result["rule_refs"][0]["business_version"] == version
    if company == "C002":
        assert result["blank_count"] == 6


def test_B01_B07_database_and_user_permissions(db):
    with db.transaction(readonly=True) as conn:
        assert_access(conn, U1, "C001", write=True)
        assert_access(conn, U1, "C002", write=True)
        assert_access(conn, U2, "C001")
        with pytest.raises(AppError):
            assert_access(conn, U2, "C001", write=True)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction() as conn:
        conn.execute("UPDATE invoices SET amount=1 WHERE company_id='C001' AND invoice_id='F002'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction() as conn:
        conn.execute("UPDATE bank_entries SET note='forbidden' WHERE company_id='C001'")


def test_B06_blank_note_and_period(db, admin):
    admin.execute(
        "UPDATE invoices SET purpose=E'\\t  \\n',note='not a purpose' WHERE company_id='C001' AND invoice_id='F001'"
    )
    assert business.check_purposes(db, U1, "C001", "2026-09")["missing_count"] == 6
    with pytest.raises(AppError) as exc:
        business.check_purposes(db, U1, "C001", "2026-08")
    assert exc.value.code == "PERIOD_UNSUPPORTED"


def test_B05_disabled_rule_not_used(db):
    result = get_rules(db, U1, "C001", "2026-09", ["purpose"])
    assert len(result["rules"]) == 1
    assert result["rules"][0]["business_version"] == "2.0"


def test_B03_consistent_snapshot(db, admin, monkeypatch):
    original = business._snapshot

    def concurrent_update(conn, company_id):
        with admin.transaction():
            admin.execute(
                "UPDATE invoices SET purpose='another session' WHERE company_id='C001' AND invoice_id='F002'"
            )
            admin.execute(
                "UPDATE company_heads SET data_revision=data_revision+1 WHERE company_id='C001'"
            )
        return original(conn, company_id)

    monkeypatch.setattr(business, "_snapshot", concurrent_update)
    result = business.check_purposes(db, U1, "C001", "2026-09")
    assert result["data_revision"] == 1
    assert result["missing_count"] == 5
    assert (
        admin.execute("SELECT data_revision FROM company_heads WHERE company_id='C001'").fetchone()[
            "data_revision"
        ]
        == 2
    )
