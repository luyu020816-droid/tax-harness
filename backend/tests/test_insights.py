import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import insights, telemetry
from app.api import create_app
from app.errors import AppError
from app.identity import Identity
from app.insight_access import public_run
from app.insight_metrics import costs, percentile, union_ms, window
from app.trace import record_context
from app.values import digest, jsonb, uid

AUTH = Identity("U001")


def context(db, ex, usage=None, kind=None):
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "private secret"}]}
    manifest = {
        "kind": kind,
        "sources": [{"source_id": ex.source_id, "revision": 1}],
        "token_count": 12,
        "count_kind": "estimated",
        "payload_hash": digest(payload),
    }
    with db.transaction() as conn:
        cid = record_context(conn, ex, 1, payload, manifest)
        if usage is not None:
            conn.execute(
                "UPDATE context_snapshots SET usage=%s,duration_ms=12 WHERE context_id=%s",
                (jsonb(usage), cid),
            )
    return cid, payload


def test_metrics_do_not_double_count_intervals_or_fake_missing_cost():
    now = datetime.now(UTC)
    assert (
        union_ms(
            [
                (now, now + timedelta(seconds=2)),
                (now + timedelta(seconds=1), now + timedelta(seconds=3)),
            ]
        )
        == 3000
    )
    assert union_ms([(now, None)]) is None
    assert percentile([], 0.95) is None
    assert percentile([1, 2, 3], 0.95) == 3
    assert (
        costs([{"model": "unknown", "dispatched": True, "usage": None}], {})["complete_cost"]
        is None
    )
    assert costs([{"dispatched": False}], {})["unknown_calls"] == 0
    with pytest.raises(AppError):
        window("2026-10-01", "2026-10-02")


def test_input_composition_is_partition_not_provider_usage():
    from app.values import canonical

    payload = {
        "model": "m",
        "tools": [{"name": "t"}],
        "messages": [
            {
                "role": "system",
                "content": "系统\n正式状态：\n"
                + canonical({"task": {"period": "2026-09"}, "preferences": [], "memory_epoch": 1}),
            },
            {"role": "user", "content": "之前的问题"},
            {"role": "assistant", "content": "旧答复"},
            {"role": "user", "content": "本次问题"},
        ],
    }
    parts = telemetry.input_composition(payload)
    assert sum(parts["parts"].values()) == len(canonical(payload).encode("utf-8")) / 2
    assert parts["parts"]["task_and_preferences"] > 0 and parts["parts"]["current_input"] > 0
    assert telemetry.input_composition(payload, material=True)["parts"]["current_material"] > 0


def test_interrupted_takeover_time_is_not_complete_active_latency(db, execution_factory, admin):
    ex = execution_factory("查询资料")
    admin.execute(
        "UPDATE runs SET state='interrupted',finished_at=clock_timestamp() WHERE run_id=%s",
        (ex.run_id,),
    )
    admin.execute("UPDATE turns SET status='interrupted' WHERE turn_id=%s", (ex.turn_id,))
    assert insights.detail(db, AUTH, ex.turn_id)["request"]["active_ms"] is None
    result = insights.summary(db, AUTH, insights.filters(AUTH))
    assert result["timed_requests"] == 0 and result["request_count"] == 1


@pytest.mark.asyncio
async def test_model_failure_usage_and_summary_attempts_are_persisted(db, execution_factory, admin):
    ex = execution_factory("查询甲企业")
    cid, payload = context(db, ex)

    class Broken:
        async def complete(self, payload):
            raise AppError("MODEL_UNAVAILABLE", "timeout", 503)

    with pytest.raises(AppError):
        await telemetry.complete(db, Broken(), ex, cid, payload)
    row = admin.execute("SELECT * FROM trace_spans").fetchone()
    assert row["status"] == "failed" and row["duration_ms"] >= 0
    assert row["attributes"]["usage"] is None
    assert row["attributes"]["request_dispatched"] is True
    # A repeated logical key cannot create a second billed attempt record.
    with pytest.raises(AppError):
        await telemetry.complete(db, Broken(), ex, cid, payload)
    assert admin.execute("SELECT count(*) n FROM trace_spans").fetchone()["n"] == 1
    f = insights.filters(AUTH)
    result = insights.summary(db, AUTH, f)
    assert result["model_attempts"] == 1 and result["cost"]["complete_cost"] is None
    assert result["coverage"]["usage_known"] == 0


def test_history_usage_control_snapshots_pagination_and_identity(db, execution_factory, admin):
    ex = execution_factory("查询甲企业")
    context(db, ex, {"prompt_tokens": 100, "completion_tokens": 20})
    context(db, ex, None, "control_execution")
    result = insights.summary(db, AUTH, insights.filters(AUTH))
    assert result["request_count"] == 1 and result["model_attempts"] == 1
    assert result["input_tokens"] == 100 and result["output_tokens"] == 20
    assert (
        insights.summary(db, Identity("U002"), insights.filters(Identity("U002")))["request_count"]
        == 0
    )
    with pytest.raises(AppError):
        insights.detail(db, Identity("U002"), ex.turn_id)
    execution_factory("另一项查询")
    # Pin the cohort to persisted DB timestamps. Python/PG wall-clock precision
    # can otherwise put the freshly committed second row beyond the ceiling.
    ceiling = admin.execute("SELECT max(created_at) AS at FROM turns").fetchone()["at"]
    f = insights.filters(AUTH, as_of=ceiling.isoformat())
    first = insights.requests(db, AUTH, f, limit=1)
    second = insights.requests(db, AUTH, f, first["next_cursor"], 1)
    assert first["requests"][0]["turn_id"] != second["requests"][0]["turn_id"]
    assert second["has_more"] is False
    wrong = {**f, "model": "another"}
    with pytest.raises(AppError):
        insights.requests(db, AUTH, wrong, first["next_cursor"], 1)


def test_suppression_redacts_old_export_and_nodes_and_revocation_hides_counts(
    db, execution_factory, admin
):
    ex = execution_factory("查询甲企业")
    cid, _ = context(db, ex, {"prompt_tokens": 100, "completion_tokens": 20})
    assert "private secret" in json.dumps(insights.node(db, AUTH, cid))
    admin.execute(
        "UPDATE sources SET recall_state='suppressed' WHERE source_id=%s", (ex.source_id,)
    )
    assert insights.node(db, AUTH, cid)["redacted"]
    assert public_run(db, AUTH, ex.run_id)["contexts"][0]["payload"]["redacted"]
    assert "private secret" not in json.dumps(insights.detail(db, AUTH, ex.turn_id))
    admin.execute("DELETE FROM user_company_grants WHERE user_id='U001' AND company_id='C001'")
    assert insights.summary(db, AUTH, insights.filters(AUTH))["request_count"] == 0
    assert not insights.requests(db, AUTH, insights.filters(AUTH))["requests"]
    with pytest.raises(AppError):
        insights.detail(db, AUTH, ex.turn_id)


def test_insight_api_is_authenticated_and_read_only(db, settings, execution_factory, admin):
    ex = execution_factory("查询甲企业")
    context(db, ex, {"prompt_tokens": 100, "completion_tokens": 20})
    before = admin.execute(
        "SELECT jsonb_agg(to_jsonb(i) ORDER BY company_id,invoice_id) x FROM invoices i"
    ).fetchone()["x"]
    with TestClient(create_app(settings=settings, db=db, start_workers=False)) as client:
        assert client.get("/api/v1/insights/summary").status_code == 401
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        assert client.get("/api/v1/insights/summary").json()["input_tokens"] == 100
        assert client.get("/api/v1/insights/requests").status_code == 200
        assert client.get(f"/api/v1/insights/requests/{ex.turn_id}").status_code == 200
        assert client.get(f"/api/v1/insights/requests/{ex.turn_id}/export").status_code == 200
        assert (
            client.get("/api/v1/insights/summary?from=2026-10-01&to=2026-10-02").status_code == 422
        )
    after = admin.execute(
        "SELECT jsonb_agg(to_jsonb(i) ORDER BY company_id,invoice_id) x FROM invoices i"
    ).fetchone()["x"]
    assert before == after


def test_import_report_recalculates_metrics_and_preserves_scope(db, admin):
    from app.config import ROOT
    from app.insight_evaluations import import_report, normalize, read

    report = json.loads((ROOT / "evals/live-20261101/comparison.json").read_text(encoding="utf-8"))
    normalized = normalize(report)
    assert normalized["aggregate"] == report["aggregate"]
    assert "payload" not in json.dumps(normalized)
    with admin.transaction():
        first = import_report(admin, "U001", ["C001"], report)
        second = import_report(admin, "U001", ["C001"], report)
    assert first == second
    assert len(read(db, AUTH)) == 1
    assert read(db, Identity("U002")) == []
    admin.execute("DELETE FROM user_company_grants WHERE user_id='U001' AND company_id='C001'")
    assert read(db, AUTH) == []


def test_nested_measurement_and_paged_trace(db, execution_factory):
    ex = execution_factory("只读检查")
    with telemetry.phase(db, ex, "parent", "tool", "查询"):
        with telemetry.phase(db, ex, "child", "host", "子阶段"):
            pass
    result = insights.detail(db, AUTH, ex.turn_id)
    parent, child = result["runs"][0]["spans"]
    assert child["parent_span_id"] == parent["span_id"]
    for i in range(102):
        with telemetry.phase(db, ex, f"phase:{i}", "host", "阶段"):
            pass
    result = insights.detail(db, AUTH, ex.turn_id)
    assert len(result["runs"][0]["spans"]) == 100
    page = insights.read_spans(db, AUTH, ex.run_id, 100)
    assert len(page["spans"]) == 4 and not page["has_more"]


def test_deleted_preference_cannot_reappear_in_old_input(db, execution_factory, admin):
    from app.preferences import change_preference

    pref = change_preference(
        db, AUTH, action="remember", key="response_style", value="简洁", request_id=uid()
    )
    ex = execution_factory("查询资料")
    cid, _ = context(db, ex, {"prompt_tokens": 20, "completion_tokens": 1})
    with db.transaction() as conn:
        conn.execute(
            "UPDATE context_snapshots SET manifest=manifest || %s WHERE context_id=%s",
            (
                jsonb(
                    {
                        "dependencies": [
                            {
                                "kind": "preference",
                                "id": str(pref["preference_id"]),
                                "revision": 1,
                                "generation": 1,
                            }
                        ]
                    }
                ),
                cid,
            ),
        )
    change_preference(
        db,
        AUTH,
        action="delete",
        target_id=str(pref["preference_id"]),
        expected_revision=1,
        request_id=uid(),
    )
    assert insights.node(db, AUTH, cid)["redacted"]


def test_private_scope_and_revoked_dependency_hide_entire_request(
    db, execution_factory, source_factory, admin
):
    private = execution_factory("私人备忘", company=None)
    assert (
        insights.summary(db, AUTH, insights.filters(AUTH, company_id="C001"))["request_count"] == 0
    )
    assert insights.summary(db, AUTH, insights.filters(AUTH))["request_count"] == 1
    ex = execution_factory("甲乙企业关联查询")
    cid, _ = context(db, ex, {"prompt_tokens": 5, "completion_tokens": 1})
    source = source_factory("乙企业原话", company="C002")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE context_snapshots SET manifest=jsonb_set(manifest,'{sources}',(manifest->'sources') || %s) WHERE context_id=%s",
            (jsonb([{"source_id": str(source["source_id"]), "revision": 1}]), cid),
        )
    admin.execute("DELETE FROM user_company_grants WHERE user_id='U001' AND company_id='C002'")
    assert insights.summary(db, AUTH, insights.filters(AUTH))["request_count"] == 1
    with pytest.raises(AppError):
        insights.detail(db, AUTH, ex.turn_id)
    assert insights.detail(db, AUTH, private.turn_id)["request"]["turn_id"] == private.turn_id


@pytest.mark.asyncio
async def test_performed_delivery_is_not_claimed_as_full_user_intent_coverage(db, settings, admin):
    from test_runtime import ScriptedModel, final, model_settings, tool

    from app.identity import create_session
    from app.runtime import Runtime
    from app.turns import accept_turn

    session = create_session(db, AUTH, "C001")
    turn = accept_turn(db, AUTH, str(session["session_id"]), uid(), "查询甲企业2026年9月月度资料")
    runtime = Runtime(
        db,
        model_settings(settings),
        ScriptedModel(
            [tool("query_month", {"company_id": "C001", "period": "2026-09"}), final("查询完成")]
        ),
    )
    await runtime.run(runtime.claim())
    evidence = insights.detail(db, AUTH, turn["turn_id"])["runs"][0]["evidence"]
    assert evidence["performed_delivery"][0]["status"] == "delivered"
    assert evidence["user_intent_coverage"] == "not_verified"
    admin.execute("UPDATE turns SET final_cards='[]' WHERE turn_id=%s", (turn["turn_id"],))
    evidence = insights.detail(db, AUTH, turn["turn_id"])["runs"][0]["evidence"]
    assert evidence["performed_delivery"][0]["status"] == "executed_not_delivered"


@pytest.mark.asyncio
async def test_optional_measurement_failure_does_not_change_model_result(
    db, execution_factory, monkeypatch
):
    from contextlib import contextmanager

    import psycopg

    ex = execution_factory("只读查询")
    cid, payload = context(db, ex)

    @contextmanager
    def unavailable(db):
        raise psycopg.OperationalError("injected optional metric outage")
        yield

    class Model:
        async def complete(self, payload):
            return {
                "message": {"content": "actual"},
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }

    monkeypatch.setattr(telemetry, "_measurement_transaction", unavailable)
    result = await telemetry.complete(db, Model(), ex, cid, payload)
    assert result["message"]["content"] == "actual" and result["usage"]["prompt_tokens"] == 10


def test_price_calculation_uses_cache_and_does_not_add_currencies():
    price = {
        "model": "m",
        "currency": "USD",
        "as_of": "2026-10-31",
        "source": "offline-snapshot",
        "input_cache_hit_per_million": "0.003",
        "input_cache_miss_per_million": "0.15",
        "output_per_million": "0.6",
    }
    attempt = {
        "model": "m",
        "dispatched": True,
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "prompt_cache_hit_tokens": 800,
            "prompt_cache_miss_tokens": 200,
        },
    }
    assert costs([attempt], {"m": price})["complete_cost"] == "0.0000924"
    other = {**attempt, "model": "other"}
    result = costs(
        [attempt, other], {"m": price, "other": {**price, "model": "other", "currency": "EUR"}}
    )
    assert result["complete_cost"] is None and result["known_cost"] is None
    captured = {**attempt, "price_captured": True, "price_snapshot": price}
    changed = {**price, "output_per_million": "99"}
    assert costs([captured], {"m": changed})["complete_cost"] == "0.0000924"
    assert costs([{**captured, "price_snapshot": None}], {"m": price})["complete_cost"] is None
