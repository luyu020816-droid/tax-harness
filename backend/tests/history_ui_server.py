"""Live-model browser fixture on 8106, in its own DB. Never resets the preview DB."""

import psycopg
import uvicorn
from psycopg.conninfo import make_conninfo

from app.api import create_app
from app.cli import initialize
from app.config import ROOT, Settings
from app.db import Database
from app.history_jobs import HistoryJobs
from app.identity import Identity, create_session
from app.retrieval_engine import LocalRetrieval
from app.sources import add_source, next_sequence
from app.values import uid

settings = Settings()
name = "tax_harness_history_ui"
settings = settings.model_copy(
    update={
        "database_url": make_conninfo(settings.database_url, dbname=name),
        "admin_database_url": make_conninfo(settings.admin_database_url, dbname=name),
        "ingest_database_url": "",
    }
)
assert settings.model_configured
with psycopg.connect(
    make_conninfo(settings.admin_database_url, dbname="postgres"), autocommit=True
) as conn:
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
        conn.execute("CREATE DATABASE tax_harness_history_ui")
initialize(settings)
db = Database(settings.database_url)
db.open()
auth = Identity("U001")
session = create_session(db, auth, "C001")
with db.transaction() as conn:
    conn.execute(
        "UPDATE sources SET recall_state='suppressed' WHERE turn_id IN (SELECT turn_id FROM turns WHERE request_hash='history-ui-fixture')"
    )
    sequence, turn = next_sequence(conn, auth), uid()
    content = "🙂合成历史联调样例：备用投影仪放在西侧器材柜的第二层，归还时记录使用人。"
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,user_id,client_request_id,request_hash,source_seq,text,status,final_message,created_at) VALUES(%s,%s,'U001',%s,'history-ui-fixture',%s,%s,'completed','合成历史记录','2026-09-03 10:00+08')",
        (turn, session["session_id"], uid(), sequence, content),
    )
    source = add_source(conn, auth, content, turn_id=turn, source_seq=sequence, companies=["C001"])
    conn.execute(
        "UPDATE sources SET created_at='2026-09-03 10:00+08' WHERE source_id=%s",
        (source["source_id"],),
    )
engine = LocalRetrieval(settings.knowledge_model_directory)
jobs = HistoryJobs(db, settings, engine)
job = jobs.claim()
while job:
    jobs.run(job)
    job = jobs.claim()
app = create_app(settings, db)
# Share the already loaded engine and the same core implementations.
app.state.runtime.tools.history.engine = engine
app.state.runtime.tools.retrieval.engine = engine
app.state.history_jobs.engine = engine
app.state.knowledge_jobs.engine = engine


@app.post("/__test/history/suppress")
def suppress_fixture():
    # This endpoint exists only in this explicit, isolated browser test server.
    assert name == "tax_harness_history_ui"
    with db.transaction() as conn:
        conn.execute("SELECT user_id FROM users WHERE user_id='U001' FOR UPDATE")
        conn.execute(
            "UPDATE sources SET recall_state='suppressed' WHERE source_id=%s AND content_hash=%s AND user_id='U001'",
            (source["source_id"], source["content_hash"]),
        )
        conn.execute("UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id='U001'")
        conn.execute(
            "UPDATE summaries SET status='invalidated' WHERE user_id='U001' AND status='active'"
        )
    return {"suppressed": True}


if __name__ == "__main__":
    (ROOT / ".local/evidence/m5-history-ui").mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host="127.0.0.1", port=8106)
