from app.values import jsonb


def emit(
    conn,
    turn_id: str,
    run_id: str | None,
    kind: str,
    payload: dict,
    *,
    tool_call_id=None,
    input_ref=None,
    result_ref=None,
) -> int:
    sequence = conn.execute(
        "UPDATE turns SET next_event_seq=next_event_seq+1,updated_at=now() WHERE turn_id=%s RETURNING next_event_seq",
        (turn_id,),
    ).fetchone()["next_event_seq"]
    conn.execute(
        "INSERT INTO run_events(turn_id,event_seq,run_id,kind,payload,tool_call_id,input_ref,result_ref) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (turn_id, sequence, run_id, kind, jsonb(payload), tool_call_id, input_ref, result_ref),
    )
    return sequence


def interrupt_session(conn, user_id: str, session_id: str) -> None:
    rows = conn.execute(
        "UPDATE turns SET status='interrupted',active_run_generation=active_run_generation+1,error=%s "
        "WHERE user_id=%s AND session_id=%s AND status IN ('queued','running') RETURNING turn_id",
        (
            jsonb(
                {
                    "code": "SCOPE_CHANGED",
                    "message": "企业已切换，本轮处理已停止；已提交结果可在办理记录核对。",
                }
            ),
            user_id,
            session_id,
        ),
    ).fetchall()
    for row in rows:
        turn_id = row["turn_id"]
        conn.execute(
            "UPDATE work_items w SET state='cancelled',fencing_token=fencing_token+1,lease_until=NULL FROM sources s WHERE w.source_id=s.source_id AND s.turn_id=%s",
            (turn_id,),
        )
        conn.execute(
            "UPDATE runs SET state='interrupted',finished_at=now() WHERE turn_id=%s AND finished_at IS NULL",
            (turn_id,),
        )
        conn.execute(
            "UPDATE source_assessments a SET state='invalidated' FROM sources s WHERE a.source_id=s.source_id AND s.turn_id=%s AND a.state='pending'",
            (turn_id,),
        )
        emit(
            conn,
            str(turn_id),
            None,
            "cancelled",
            {"status": "interrupted", "reason": "scope_changed"},
        )
