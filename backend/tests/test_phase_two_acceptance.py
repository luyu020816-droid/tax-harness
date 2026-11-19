"""Independent negative acceptance cases spanning knowledge and compaction."""

import pytest
from test_conversation_context import settled
from test_knowledge import knowledge as knowledge
from test_knowledge import uploaded
from test_runtime import model_settings

from app import context
from app.errors import AppError
from app.identity import Identity
from app.knowledge_admin import disable_company, publish_company
from app.summary_evidence import pack
from app.values import digest, jsonb, uid


@pytest.mark.parametrize("path", ["reassemble", "already_assembled"])
def test_legacy_summary_must_not_reintroduce_disabled_company_knowledge(
    db, admin, settings, execution_factory, knowledge, path
):
    created = uploaded(knowledge, text="企业内部制度：报销标准为七百元。")
    current = knowledge[0].get(Identity("U001"), created["document_id"])
    with admin.transaction():
        doc = publish_company(
            admin, created["document_id"], created["version_id"], current["revision"], uid()
        )
    found = knowledge[2].search(Identity("U001"), company_id="C001", query="报销标准")
    assert found["evidence"]
    execution = execution_factory("继续解释这份制度")
    marker = "INVALID-COMPANY-POLICY-700"
    user_source, assistant_source = settled(db, execution.session_id, "查询报销标准", marker)
    with db.transaction() as conn:
        old = conn.execute(
            "SELECT turn_id,revision FROM sources WHERE source_id=%s", (assistant_source,)
        ).fetchone()
        run_id = uid()
        conn.execute(
            "INSERT INTO runs(run_id,turn_id,generation,state,final_source_id) VALUES(%s,%s,1,'completed',%s)",
            (run_id, old["turn_id"], assistant_source),
        )
        conn.execute(
            "INSERT INTO context_snapshots(context_id,run_id,model_step,payload,payload_hash,manifest,token_count,count_kind) VALUES(%s,%s,1,%s,%s,%s,1,'test')",
            (
                uid(),
                run_id,
                jsonb({"model": "test"}),
                digest({"model": "test"}),
                jsonb({"retrieval_refs": [found["query_id"]]}),
            ),
        )
        conn.execute(
            "UPDATE turns SET final_cards=%s WHERE turn_id=%s",
            (
                jsonb(
                    [
                        {
                            "kind": "knowledge_quote",
                            "reference_label": "制度依据",
                            "citation_id": found["evidence"][0]["citation_id"],
                        }
                    ]
                ),
                old["turn_id"],
            ),
        )
        seq = conn.execute(
            "UPDATE users SET next_source_seq=next_source_seq+1 WHERE user_id='U001' RETURNING next_source_seq"
        ).fetchone()["next_source_seq"]
        conn.execute("UPDATE turns SET source_seq=%s WHERE turn_id=%s", (seq, execution.turn_id))
        execution.source_seq = seq
        refs = [
            {"source_id": user_source, "revision": 1},
            {"source_id": assistant_source, "revision": old["revision"]},
        ]
        conn.execute(
            "INSERT INTO summaries(summary_id,user_id,session_id,scope,covered_sources,memory_epoch,content,status,generation) VALUES(%s,'U001',%s,'C001',%s,%s,%s,'active',1)",
            (uid(), execution.session_id, jsonb(refs), execution.expected_epoch, pack(marker, [])),
        )
    before, manifest, _ = context.assemble(db, model_settings(settings), execution, [])
    assert marker in str(before)
    with admin.transaction():
        disable_company(admin, str(doc["document_id"]), doc["revision"], uid())
    if path == "already_assembled":
        with pytest.raises(AppError) as caught:
            context.validate(db, execution, before, manifest)
        assert caught.value.code in {"STALE_CONTEXT", "SOURCE_INVALIDATED", "SUMMARY_INVALID"}
        return
    try:
        after, _, _ = context.assemble(db, model_settings(settings), execution, [])
    except AppError as exc:
        assert exc.code in {"STALE_CONTEXT", "SOURCE_INVALIDATED", "SUMMARY_INVALID"}
        return
    assert marker not in str(after), "Disabled knowledge was reintroduced through the old summary"
