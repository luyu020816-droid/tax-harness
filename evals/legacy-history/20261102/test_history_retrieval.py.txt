"""Protocol/authority tests use synthetic vectors, not semantic-quality claims."""

import pytest
from test_knowledge import ProgramEngine

from app.errors import AppError
from app.history_jobs import HistoryJobs, passages
from app.history_retrieval import HistoryRetrieval
from app.identity import Identity
from app.preferences import change_preference
from app.values import uid

AUTH = Identity("U001")


@pytest.fixture
def history(db, settings):
    engine = ProgramEngine()
    policy = {"profile_id": "a" * 64, "mode": "hybrid", "cutoff": 0, "policy_id": "c" * 64}
    return HistoryJobs(db, settings, engine), HistoryRetrieval(
        db, settings, engine, selection_policy=policy
    )


def drain(jobs):
    while job := jobs.claim():
        jobs.run(job)


def test_date_browse_without_query_works_without_vectors(history, source_factory, admin):
    jobs, retrieval = history
    ids = []
    for i in range(8):
        source = source_factory(f"九月历史消息{i}")
        ids.append(str(source["source_id"]))
    admin.execute("UPDATE sources SET created_at='2026-09-01 00:30+08'")
    args = {"company_id": "C001", "date_from": "2026-09-01", "date_to": "2026-10-01"}
    first = retrieval.search(AUTH, **args)
    assert first["retrieval_mode"] == "date" and len(first["matches"]) == 5 and first["has_more"]
    second = retrieval.search(AUTH, **args, cursor=first["next_cursor"])
    assert [r["source_id"] for r in first["matches"] + second["matches"]] == list(reversed(ids))
    assert not second["has_more"]
    with pytest.raises(AppError):
        retrieval.search(AUTH, **{**args, "company_id": "C002"}, cursor=first["next_cursor"])


@pytest.mark.parametrize(
    "start,end",
    [
        (None, None),
        ("2026-08-01", "2026-10-01"),
        ("2026-10-02", "2026-10-01"),
        ("bad", "2026-10-01"),
    ],
)
def test_date_browse_requires_bounded_valid_window(history, start, end):
    with pytest.raises(AppError) as error:
        history[1].search(AUTH, date_from=start, date_to=end)
    assert error.value.code == "INVALID_ARGUMENT"


def test_semantic_results_are_persistent_and_role_correct(history, source_factory, admin):
    jobs, retrieval = history
    source = source_factory("备份设备采用异地存储，保管编号F072。")
    admin.execute(
        "UPDATE sources SET role='assistant',created_at=now()-interval '40 days' WHERE source_id=%s",
        (source["source_id"],),
    )
    drain(jobs)
    result = retrieval.search(AUTH, company_id="C001", query="备份设备", entity="F072")
    assert result["retrieval_mode"] == "hybrid", result
    assert result["status"] == "ok" and result["coverage"]["pending"] == 0
    citation = result["evidence"][0]["citation_id"]
    fresh = HistoryRetrieval(retrieval.db, retrieval.settings, retrieval.engine)
    evidence = fresh.read(AUTH, citation)
    assert evidence["role"] == "assistant" and evidence["historical_only"]
    assert evidence["excerpt"] == source["content"]
    with pytest.raises(AppError):
        fresh.read(Identity("U002"), citation)


def test_stale_vectors_and_citations_cannot_revive_deleted_source(history, source_factory, admin):
    jobs, retrieval = history
    source = source_factory("回复使用纯文字，不要图表。")
    drain(jobs)
    initial = retrieval.search(AUTH, company_id="C001", query="回复格式")
    citation = initial["evidence"][0]["citation_id"]
    admin.execute(
        "UPDATE sources SET recall_state='suppressed' WHERE source_id=%s", (source["source_id"],)
    )
    assert admin.execute("SELECT count(*) AS n FROM history_embeddings").fetchone()["n"] > 0
    assert not retrieval.search(AUTH, company_id="C001", query="回复格式")["evidence"]
    with pytest.raises(AppError) as error:
        retrieval.read(AUTH, citation)
    assert error.value.code == "SOURCE_INVALIDATED"


def test_formal_preference_deletion_invalidates_old_history(history, db, source_factory):
    jobs, retrieval = history
    pref = change_preference(
        db, AUTH, action="remember", key="response_style", value="简明扼要", request_id=uid()
    )
    drain(jobs)
    first = retrieval.search(AUTH, query="简明扼要")
    assert first["evidence"]
    change_preference(
        db,
        AUTH,
        action="delete",
        target_id=str(pref["preference_id"]),
        expected_revision=pref["revision"],
        request_id=uid(),
    )
    with pytest.raises(AppError):
        retrieval.read(AUTH, first["evidence"][0]["citation_id"])
    with db.transaction(readonly=True) as conn:
        assert not conn.execute("SELECT 1 FROM preferences WHERE status='active'").fetchone()


def test_exact_entity_company_user_and_input_watermark_filter(history, source_factory):
    jobs, retrieval = history
    source_factory("F002归档由财务审核", company="C001")
    bound = source_factory("F003归档由仓库审核", company="C001")
    source_factory("F002乙企业其他要求", company="C002")
    source_factory("F002另一用户私有内容", user="U002", company="C001")
    source_factory("F002晚于本轮读取时点", company="C001")
    drain(jobs)
    result = retrieval.search(
        AUTH, company_id="C001", query="归档", entity="F002", before_source_seq=bound["source_seq"]
    )
    assert len(result["evidence"]) == 1 and result["evidence"][0]["excerpt"] == "F002归档由财务审核"
    assert not retrieval.search(AUTH, query="F002", entity="F002")["evidence"]


def test_pending_index_reports_gap_without_claiming_never_said(history, source_factory):
    source_factory("报销资料每周五汇总。")
    result = history[1].search(AUTH, company_id="C001", query="报销资料")
    assert result["evidence"] and result["status"] == "partial"
    assert result["coverage"]["pending"] == 1 and result["retrieval_mode"] == "lexical_only"


def test_expired_or_suppressed_job_never_publishes(history, source_factory, admin):
    jobs, _ = history
    source = source_factory("有效的历史内容")
    job = jobs.claim()
    admin.execute(
        "UPDATE history_jobs SET lease_until=now()-interval '1 second' WHERE source_id=%s",
        (source["source_id"],),
    )
    jobs.run(job)
    assert not admin.execute("SELECT 1 FROM history_passages").fetchone()
    replacement = jobs.claim()
    assert replacement["fencing_token"] > job["fencing_token"]
    admin.execute(
        "UPDATE sources SET recall_state='suppressed' WHERE source_id=%s", (source["source_id"],)
    )
    jobs.run(replacement)
    assert not admin.execute("SELECT 1 FROM history_passages").fetchone()


def test_inference_failure_keeps_source_and_lexical_fallback(
    history, source_factory, admin, monkeypatch
):
    jobs, retrieval = history
    source_factory("发生依赖故障时仍保留原话。")
    monkeypatch.setattr(
        jobs.engine, "embed", lambda texts: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    drain(jobs)
    assert admin.execute("SELECT state FROM history_jobs").fetchone()["state"] == "blocked"
    assert retrieval.search(AUTH, company_id="C001", query="保留原话")["matches"]
    assert admin.execute("SELECT count(*) AS n FROM sources").fetchone()["n"] == 1


def test_passages_have_verifiable_offsets_and_no_silent_truncation():
    text = "开头🙂" + "甲乙丙丁" * 900 + "结尾🙂"
    parts = passages(text, len)
    assert parts[0][0] == 0 and parts[-1][1] == len(text)
    assert all(text[a:b] == body and len(body) <= 600 for a, b, body in parts)
    assert all(b >= c for (_, b, _), (c, _, _) in zip(parts, parts[1:], strict=False))


def test_date_budget_pagination_does_not_skip_large_messages(history, source_factory, admin):
    ids = [str(source_factory("历史长文" * 400 + str(i))["source_id"]) for i in range(7)]
    admin.execute("UPDATE sources SET created_at='2026-09-03 10:00+08'")
    cursor, found = None, []
    for _ in range(8):
        page = history[1].search(
            AUTH, company_id="C001", date_from="2026-09-01", date_to="2026-10-01", cursor=cursor
        )
        assert page["coverage"]["evidence_tokens"] <= 3000
        found.extend(r["source_id"] for r in page["matches"])
        if not page["has_more"]:
            break
        assert page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    assert found == list(reversed(ids))


def test_history_quote_cannot_use_unserved_source_tail(
    db, settings, source_factory, admin, execution_factory
):
    from test_answer_renderer import recorded, rendered

    source = source_factory("已入模的开头🙂" + "普通背景" * 500 + "未入模的末尾内容")
    admin.execute(
        "UPDATE sources SET created_at='2026-09-03 10:00+08' WHERE source_id=%s",
        (source["source_id"],),
    )
    execution = execution_factory("查甲企业九月份的历史消息")
    output, payload, manifest = recorded(
        db,
        settings,
        execution,
        "search_history",
        {"company_id": "C001", "date_from": "2026-09-01", "date_to": "2026-10-01"},
    )
    evidence = output["data"]["matches"][0]
    assert "未入模的末尾内容" not in evidence["content"]
    with pytest.raises(AppError) as error:
        rendered(
            db,
            execution,
            payload,
            manifest,
            [
                {
                    "type": "history_quote",
                    "source_id": evidence["source_id"],
                    "citation_id": evidence["citation_id"],
                    "quote": "未入模的末尾内容",
                }
            ],
        )
    assert error.value.code == "REPLY_EVIDENCE_MISSING"
    reply = rendered(
        db,
        execution,
        payload,
        manifest,
        [
            {
                "type": "history_quote",
                "source_id": evidence["source_id"],
                "citation_id": evidence["citation_id"],
                "quote": "已入模的开头🙂",
            }
        ],
    )
    assert reply.cards[0]["kind"] == "history_quote"
    assert "已入模的开头" not in reply.message
    two = rendered(
        db,
        execution,
        payload,
        manifest,
        [
            {
                "type": "history_quote",
                "source_id": evidence["source_id"],
                "citation_id": evidence["citation_id"],
                "quote": "已入模的开头🙂",
            },
            {
                "type": "history_quote",
                "source_id": evidence["source_id"],
                "citation_id": evidence["citation_id"],
                "quote": "普通背景",
            },
        ],
    )
    assert len(two.cards) == 2
    admin.execute(
        "UPDATE sources SET recall_state='suppressed' WHERE source_id=%s", (source["source_id"],)
    )
    with pytest.raises(AppError):
        rendered(
            db,
            execution,
            payload,
            manifest,
            [
                {
                    "type": "history_quote",
                    "source_id": evidence["source_id"],
                    "quote": "已入模的开头🙂",
                }
            ],
        )


def test_history_http_uses_same_query_and_citation_service(
    history, db, settings, source_factory, admin
):
    from fastapi.testclient import TestClient

    from app.api import create_app

    source_factory("历史HTTP原话与来源。")
    admin.execute("UPDATE sources SET created_at='2026-09-03 10:00+08'")
    app = create_app(settings, db, start_workers=False)
    with TestClient(app) as client:
        client.post("/api/v1/demo/login", json={"user_id": "U001"})
        response = client.post(
            "/api/v1/retrieval/search",
            json={
                "corpus": "history",
                "company_id": "C001",
                "date_from": "2026-09-01",
                "date_to": "2026-10-01",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        evidence = client.get("/api/v1/evidence/" + data["evidence"][0]["citation_id"])
        assert evidence.status_code == 200 and evidence.json()["role"] == "user"
        client.post("/api/v1/demo/login", json={"user_id": "U002"})
        denied = client.get("/api/v1/evidence/" + data["evidence"][0]["citation_id"])
        assert denied.status_code != 200


def test_source_change_during_inference_cannot_publish(history, source_factory, admin, monkeypatch):
    jobs, _ = history
    source = source_factory("推理期间发生更正的历史内容。")
    original = jobs.engine.embed

    def change(texts):
        vectors = original(texts)
        admin.execute(
            "UPDATE sources SET recall_state='suppressed' WHERE source_id=%s",
            (source["source_id"],),
        )
        return vectors

    monkeypatch.setattr(jobs.engine, "embed", change)
    drain(jobs)
    assert not admin.execute("SELECT 1 FROM history_passages").fetchone()


def test_profile_mismatch_is_gap_not_vector_mix(history, source_factory, monkeypatch):
    jobs, retrieval = history
    source_factory("个人电脑保管由小吴负责。")
    drain(jobs)
    monkeypatch.setattr(retrieval.engine, "profile", lambda: ("f" * 64, {"test_only": True}))
    result = retrieval.search(AUTH, company_id="C001", query="电脑保管")
    assert result["retrieval_mode"] == "lexical_only" and result["status"] == "partial"
    assert result["coverage"]["ready"] == 0 and result["coverage"]["pending"] == 1


def test_partial_history_tool_does_not_advertise_exhaustive_not_found(
    db, settings, source_factory, execution_factory
):
    from test_answer_renderer import recorded, rendered

    source_factory("原始历史记录仍在等待语义索引。")
    execution = execution_factory("查甲企业以前关于广州天气的原话")
    output, payload, manifest = recorded(
        db, settings, execution, "search_history", {"company_id": "C001", "query": "广州天气"}
    )
    data = output["data"]
    assert data["status"] == "partial" and not data["matches"]
    assert "not_found" not in data
    assert "history_partial" in data["allowed_answer_codes"]
    assert "history_not_found" not in data["allowed_answer_codes"]
    with pytest.raises(AppError):
        rendered(
            db, execution, payload, manifest, [{"type": "explanation", "code": "history_not_found"}]
        )
    reply = rendered(
        db, execution, payload, manifest, [{"type": "explanation", "code": "history_partial"}]
    )
    assert "不能证明过去没有说过" in reply.message


def test_long_message_can_be_read_to_the_end_without_inference(history, source_factory, admin):
    content = "🙂开头" + "原始长消息" * 700 + "末尾完整保留"
    source = source_factory(content)
    admin.execute(
        "UPDATE sources SET created_at='2026-09-03 10:00+08' WHERE source_id=%s",
        (source["source_id"],),
    )
    service = history[1]
    first = service.search(AUTH, company_id="C001", date_from="2026-09-01", date_to="2026-10-01")[
        "evidence"
    ][0]
    assert first["excerpt_truncated"] and first["total_characters"] == len(content)
    combined = first["excerpt"]
    citation = first["citation_id"]
    start = first["next_char_start"]
    while start is not None:
        piece = service.read(AUTH, citation, char_start=start)
        assert piece["locator"]["char_start"] == start
        combined += piece["excerpt"]
        citation, start = piece["citation_id"], piece["next_char_start"]
    assert combined == content
    with pytest.raises(AppError):
        service.read(Identity("U002"), citation, char_start=0)
    admin.execute(
        "UPDATE sources SET recall_state='suppressed' WHERE source_id=%s", (source["source_id"],)
    )
    with pytest.raises(AppError):
        service.read(AUTH, citation, char_start=0)


def test_model_rewrite_cannot_replace_original_history_question(db, execution_factory):
    from test_runtime import tool

    from app.tools import ToolExecutor

    original = "请查甲企业以前关于不存在的相机转接配件的原话"
    execution = execution_factory(original)
    output, _ = ToolExecutor(db).execute(
        execution,
        tool("search_history", {"company_id": "C001", "query": "相机 转接 配件"})["tool_calls"][0],
    )
    assert output["ok"] and output["data"]["not_found"]
    assert output["data"]["raw_user_query"] == original
    with db.transaction(readonly=True) as conn:
        plans = [
            r["query_plan"]["query"]
            for r in conn.execute("SELECT query_plan FROM history_queries").fetchall()
        ]
    assert original in plans and "相机 转接 配件" in plans


def test_history_passages_vectors_and_citations_are_in_snapshot(history, source_factory, admin):
    from app.recovery import _snapshot

    jobs, retrieval = history
    source_factory("用于快照核对的真实持久历史记录。")
    drain(jobs)
    result = retrieval.search(AUTH, company_id="C001", query="快照核对")
    assert result["evidence"]
    first = _snapshot(admin)
    for table in (
        "history_jobs",
        "history_passages",
        "history_embeddings",
        "history_queries",
        "history_evidence",
    ):
        assert first["tables"][table]["rows"] > 0
    admin.execute(
        "UPDATE history_evidence SET excerpt_hash='deliberately-invalid-for-snapshot-test'"
    )
    assert first["fingerprint"] != _snapshot(admin)["fingerprint"]


def test_partial_coverage_does_not_block_verified_history_quote(
    db, settings, source_factory, execution_factory
):
    from test_answer_renderer import recorded, rendered

    source_factory("备用投影仪放在西侧器材柜。")
    execution = execution_factory("查甲企业以前提过的备用投影仪位置")
    output, payload, manifest = recorded(
        db, settings, execution, "search_history", {"company_id": "C001", "query": "备用投影仪位置"}
    )
    data = output["data"]
    assert data["status"] == "partial" and data["allow_history_quotes"]
    evidence = data["matches"][0]
    reply = rendered(
        db,
        execution,
        payload,
        manifest,
        [
            {
                "type": "history_quote",
                "source_id": evidence["source_id"],
                "citation_id": evidence["citation_id"],
                "quote": "备用投影仪放在西侧器材柜。",
            },
            {"type": "explanation", "code": "history_partial"},
        ],
    )
    assert reply.cards[0]["kind"] == "history_quote"


def test_joint_company_history_requires_all_grants(history, db, admin):
    from app.sources import add_source, next_sequence

    with db.transaction() as conn:
        add_source(
            conn,
            AUTH,
            "比较甲企业和乙企业后，约定月底统一核对。",
            source_seq=next_sequence(conn, AUTH),
            companies=["C001", "C002"],
        )
    jobs, retrieval = history
    drain(jobs)
    assert not retrieval.search(AUTH, company_id="C001", query="月底核对")["evidence"]
    assert not retrieval.search(AUTH, query="月底核对")["evidence"]
    joint = retrieval.search(AUTH, company_ids=["C001", "C002"], query="月底核对")
    evidence = joint["evidence"][0]
    assert evidence["company_ids"] == ["C001", "C002"]
    from app.knowledge_admin import revoke_read

    revoke_read(admin, "U001", "C002", uid())
    with pytest.raises(AppError):
        retrieval.read(AUTH, evidence["citation_id"])
    with pytest.raises(AppError):
        retrieval.search(AUTH, company_ids=["C001", "C002"], query="月底核对")


def test_joint_history_tool_records_scope_and_requires_explicit_request(
    db, settings, execution_factory
):
    from test_answer_renderer import recorded
    from test_runtime import tool

    from app.sources import add_source, next_sequence
    from app.tools import ToolExecutor

    with db.transaction() as conn:
        add_source(
            conn,
            AUTH,
            "比较甲企业和乙企业后，约定月底统一核对。",
            source_seq=next_sequence(conn, AUTH),
            companies=["C001", "C002"],
        )
    ex = execution_factory("查甲企业和乙企业以前比较的历史原话")
    output, _, _ = recorded(
        db, settings, ex, "search_history", {"company_ids": ["C001", "C002"], "query": "比较"}
    )
    assert output["data"]["matches"]
    with db.transaction(readonly=True) as conn:
        scopes = conn.execute(
            "SELECT company_id FROM source_scopes WHERE source_id=%s ORDER BY company_id",
            (output["result_ref"],),
        ).fetchall()
    assert [r["company_id"] for r in scopes] == ["C001", "C002"]
    other = execution_factory("查我以前的历史")
    denied, _ = ToolExecutor(db).execute(
        other,
        tool("search_history", {"company_ids": ["C001", "C002"], "query": "比较"})["tool_calls"][0],
    )
    assert not denied["ok"] and denied["error"]["code"] == "TARGET_SCOPE_MISMATCH"


def test_malformed_history_cursor_is_argument_error(history):
    with pytest.raises(AppError) as error:
        history[1].search(
            AUTH, company_id="C001", date_from="2026-09-01", date_to="2026-10-01", cursor="-" * 36
        )
    assert error.value.code == "INVALID_ARGUMENT"


def complete_fixture_reply(db, execution, reply):
    from app.values import jsonb

    with db.transaction() as conn:
        conn.execute(
            "UPDATE turns SET status='completed',final_message=%s,final_cards=%s WHERE turn_id=%s",
            (reply.message, jsonb(reply.cards), execution.turn_id),
        )


def in_same_session(db, execution, session_id):
    execution.session_id = session_id
    with db.transaction() as conn:
        conn.execute(
            "UPDATE turns SET session_id=%s WHERE turn_id=%s", (session_id, execution.turn_id)
        )
    return execution


def test_date_navigation_resumes_only_published_page_and_reaches_end(
    db, settings, source_factory, admin, execution_factory
):
    import json

    from test_answer_renderer import recorded, rendered
    from test_runtime import model_settings

    from app import context
    from app.history_retrieval import HistoryRetrieval, local_engine

    for i in range(7):
        source_factory(f"九月分页原话{i}")
    admin.execute("UPDATE sources SET created_at='2026-09-03 10:00+08'")
    first = execution_factory("列出甲企业九月历史第一页")
    output, payload, manifest = recorded(
        db,
        settings,
        first,
        "search_history",
        {"company_id": "C001", "date_from": "2026-09-01", "date_to": "2026-10-01"},
    )
    reply = rendered(
        db,
        first,
        payload,
        manifest,
        [{"type": "result", "result_ref": output["result_ref"], "view": "history_page"}],
    )
    assert len([c for c in reply.cards if c["kind"] == "history_quote"]) == 5
    complete_fixture_reply(db, first, reply)
    # A later query which was never published must not replace the visible page.
    service = HistoryRetrieval(db, settings, local_engine(str(settings.knowledge_model_directory)))
    service.search(
        AUTH,
        company_id="C001",
        date_from="2026-08-01",
        date_to="2026-08-02",
        session_id=first.session_id,
        origin_turn_id=first.turn_id,
    )
    second = in_same_session(db, execution_factory("继续下一页"), first.session_id)
    request, _, _ = context.assemble(db, model_settings(settings), second, [])
    state = json.loads(request["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
    args = state["history_navigation"]["page"]["arguments"]
    assert args["cursor"] == output["data"]["query_id"]
    next_output, next_payload, next_manifest = recorded(
        db, settings, second, "search_history", args
    )
    next_reply = rendered(
        db,
        second,
        next_payload,
        next_manifest,
        [{"type": "result", "result_ref": next_output["result_ref"], "view": "history_page"}],
    )
    assert len([c for c in next_reply.cards if c["kind"] == "history_quote"]) == 2
    complete_fixture_reply(db, second, next_reply)
    third = in_same_session(db, execution_factory("再下一页"), first.session_id)
    request, _, _ = context.assemble(db, model_settings(settings), third, [])
    state = json.loads(request["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
    terminal, terminal_payload, terminal_manifest = recorded(
        db, settings, third, "search_history", state["history_navigation"]["page"]["arguments"]
    )
    end = rendered(
        db,
        third,
        terminal_payload,
        terminal_manifest,
        [{"type": "result", "result_ref": terminal["result_ref"], "view": "history_page"}],
    )
    assert "末页" in end.message and not [c for c in end.cards if c["kind"] == "history_quote"]


def test_long_history_navigation_keeps_reference_without_copying_body(
    db, settings, source_factory, admin, execution_factory
):
    import json

    from test_answer_renderer import recorded, rendered
    from test_runtime import model_settings

    from app import context

    source = source_factory("开头" + "历史正文" * 400 + "末尾关键原话")
    admin.execute(
        "UPDATE sources SET created_at='2026-09-03 10:00+08' WHERE source_id=%s",
        (source["source_id"],),
    )
    first = execution_factory("读甲企业九月那条历史")
    output, payload, manifest = recorded(
        db,
        settings,
        first,
        "search_history",
        {"company_id": "C001", "date_from": "2026-09-01", "date_to": "2026-10-01"},
    )
    reply = rendered(
        db,
        first,
        payload,
        manifest,
        [{"type": "result", "result_ref": output["result_ref"], "view": "history_page"}],
    )
    complete_fixture_reply(db, first, reply)
    second = in_same_session(db, execution_factory("继续读这一条的剩余内容"), first.session_id)
    request, _, _ = context.assemble(db, model_settings(settings), second, [])
    state = json.loads(request["messages"][0]["content"].split("\n正式状态：\n", 1)[1])
    ref = state["history_navigation"]["citations"][0]
    assert ref["next_char_start"] == 1200 and "content" not in ref
    piece, payload, manifest = recorded(
        db,
        settings,
        second,
        "read_history",
        {"citation_id": ref["citation_id"], "char_start": ref["next_char_start"]},
    )
    evidence = piece["data"]
    end = rendered(
        db,
        second,
        payload,
        manifest,
        [
            {
                "type": "history_quote",
                "source_id": evidence["source_id"],
                "citation_id": evidence["citation_id"],
                "quote": "末尾关键原话",
            }
        ],
    )
    assert end.cards[0]["kind"] == "history_quote"
