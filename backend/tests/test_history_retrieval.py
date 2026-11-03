"""Manual-only product contract; prior positive history tests are archived in evals/legacy-history."""

import pytest

from app.errors import AppError
from app.history_jobs import HistoryJobs, passages
from app.history_retrieval import HistoryRetrieval
from app.identity import Identity


@pytest.mark.parametrize(
    "args",
    [
        {"query": "old conversation"},
        {"company_id": "C001", "date_from": "2026-09-01", "date_to": "2026-10-01"},
        {"mode": "lexical", "query": "old"},
        {"mode": "hybrid", "query": "old"},
        {"mode": "date", "date_from": "bad", "date_to": "2026-10-01"},
        {"cursor": "invalid", "query": "old"},
    ],
)
def test_all_history_search_modes_are_disabled_even_with_old_sources(
    db, settings, source_factory, args
):
    source_factory("An old conversation is not a submitted knowledge document")
    with pytest.raises(AppError) as error:
        HistoryRetrieval(db, settings, None).search(Identity("U001"), **args)
    assert error.value.code == "HISTORY_RETRIEVAL_DISABLED"


@pytest.mark.asyncio
async def test_history_worker_does_not_start_under_legacy_flags(db, settings):
    jobs = HistoryJobs(db, settings.model_copy(update={"history_enabled": True}), None)
    await jobs.start()
    assert jobs.task is None and jobs.claim() is None
    await jobs.stop()


def test_passages_have_verifiable_offsets_and_no_silent_truncation():
    text = "开头🙂" + "甲乙丙丁" * 900 + "结尾🙂"
    parts = passages(text, len)
    assert parts[0][0] == 0 and parts[-1][1] == len(text)
    assert all(text[a:b] == body and len(body) <= 600 for a, b, body in parts)
    assert all(b >= c for (_, b, _), (c, _, _) in zip(parts, parts[1:], strict=False))


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
