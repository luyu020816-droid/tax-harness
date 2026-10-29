from app.errors import AppError
from app.identity import Identity, assert_access
from app.values import canonical, digest, jsonb, uid


def next_sequence(conn, auth: Identity) -> int:
    return conn.execute(
        "UPDATE users SET next_source_seq=next_source_seq+1 WHERE user_id=%s RETURNING next_source_seq",
        (auth.user_id,),
    ).fetchone()["next_source_seq"]


def add_source(
    conn,
    auth: Identity,
    content,
    *,
    role="user",
    kind="message",
    turn_id=None,
    source_seq: int,
    companies: list[str] | None = None,
    source_id: str | None = None,
) -> dict:
    text = content if isinstance(content, str) else canonical(content)
    companies = list(dict.fromkeys(companies or []))
    for company in companies:
        assert_access(conn, auth, company)
    row = conn.execute(
        "INSERT INTO sources(source_id,turn_id,user_id,role,kind,scope_kind,content,content_hash,source_seq) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (
            source_id or uid(),
            turn_id,
            auth.user_id,
            role,
            kind,
            "company_scoped" if companies else "user_private",
            text,
            digest(text),
            source_seq,
        ),
    ).fetchone()
    for company in companies:
        conn.execute(
            "INSERT INTO source_scopes(source_id,company_id) VALUES(%s,%s)",
            (row["source_id"], company),
        )
    conn.execute(
        "INSERT INTO source_assessments(source_id,revision,purpose,processor_version,state) "
        "VALUES(%s,1,'memory','1','pending')",
        (row["source_id"],),
    )
    conn.execute(
        "INSERT INTO history_index(source_id,source_revision,scope,searchable_text) VALUES(%s,1,%s,%s)",
        (row["source_id"], ",".join(companies) or "@user", text),
    )
    return row


def read_source(
    conn, auth: Identity, source_id: str, *, company_id=None, allow_suppressed=False
) -> dict:
    row = conn.execute(
        "SELECT * FROM sources WHERE source_id=%s AND user_id=%s AND recall_state<>'deleted'",
        (source_id, auth.user_id),
    ).fetchone()
    if not row:
        raise AppError("SOURCE_INVALIDATED", "来源已失效或不可访问。", 409)
    if digest(row["content"]) != row["content_hash"]:
        raise AppError("STATE_SOURCE_UNAVAILABLE", "来源内容校验失败，不能继续使用。", 503)
    if row["recall_state"] != "active" and not allow_suppressed:
        raise AppError("SOURCE_INVALIDATED", "来源含已失效记忆，不能作为普通历史重新使用。", 409)
    scopes = conn.execute(
        "SELECT company_id FROM source_scopes WHERE source_id=%s", (source_id,)
    ).fetchall()
    for scope in scopes:
        assert_access(conn, auth, scope["company_id"])
    if company_id and scopes and {company_id} != {x["company_id"] for x in scopes}:
        raise AppError("SOURCE_INVALIDATED", "来源不属于当前企业。", 409)
    return row


def validate_refs(conn, auth: Identity, refs: list[dict], *, company_id=None) -> list[dict]:
    if not refs:
        raise AppError("NEEDS_CLARIFICATION", "缺少这项操作的用户原话依据。", 422)
    rows = []
    for ref in refs:
        row = read_source(conn, auth, str(ref["source_id"]), company_id=company_id)
        if (
            row["role"] != "user"
            or ref.get("quote", "") not in row["content"]
            or not ref.get("quote")
        ):
            raise AppError("NEEDS_CLARIFICATION", "引用必须匹配用户的明确原话。", 422)
        rows.append(row)
    return rows


def assess(conn, source_id: str, state: str, refs=None) -> None:
    conn.execute(
        "UPDATE source_assessments SET state=CASE WHEN state='accepted' AND %s='no_change' THEN state ELSE %s END,"
        "result_refs=CASE WHEN state='accepted' AND %s='no_change' THEN result_refs ELSE %s END "
        "WHERE source_id=%s AND purpose='memory'",
        (state, state, state, jsonb(refs or []), source_id),
    )


def advance_progress(conn, auth: Identity) -> None:
    inputs = conn.execute(
        "SELECT s.source_seq,bool_and(a.state IN ('accepted','no_change','invalidated')) AS done "
        "FROM sources s JOIN source_assessments a USING(source_id) "
        "WHERE s.user_id=%s AND s.role='user' GROUP BY s.source_seq ORDER BY s.source_seq",
        (auth.user_id,),
    ).fetchall()
    cursor = 0
    gaps = []
    for row in inputs:
        if row["source_seq"] == cursor + 1 and row["done"]:
            cursor += 1
        elif not row["done"]:
            gaps.append(row["source_seq"])
    conn.execute(
        "INSERT INTO source_progress(user_id,purpose,contiguous_seq,unresolved_refs) VALUES(%s,'memory',%s,%s) "
        "ON CONFLICT(user_id,purpose) DO UPDATE SET contiguous_seq=excluded.contiguous_seq,unresolved_refs=excluded.unresolved_refs",
        (auth.user_id, cursor, jsonb(gaps)),
    )
