from app.errors import AppError
from app.events import emit
from app.execution import Execution
from app.identity import Identity, assert_access
from app.values import jsonb, safe_json, uid


def record_context(conn, execution: Execution, step: int, payload: dict, manifest: dict) -> str:
    context_id = uid()
    conn.execute(
        "INSERT INTO context_snapshots(context_id,run_id,model_step,payload,payload_hash,manifest,token_count,count_kind) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            context_id,
            execution.run_id,
            step,
            jsonb(payload),
            manifest["payload_hash"],
            jsonb(manifest),
            manifest["token_count"],
            manifest["count_kind"],
        ),
    )
    emit(
        conn,
        execution.turn_id,
        execution.run_id,
        "control_started" if manifest.get("kind") == "control_execution" else "model_started",
        {"step": step, "context_id": context_id},
        input_ref=context_id,
    )
    return context_id


def read_run(db, auth: Identity, run_id: str) -> dict:
    with db.transaction(readonly=True) as conn:
        run = conn.execute(
            "SELECT r.* FROM runs r JOIN turns t USING(turn_id) WHERE run_id=%s AND t.user_id=%s",
            (run_id, auth.user_id),
        ).fetchone()
        if not run:
            raise AppError("RESOURCE_NOT_FOUND", "运行记录不存在或不可访问。", 404)
        companies = conn.execute(
            "SELECT DISTINCT x.company_id FROM sources s JOIN source_scopes x USING(source_id) WHERE s.turn_id=%s",
            (run["turn_id"],),
        ).fetchall()
        for company in companies:
            assert_access(conn, auth, company["company_id"])
        contexts = conn.execute(
            "SELECT * FROM context_snapshots WHERE run_id=%s ORDER BY model_step", (run_id,)
        ).fetchall()
        tools = conn.execute(
            "SELECT e.*,s.content AS actual_result FROM tool_executions e LEFT JOIN sources s ON s.source_id=e.result_source_id WHERE e.run_id=%s ORDER BY e.tool_call_id",
            (run_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM run_events WHERE run_id=%s ORDER BY event_seq", (run_id,)
        ).fetchall()
        return safe_json({"run": run, "contexts": contexts, "tools": tools, "events": events})


def list_runs(db, auth: Identity, company_id=None) -> list[dict]:
    with db.transaction(readonly=True) as conn:
        if company_id:
            assert_access(conn, auth, company_id)
        return safe_json(
            conn.execute(
                "SELECT r.*,t.text,t.session_id FROM runs r JOIN turns t USING(turn_id) WHERE t.user_id=%s "
                "AND (%s::text IS NULL OR EXISTS(SELECT 1 FROM sources s JOIN source_scopes z USING(source_id) WHERE s.turn_id=t.turn_id AND z.company_id=%s)) "
                "ORDER BY started_at DESC LIMIT 100",
                (auth.user_id, company_id, company_id),
            ).fetchall()
        )
