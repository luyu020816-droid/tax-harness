CREATE TABLE knowledge_publications (
    document_id uuid NOT NULL,
    publication_revision bigint NOT NULL CHECK (publication_revision > 0),
    version_id uuid NOT NULL,
    scope text NOT NULL CHECK (scope IN ('user_private', 'company')),
    origin text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (document_id, publication_revision),
    FOREIGN KEY (document_id, version_id) REFERENCES knowledge_versions(document_id, version_id) ON DELETE CASCADE
);

-- These signals existed only after publication or an actually delivered citation.
-- Ready candidates alone provide no publication evidence.
WITH signals AS (
    SELECT d.document_id, d.revision, d.active_version_id AS version_id, d.scope, 'legacy_pointer' AS origin, 1 AS authority
    FROM knowledge_documents d WHERE d.active_version_id IS NOT NULL
    UNION ALL
    SELECT v.document_id, v.expected_document_revision + 1, v.version_id, 'user_private', 'legacy_job', 3
    FROM retrieval_jobs j JOIN knowledge_versions v USING (version_id)
    WHERE j.state='done' AND j.progress->'published'='true'::jsonb
    UNION ALL
    SELECT e.document_id, e.document_revision, e.version_id,
        CASE WHEN q.user_id<>d.owner_id THEN 'company' ELSE 'user_private' END, 'legacy_citation', 4
    FROM retrieval_evidence e JOIN retrieval_queries q USING (query_id) JOIN knowledge_documents d USING (document_id)
    UNION ALL
    SELECT v.document_id, CASE WHEN a.info->>'revision' ~ '^[0-9]{1,18}$'
        THEN (a.info->>'revision')::bigint END, v.version_id, a.scope, 'legacy_maintenance', 2
    FROM (
        SELECT result AS info, 'company' AS scope FROM knowledge_maintenance_ops WHERE action='publish_company'
        UNION ALL
        SELECT result_ref, 'user_private' FROM memory_change_ops WHERE target_type='knowledge' AND action='publish' AND state='committed'
    ) a
    JOIN knowledge_versions v ON v.version_id::text=a.info->>'active_version_id'
        AND v.document_id::text=a.info->>'document_id'
)
INSERT INTO knowledge_publications(document_id, publication_revision, version_id, scope, origin)
SELECT DISTINCT ON (document_id, revision) document_id, revision, version_id, scope, origin
FROM signals WHERE revision > 0 ORDER BY document_id, revision, authority, version_id;

CREATE FUNCTION knowledge_version_at(doc uuid, starts date, until date, reader text)
RETURNS TABLE (version_id uuid, publication_revision bigint, full_coverage boolean)
LANGUAGE sql STABLE AS $$
    SELECT v.version_id, p.publication_revision,
           v.effective_from <= starts AND (until IS NULL OR v.effective_to IS NULL OR v.effective_to >= until)
    FROM knowledge_publications p
    JOIN knowledge_versions v USING (document_id, version_id)
    JOIN knowledge_documents d USING (document_id)
    WHERE p.document_id=doc AND d.status='active' AND v.state='ready'
      AND (d.scope='company' OR d.owner_id=reader)
      AND (p.scope=d.scope OR (p.scope='user_private' AND d.owner_id=reader))
      AND (v.effective_to IS NULL OR v.effective_to > starts)
      AND (CASE WHEN until IS NULL THEN v.effective_from <= starts ELSE v.effective_from < until END)
    -- For an interval, the highest-ranked version that overlaps any part wins
    -- at least somewhere. Require it to cover the whole interval before citing.
    ORDER BY p.publication_revision DESC
    LIMIT 1
$$;

INSERT INTO schema_migrations(version) VALUES(29);
