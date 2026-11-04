"""Independent chat-history endpoints; knowledge routes remain separate."""

from types import SimpleNamespace
from uuid import UUID

from fastapi import APIRouter

from app import conversation_history
from app.tool_schemas import ReadConversation, SearchConversations


def conversation_router(db, auth_dependency, settings=None, engine=None):
    router = APIRouter(prefix="/api/v1/conversations")

    def observation(auth):
        with db.transaction(readonly=True) as conn:
            seq = conn.execute(
                "SELECT next_source_seq FROM users WHERE user_id=%s", (auth.user_id,)
            ).fetchone()["next_source_seq"]
        return SimpleNamespace(auth=auth, source_seq=seq + 1)

    @router.get("/sessions/{session_id}")
    def session(session_id: UUID, current=auth_dependency):
        from app.identity import owned
        from app.values import safe_json

        with db.transaction(readonly=True) as conn:
            return safe_json(owned(conn, "sessions", "session_id", str(session_id), current))

    @router.post("/search")
    def search(body: SearchConversations, current=auth_dependency):
        return conversation_history.search(
            db,
            observation(current),
            settings=settings,
            engine=engine,
            **body.model_dump(mode="json"),
        )

    @router.post("/read")
    def read(body: ReadConversation, current=auth_dependency):
        return conversation_history.read_page(
            db, observation(current), **body.model_dump(mode="json")
        )

    return router
