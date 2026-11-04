"""Project settled dialogue from the log without an arbitrary turn-count cutoff."""

from copy import deepcopy

from app.errors import AppError
from app.identity import owned
from app.sources import read_source


def groups(db, execution, covered=(), *, excluded=None):
    covered = {str(r["source_id"]) for r in covered}
    result = []
    excluded = [] if excluded is None else excluded
    with db.transaction(readonly=True) as conn:
        session = owned(conn, "sessions", "session_id", execution.session_id, execution.auth)
        after = -1
        while True:
            rows = conn.execute(
                "SELECT t.turn_id,t.source_seq,t.text,t.final_message,t.final_response_ref,t.status FROM conversation_events e "
                "JOIN sources s ON s.source_id=e.source_id JOIN turns t ON t.turn_id=e.turn_id "
                "WHERE e.session_id=%s AND e.user_id=%s AND s.role='user' "
                "AND t.source_seq>%s AND t.source_seq<%s AND t.turn_id<>%s "
                "AND t.status NOT IN ('queued','running','deleted') ORDER BY t.source_seq LIMIT 100",
                (
                    execution.session_id,
                    execution.auth.user_id,
                    after,
                    execution.source_seq,
                    execution.turn_id,
                ),
            ).fetchall()
            if not rows:
                break
            after = rows[-1]["source_seq"]
            for row in rows:
                sources = conn.execute(
                    "SELECT * FROM sources WHERE turn_id=%s AND role IN ('user','assistant') ORDER BY created_at,source_id",
                    (row["turn_id"],),
                ).fetchall()
                refs = []
                user_text = None
                assistant_text = None
                try:
                    for source in sources:
                        if (
                            not session["selected_company_id"]
                            and source["scope_kind"] != "user_private"
                        ):
                            raise AppError("SCOPE_MISMATCH", "当前范围不包含此企业记录。", 409)
                        read_source(
                            conn,
                            execution.auth,
                            str(source["source_id"]),
                            company_id=session["selected_company_id"],
                        )
                        if source["role"] == "assistant":
                            from app.conversation_log import read

                            read(conn, execution.auth, str(source["source_id"]))
                        if source["role"] == "assistant" and str(source["source_id"]) != str(
                            row["final_response_ref"]
                        ):
                            continue
                        if source["role"] == "user":
                            user_text = source["content"]
                        else:
                            assistant_text = source["content"]
                        refs.append(
                            {"source_id": str(source["source_id"]), "revision": source["revision"]}
                        )
                except AppError as exc:
                    if exc.code not in (
                        "SOURCE_INVALIDATED",
                        "RESOURCE_NOT_FOUND",
                        "SCOPE_MISMATCH",
                        "ACCESS_DENIED",
                    ):
                        raise
                    excluded.append({"turn_id": str(row["turn_id"]), "reason": exc.code})
                    continue
                if user_text is None or not refs or all(r["source_id"] in covered for r in refs):
                    continue
                messages = [{"role": "user", "content": user_text}]
                # Historical tools are historical evidence, never replayed side effects.
                events = conn.execute(
                    "SELECT e.payload,e.run_id FROM run_events e JOIN runs r USING(run_id) "
                    "WHERE e.turn_id=%s AND e.kind='model_finished' AND r.final_source_id IS NOT NULL ORDER BY e.event_seq",
                    (row["turn_id"],),
                ).fetchall()
                for event in events:
                    message = deepcopy(event["payload"].get("message") or {})
                    calls = message.get("tool_calls") or []
                    if not calls:
                        continue
                    outputs, tool_refs = [], []
                    for call in calls:
                        found = conn.execute(
                            "SELECT result_source_id FROM tool_executions WHERE run_id=%s AND tool_call_id=%s AND state IN ('done','failed')",
                            (event["run_id"], call["id"]),
                        ).fetchone()
                        if not found or not found["result_source_id"]:
                            break
                        try:
                            source = read_source(
                                conn,
                                execution.auth,
                                str(found["result_source_id"]),
                                company_id=session["selected_company_id"],
                            )
                        except AppError as exc:
                            if exc.code not in (
                                "SOURCE_INVALIDATED",
                                "RESOURCE_NOT_FOUND",
                                "SCOPE_MISMATCH",
                                "ACCESS_DENIED",
                            ):
                                raise
                            excluded.append(
                                {
                                    "turn_id": str(row["turn_id"]),
                                    "reason": exc.code,
                                    "kind": "tool_body",
                                }
                            )
                            break
                        # Knowledge/history bodies must be re-read with current dependencies.
                        import json

                        data = json.loads(source["content"]).get("data") or {}
                        if data.get("kind") in (
                            "knowledge_search",
                            "knowledge_evidence",
                            "history_search",
                            "history_evidence",
                            "conversation_search",
                            "conversation_evidence",
                        ):
                            break
                        outputs.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": source["content"],
                            }
                        )
                        tool_refs.append(
                            {"source_id": str(source["source_id"]), "revision": source["revision"]}
                        )
                    if len(outputs) == len(calls):
                        # Provider call IDs need only be unique within their original run.
                        for call, output in zip(calls, outputs, strict=True):
                            qualified = f"history_{event['run_id']}_{call['id']}"
                            call["id"] = qualified
                            output["tool_call_id"] = qualified
                        messages.extend([message, *outputs])
                        refs.extend(tool_refs)
                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_text
                        if assistant_text is not None
                        else f"（该轮状态为{row['status']}，没有可核验的最终助手回复；不得据此推断操作成功。）",
                    }
                )
                result.append(
                    {
                        "turn_id": str(row["turn_id"]),
                        "source_seq": row["source_seq"],
                        "sources": refs,
                        "messages": messages,
                    }
                )
    return result
