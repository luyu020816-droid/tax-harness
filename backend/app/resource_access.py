"""Current authorization for turn/session projections, including input dependencies."""

TURN_SCOPES = """
SELECT z.company_id FROM sources x JOIN source_scopes z USING(source_id) WHERE x.turn_id=t.turn_id
UNION
SELECT z.company_id FROM runs rr JOIN context_snapshots cc USING(run_id)
CROSS JOIN LATERAL jsonb_array_elements(coalesce(cc.manifest->'sources','[]')) ref
JOIN source_scopes z ON z.source_id=(ref->>'source_id')::uuid WHERE rr.turn_id=t.turn_id
UNION
SELECT c.company_id FROM runs rr JOIN context_snapshots cc USING(run_id)
CROSS JOIN companies c WHERE rr.turn_id=t.turn_id AND
    (coalesce(cc.manifest->'heads','{}') ? c.company_id OR cc.manifest->>'company_id'=c.company_id)
"""
TURN_VISIBLE = f"""t.user_id=%(user)s AND NOT EXISTS(
    SELECT 1 FROM ({TURN_SCOPES}) scopes WHERE NOT EXISTS(
        SELECT 1 FROM user_company_grants g WHERE g.user_id=%(user)s
        AND g.company_id=scopes.company_id AND g.can_read))"""
SESSION_VISIBLE = f"""s.user_id=%(user)s AND s.deleted_at IS NULL
AND (s.selected_company_id IS NULL OR EXISTS(SELECT 1 FROM user_company_grants g
    WHERE g.user_id=%(user)s AND g.company_id=s.selected_company_id AND g.can_read))
AND NOT EXISTS(SELECT 1 FROM turns t WHERE t.session_id=s.session_id AND NOT ({TURN_VISIBLE}))"""
SESSION_TITLE = """(SELECT CASE WHEN EXISTS(SELECT 1 FROM sources x WHERE x.turn_id=t.turn_id
    AND x.user_id=t.user_id AND x.role='user' AND x.recall_state='active' AND x.content=t.text)
    THEN left(t.text,120) ELSE '已保护的会话' END FROM turns t
    WHERE t.session_id=s.session_id AND t.request_kind<>'compact' ORDER BY t.source_seq LIMIT 1)"""


def session_allowed(conn, user_id, session_id):
    return bool(
        conn.execute(
            "SELECT 1 FROM sessions s WHERE s.session_id=%(session)s AND " + SESSION_VISIBLE,
            {"user": user_id, "session": session_id},
        ).fetchone()
    )


def turn_allowed(conn, user_id, turn_id):
    return bool(
        conn.execute(
            "SELECT 1 FROM turns t WHERE t.turn_id=%(turn)s AND " + TURN_VISIBLE,
            {"user": user_id, "turn": turn_id},
        ).fetchone()
    )
