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
