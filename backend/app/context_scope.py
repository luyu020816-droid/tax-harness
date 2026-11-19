"""Agent-scoped summary identity and shared history filtering."""

from app.agent_authority import history_scope_allows
from app.values import digest


def summary_key(conn, execution, binding=None):
    binding = (
        binding
        or conn.execute(
            "SELECT agent_id,agent_version,snapshot FROM agent_turn_bindings WHERE turn_id=%s",
            (execution.turn_id,),
        ).fetchone()
    )
    if not binding:
        return ""
    head = conn.execute(
        "SELECT h.knowledge_epoch FROM sessions s LEFT JOIN company_heads h ON h.company_id=s.selected_company_id WHERE s.session_id=%s",
        (execution.session_id,),
    ).fetchone()
    return digest(
        [
            str(binding["agent_id"]),
            binding["agent_version"],
            binding["snapshot"],
            head["knowledge_epoch"] if head else None,
        ]
    )


def find_summary(conn, execution, scope, epoch, *, lock=False, binding=None):
    return conn.execute(
        "SELECT * FROM summaries WHERE user_id=%s AND session_id=%s AND scope=%s AND memory_epoch=%s AND agent_scope=%s AND status='active' ORDER BY generation DESC LIMIT 1"
        + (" FOR UPDATE" if lock else ""),
        (
            execution.auth.user_id,
            execution.session_id,
            scope,
            epoch,
            summary_key(conn, execution, binding),
        ),
    ).fetchone()


def filter_history(conn, snapshot, groups, excluded=None):
    if not snapshot or snapshot["knowledge_mode"] == "authorized":
        return groups
    permitted = []
    for group in groups:
        previous = conn.execute(
            "SELECT snapshot FROM agent_turn_bindings WHERE turn_id=%s", (group["turn_id"],)
        ).fetchone()
        if history_scope_allows(snapshot, previous["snapshot"] if previous else None):
            permitted.append(group)
        elif excluded is not None:
            excluded.append({"turn_id": group["turn_id"], "reason": "AGENT_KNOWLEDGE_SCOPE"})
    return permitted
