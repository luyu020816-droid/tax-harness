from psycopg import sql

from app.config import ROOT
from app.values import uid


def test_schema_upgrade_fences_legacy_work_and_keeps_records(admin):
    # Real old tables in a rollback-only namespace; never drop or replace public.
    namespace = "protocol_" + uid().replace("-", "")
    with admin.transaction(force_rollback=True):
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
        admin.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(namespace)))
        for version in (1, 2, 3):
            migration = next((ROOT / "backend/migrations").glob(f"{version:03}_*.sql"))
            admin.execute(migration.read_text(encoding="utf-8"))
        admin.execute("INSERT INTO users(user_id,display_name) VALUES('U001','旧用户')")
        session, turn, source, work, run = [uid() for _ in range(5)]
        admin.execute("INSERT INTO sessions(session_id,user_id) VALUES(%s,'U001')", (session,))
        admin.execute(
            "INSERT INTO turns(turn_id,session_id,user_id,client_request_id,request_hash,source_seq,text,status,active_run_generation) "
            "VALUES(%s,%s,'U001','legacy','hash',1,'旧协议请求','running',7)",
            (turn, session),
        )
        admin.execute(
            "INSERT INTO sources(source_id,turn_id,user_id,role,kind,scope_kind,content,content_hash,source_seq) "
            "VALUES(%s,%s,'U001','user','message','user_private','旧原文','hash',1)",
            (source, turn),
        )
        admin.execute(
            "INSERT INTO work_items(work_id,user_id,source_id,kind,state,fencing_token,lease_until) "
            "VALUES(%s,'U001',%s,'turn','running',5,now()+interval '1 hour')",
            (work, source),
        )
        admin.execute(
            "INSERT INTO runs(run_id,turn_id,generation,state) VALUES(%s,%s,7,'running')",
            (run, turn),
        )
        admin.execute(
            (ROOT / "backend/migrations/004_trusted_execution.sql").read_text(encoding="utf-8")
        )
        stopped = admin.execute("SELECT * FROM turns WHERE turn_id=%s", (turn,)).fetchone()
        lease = admin.execute("SELECT * FROM work_items WHERE work_id=%s", (work,)).fetchone()
        assert stopped["status"] == "interrupted" and stopped["active_run_generation"] == 8
        assert stopped["error"]["code"] == "PROTOCOL_UPGRADED"
        assert (
            lease["state"] == "cancelled"
            and lease["fencing_token"] == 6
            and lease["lease_until"] is None
        )
        assert (
            admin.execute("SELECT content FROM sources WHERE source_id=%s", (source,)).fetchone()[
                "content"
            ]
            == "旧原文"
        )
        assert (
            admin.execute("SELECT state FROM runs WHERE run_id=%s", (run,)).fetchone()["state"]
            == "interrupted"
        )
