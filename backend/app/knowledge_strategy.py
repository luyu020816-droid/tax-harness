"""Versioned retrieval controls; calibration and resource authorization remain mandatory."""

from pydantic import Field

from app.agent_profiles import _require_manager
from app.errors import AppError
from app.identity import assert_access
from app.tool_schemas import StrictModel
from app.values import jsonb, safe_json


class Strategy(StrictModel):
    enabled: bool = True
    rerank: bool = True
    require_calibrated: bool = False
    evidence_budget: int = Field(default=3000, ge=256, le=6000)
    max_groups: int = Field(default=6, ge=1, le=12)


def read_in(conn, auth, company_id):
    assert_access(conn, auth, company_id)
    row = conn.execute(
        "SELECT s.revision,v.config FROM knowledge_strategies s LEFT JOIN knowledge_strategy_versions v "
        "ON v.company_id=s.company_id AND v.version=s.revision WHERE s.company_id=%s",
        (company_id,),
    ).fetchone()
    if row and row["config"] is None:
        raise AppError("STRATEGY_UNAVAILABLE", "检索策略版本缺失，暂停检索并等待修复。", 503)
    return {
        "company_id": company_id,
        "revision": row["revision"] if row else 0,
        "config": Strategy.model_validate(row["config"]).model_dump()
        if row
        else Strategy().model_dump(),
    }


def publish(db, auth, company_id, expected_revision, config):
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(8123419417)")
        _require_manager(conn, auth, lock=True)
        assert_access(conn, auth, company_id)
        conn.execute("SELECT 1 FROM company_heads WHERE company_id=%s FOR UPDATE", (company_id,))
        current = read_in(conn, auth, company_id)
        if current["revision"] != expected_revision:
            raise AppError("STRATEGY_CONFLICT", "检索策略已变化，请重新载入后发布。", 409)
        version = expected_revision + 1
        conn.execute(
            "INSERT INTO knowledge_strategies(company_id,revision) VALUES(%s,%s) "
            "ON CONFLICT(company_id) DO UPDATE SET revision=excluded.revision",
            (company_id, version),
        )
        conn.execute(
            "INSERT INTO knowledge_strategy_versions(company_id,version,config,actor_id) VALUES(%s,%s,%s,%s)",
            (company_id, version, jsonb(config.model_dump()), auth.user_id),
        )
        conn.execute(
            "UPDATE company_heads SET knowledge_epoch=knowledge_epoch+1 WHERE company_id=%s",
            (company_id,),
        )
        return safe_json(read_in(conn, auth, company_id))
