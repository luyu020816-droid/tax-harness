import pytest
from test_conversation_context import settled

from app import conversation_index as index
from app.conversation_jobs import ConversationJobs
from app.errors import AppError
from app.identity import Identity, create_session
from app.session_management import organize
from app.values import digest

AUTH = Identity("U001")


class IndexEngine:
    def profile(self):
        return "program-test-not-real-embedding", {"dimension": 1024, "test": True}

    def count(self, text):
        return len(text)

    def embed(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]


@pytest.fixture
def indexer(db, settings):
    service = ConversationJobs(db, settings, IndexEngine())
    service.configure()
    yield service
    with db.transaction() as conn:
        conn.execute("UPDATE conversation_index_profiles SET enabled=false")


def test_chat_chunks_cover_original_unicode_and_include_prefix_budget():
    text = "备份😀不得外发；" * 200
    pieces = index.chunks(text, "user", len)
    assert pieces[0][0] == 0 and pieces[-1][1] == len(text)
    last = 0
    for begin, end, excerpt in pieces:
        assert begin <= last and end > last
        assert excerpt == text[begin:end]
        assert len(index.embedding_text("user", excerpt)) <= 1200
        last = end
    assert index.chunks("", "user", len) == []


def test_index_jobs_are_transactional_and_vectors_publish_atomically(db, indexer):
    session = create_session(db, AUTH, "C001")["session_id"]
    ids = settled(db, session, "架构升级方案：先备份，不授权保存。")
    assert indexer.backfill() == 0
    for _ in range(2):
        job = indexer.claim()
        assert job
        indexer.run(job)
    with db.transaction(readonly=True) as conn:
        jobs = conn.execute(
            "SELECT * FROM conversation_index_jobs WHERE source_id=ANY(%s::uuid[])", (list(ids),)
        ).fetchall()
        assert len(jobs) == 2 and all(j["state"] == "ready" for j in jobs)
        assert all(j["published_generation_id"] == j["target_generation_id"] for j in jobs)
        assert conn.execute("SELECT count(*) n FROM knowledge_documents").fetchone()["n"] == 0
        for source_id in ids:
            p = conn.execute(
                "SELECT * FROM conversation_passages WHERE source_id=%s", (source_id,)
            ).fetchone()
            source = conn.execute(
                "SELECT content FROM sources WHERE source_id=%s", (source_id,)
            ).fetchone()
            assert p["excerpt_hash"] == digest(source["content"][p["char_start"] : p["char_end"]])
    assert indexer.claim() is None


@pytest.mark.parametrize("change", ["delete", "revoke", "suppress"])
def test_late_index_result_cannot_publish_after_invalidation(db, admin, indexer, change):
    session = create_session(db, AUTH, "C001")["session_id"]
    settled(db, session, "历史原文：不授权保存。")
    job = indexer.claim()
    original = indexer.engine.embed

    def invalidate(texts):
        if change == "delete":
            organize(db, AUTH, session, "delete", 1)
        elif change == "revoke":
            admin.execute(
                "DELETE FROM user_company_grants WHERE user_id='U001' AND company_id='C001'"
            )
        else:
            admin.execute(
                "UPDATE sources SET recall_state='suppressed' WHERE source_id=%s",
                (job["source_id"],),
            )
        return original(texts)

    indexer.engine.embed = invalidate
    indexer.run(job)
    with db.transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM conversation_index_jobs WHERE source_id=%s", (job["source_id"],)
        ).fetchone()
        assert row["state"] == "cancelled" and row["published_generation_id"] is None
        assert conn.execute("SELECT count(*) n FROM conversation_passages").fetchone()["n"] == 0


def test_expired_worker_cannot_replace_new_owner_and_retries_are_bounded(db, admin, indexer):
    session = create_session(db, AUTH, "C001")["session_id"]
    settled(db, session, "索引故障测试。")
    old = indexer.claim()
    admin.execute(
        "UPDATE conversation_index_jobs SET lease_until=now()-interval '1 second' WHERE source_id=%s",
        (old["source_id"],),
    )
    current = indexer.claim()
    assert current["fencing_token"] > old["fencing_token"]
    indexer.run(old)
    with db.transaction(readonly=True) as conn:
        assert conn.execute("SELECT count(*) n FROM conversation_passages").fetchone()["n"] == 0
    indexer.run(current)
    job = indexer.claim()
    indexer.engine.embed = lambda texts: (_ for _ in ()).throw(
        AppError("DEPENDENCY_UNAVAILABLE", "test", 503)
    )
    for number in range(3):
        indexer.run(job)
        if number != 2:
            admin.execute(
                "UPDATE conversation_index_jobs SET next_retry_at=now() WHERE source_id=%s",
                (job["source_id"],),
            )
            job = indexer.claim()
    with db.transaction(readonly=True) as conn:
        assert (
            conn.execute(
                "SELECT count(*) n FROM conversation_index_jobs WHERE state='failed'"
            ).fetchone()["n"]
            == 1
        )


class RankedEngine(IndexEngine):
    calls = 0

    def embed(self, texts):
        self.calls += 1
        return super().embed(texts)

    def rerank(self, query, texts):
        return [5.0 if "迁移" in text else -5.0 for text in texts]


@pytest.fixture
def ranked(db, settings, tmp_path):
    import json

    from app.conversation_jobs import ConversationJobs

    engine = RankedEngine()
    jobs = ConversationJobs(db, settings, engine)
    profile = jobs.configure()
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "profile_id": profile,
                "algorithm": "dense-rerank-v1",
                "dense_min": 0.5,
                "rerank_min": 0,
                "development_hash": "program-test-only",
            }
        ),
        encoding="utf-8",
    )
    config = settings.model_copy(
        update={"conversation_semantic_mode": "on", "conversation_selection_policy": policy}
    )
    yield jobs, engine, config
    with db.transaction() as conn:
        conn.execute("UPDATE conversation_index_profiles SET enabled=false")


def drain(jobs):
    while job := jobs.claim():
        jobs.run(job)


def observer(user="U001"):
    from types import SimpleNamespace

    return SimpleNamespace(auth=Identity(user), source_seq=10**9)


def test_semantic_only_finds_different_words_and_does_not_guess_no_answer(db, ranked):
    from app.conversation_history import search

    jobs, engine, config = ranked
    session = create_session(db, AUTH, "C001")["session_id"]
    source, _ = settled(db, session, "数据库迁移要先备份，保留回退副本。")
    drain(jobs)
    result = search(db, observer(), query="数据库搬家", settings=config, engine=engine)
    assert result["semantic_search"] and result["matches"][0]["source_id"] == source
    assert result["status"] == "ok"
    engine.rerank = lambda query, texts: [-5.0] * len(texts)
    result = search(db, observer(), query="保险箱密码", settings=config, engine=engine)
    assert result["status"] == "not_found" and result["matches"] == []


def test_semantic_search_has_pre_filtered_ownership_and_current_invalidation(db, ranked):
    from app.conversation_history import search

    jobs, engine, config = ranked
    session = create_session(db, AUTH, "C001")["session_id"]
    settled(db, session, "数据库迁移的私密原文。")
    drain(jobs)
    assert (
        search(db, observer("U002"), query="搬家", settings=config, engine=engine)["matches"] == []
    )
    organize(db, AUTH, session, "delete", 1)
    assert search(db, observer(), query="搬家", settings=config, engine=engine)["matches"] == []


def test_ranked_cursor_is_stable_bound_and_does_not_repeat_inference(db, ranked):
    from app.conversation_history import search

    jobs, engine, config = ranked
    session = create_session(db, AUTH, "C001")["session_id"]
    for number in range(4):
        settled(db, session, f"迁移方案第{number}项，仅讨论。")
    drain(jobs)
    first = search(db, observer(), query="搬家", page_size=1, settings=config, engine=engine)
    count = engine.calls
    second = search(
        db,
        observer(),
        query="搬家",
        page_size=1,
        cursor=first["next_cursor"],
        settings=config,
        engine=engine,
    )
    assert second["matches"][0]["source_id"] != first["matches"][0]["source_id"]
    assert count == engine.calls
    with pytest.raises(AppError):
        search(
            db,
            observer("U002"),
            query="搬家",
            page_size=1,
            cursor=first["next_cursor"],
            settings=config,
            engine=engine,
        )
    with pytest.raises(AppError):
        search(
            db,
            observer(),
            query="修改查询",
            page_size=1,
            cursor=first["next_cursor"],
            settings=config,
            engine=engine,
        )


def test_partial_index_and_unverified_policy_are_not_reported_as_not_found(db, ranked, tmp_path):
    from app.conversation_history import search

    jobs, engine, config = ranked
    session = create_session(db, AUTH, "C001")["session_id"]
    settled(db, session, "迁移原文尚未索引。")
    result = search(db, observer(), query="搬家", settings=config, engine=engine)
    assert result["status"] == "partial" and result["coverage"]["pending"] == 2
    config = config.model_copy(update={"conversation_selection_policy": tmp_path / "missing.json"})
    result = search(db, observer(), query="搬家", settings=config, engine=engine)
    assert result["status"] == "partial" and not result["semantic_search"]
    assert result["degraded_reason"] == "SELECTION_POLICY_UNAVAILABLE"


def test_shadow_computes_diagnostics_but_does_not_expose_semantic_matches(db, ranked):
    from app.conversation_history import search

    jobs, engine, config = ranked
    session = create_session(db, AUTH, "C001")["session_id"]
    settled(db, session, "数据库迁移要先备份。")
    drain(jobs)
    config = config.model_copy(update={"conversation_semantic_mode": "shadow"})
    result = search(db, observer(), query="搬家", settings=config, engine=engine)
    assert not result["semantic_search"] and not result["matches"]
    with db.transaction(readonly=True) as conn:
        saved = conn.execute(
            "SELECT query_plan FROM history_queries WHERE query_id=%s", (result["query_id"],)
        ).fetchone()
        assert saved["query_plan"]["shadow_query_id"]


def test_exact_invoice_number_is_not_confused_with_similar_identifier(db, ranked):
    from app.conversation_history import search

    jobs, engine, config = ranked
    session = create_session(db, AUTH, "C001")["session_id"]
    settled(db, session, "F0020迁移归档，不是F003。")
    drain(jobs)
    result = search(db, observer(), query="F002原来的安排", settings=config, engine=engine)
    assert result["matches"] == []


def test_neighbor_navigation_is_metadata_only_and_revalidates_parent(db):
    from app.conversation_history import evidence_in, read_page

    session = create_session(db, AUTH, "C001")["session_id"]
    user, assistant = settled(db, session, "迁移方案原始要求。", "先备份，再迁移。")
    following, _ = settled(db, session, "我同意第二套方案，但现在不授权保存。")
    result = read_page(db, observer(), source_id=assistant)
    assert {n["source_id"] for n in result["neighbors"]} == {user, following}
    assert all("content" not in n and "excerpt" not in n for n in result["neighbors"])
    with db.transaction(readonly=True) as conn:
        assert evidence_in(conn, AUTH, result["citation_id"])["neighbors"] == result["neighbors"]
    organize(db, AUTH, session, "delete", 1)
    with pytest.raises(AppError):
        with db.transaction(readonly=True) as conn:
            evidence_in(conn, AUTH, result["citation_id"])


def test_neighbor_navigation_preserves_explicit_date_window(db, admin):
    from datetime import UTC, datetime, timedelta

    from app.conversation_history import evidence_in, search

    session = create_session(db, AUTH, "C001")["session_id"]
    older, _ = settled(db, session, "昨天旧方案。")
    current, _ = settled(db, session, "今天方案TODAY-653。")
    begin = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    admin.execute(
        "UPDATE sources SET created_at=%s WHERE source_id=%s", (begin - timedelta(hours=1), older)
    )
    result = search(
        db,
        observer(),
        query="TODAY-653",
        date_from=begin.isoformat(),
        date_to=(begin + timedelta(days=1)).isoformat(),
    )
    with db.transaction(readonly=True) as conn:
        evidence = evidence_in(conn, AUTH, result["matches"][0]["citation_id"])
    assert current == evidence["source_id"]
    assert older not in {n["source_id"] for n in evidence["neighbors"]}


def test_company_id_filters_metadata_instead_of_requiring_literal_prose(db, ranked):
    from app.conversation_history import search

    jobs, engine, config = ranked
    session = create_session(db, AUTH, "C001")["session_id"]
    source, _ = settled(db, session, "甲企业迁移要先备份。")
    drain(jobs)
    result = search(db, observer(), query="C001搬家", settings=config, engine=engine)
    assert result["matches"][0]["source_id"] == source
    with pytest.raises(AppError, match="无权"):
        search(db, observer("U002"), query="乙企业搬家", settings=config, engine=engine)


def test_legacy_cursor_stays_chronological_after_semantics_enabled(db, ranked):
    from app.conversation_history import search

    jobs, engine, config = ranked
    session = create_session(db, AUTH, "C001")["session_id"]
    sources = [settled(db, session, f"LEGACY-PAGE-863 迁移第{n}条。")[0] for n in range(3)]
    first = search(db, observer(), query="LEGACY-PAGE-863", page_size=1, mode="lexical")
    before = engine.calls
    second = search(
        db,
        observer(),
        query="LEGACY-PAGE-863",
        page_size=1,
        cursor=first["next_cursor"],
        settings=config,
        engine=engine,
    )
    assert second["matches"][0]["source_id"] == sources[1]
    assert before == engine.calls and not second["semantic_search"]


@pytest.mark.asyncio
async def test_index_processing_renews_lease_during_slow_model(db, indexer, settings):
    import time

    indexer.settings = settings.model_copy(update={"knowledge_lease_seconds": 3})
    session = create_session(db, AUTH, "C001")["session_id"]
    settled(db, session, "长推理需要续租。")
    original = indexer.engine.embed

    def slow(texts):
        time.sleep(4)
        return original(texts)

    indexer.engine.embed = slow
    job = indexer.claim()
    await indexer.process(job)
    with db.transaction(readonly=True) as conn:
        assert (
            conn.execute(
                "SELECT state FROM conversation_index_jobs WHERE source_id=%s", (job["source_id"],)
            ).fetchone()["state"]
            == "ready"
        )


def test_semantic_excerpt_is_query_centered_and_bounded_without_cutting_original(db, ranked):
    from app.conversation_history import read_page, search

    jobs, engine, config = ranked
    session = create_session(db, AUTH, "C001")["session_id"]
    raw = "迁移背景" * 80 + "最后一步：保留离线回退副本。" + "普通背景" * 250
    source, _ = settled(db, session, raw)
    drain(jobs)
    result = search(db, observer(), query="最后一步", settings=config, engine=engine)
    item = next(m for m in result["matches"] if m["source_id"] == source)
    assert len(item["content"]) <= 240 and "离线回退副本" in item["content"]
    assert item["content"] == raw[item["locator"]["char_start"] : item["locator"]["char_end"]]
    assert (
        read_page(db, observer(), source_id=source, char_start=item["next_char_start"])["excerpt"]
        == raw[item["next_char_start"] : item["next_char_start"] + 1200]
    )


def test_reference_input_contains_only_currently_callable_tools(db, settings, execution_factory):
    from test_compaction import model_settings

    from app.context import assemble

    execution = execution_factory("查历史并引用，不修改。")
    before, _, _ = assemble(db, model_settings(settings), execution, [])
    execution.reference_context = True
    after, _, _ = assemble(db, model_settings(settings), execution, [])
    available = {tool["function"]["name"]: tool["function"] for tool in after["tools"]}
    assert not {"prepare_invoice_write", "update_invoice", "change_task"} & available.keys()
    assert {
        "get_invoice_operation",
        "read_conversation",
        "search_conversations",
    } <= available.keys()
    assert available["manage_preference"]["parameters"]["properties"]["action"]["enum"] == ["list"]
    assert len(str(after["tools"])) < len(str(before["tools"]))


def test_retrieval_diagnostics_follow_run_content_visibility(db, admin, execution_factory):
    from test_insights import context as record_test_context

    from app import insights
    from app.telemetry import phase

    execution = execution_factory("原文诊断权限。")
    record_test_context(db, execution)
    with phase(db, execution, "diagnostic-test", "retrieval", "聊天融合与支持度") as span:
        span["attributes"].update(
            profile_id="program-profile",
            candidate_count=3,
            coverage={"eligible": 2, "indexed": 2, "pending": 0},
        )
    detail = insights.detail(db, AUTH, execution.turn_id)
    node = next(s for s in detail["runs"][0]["spans"] if s["kind"] == "retrieval")
    assert node["attributes"]["retrieval"]["candidate_count"] == 3
    admin.execute("DELETE FROM user_company_grants WHERE user_id='U001' AND company_id='C001'")
    try:
        withdrawn = insights.detail(db, AUTH, execution.turn_id)
    except AppError as error:
        assert error.code in ("ACCESS_DENIED", "RESOURCE_NOT_FOUND")
    else:
        assert all(
            "retrieval" not in s["attributes"] for run in withdrawn["runs"] for s in run["spans"]
        )
