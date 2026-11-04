CREATE TABLE conversation_index_profiles (
    profile_id text PRIMARY KEY,
    inference_profile_id text NOT NULL,
    manifest jsonb NOT NULL,
    dimension integer NOT NULL CHECK (dimension=1024),
    enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX conversation_one_enabled_profile ON conversation_index_profiles(enabled) WHERE enabled;
CREATE TABLE conversation_index_jobs (
    source_id uuid NOT NULL REFERENCES sources,
    profile_id text NOT NULL REFERENCES conversation_index_profiles,
    source_revision bigint NOT NULL,
    source_hash text NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','ready','failed','cancelled')),
    target_generation_id uuid,
    published_generation_id uuid,
    expected_chunk_count integer,
    attempts integer NOT NULL DEFAULT 0,
    next_retry_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    fencing_token bigint NOT NULL DEFAULT 0,
    last_error jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(source_id,profile_id)
);
CREATE INDEX conversation_jobs_claim ON conversation_index_jobs(state,next_retry_at,lease_until);
CREATE TABLE conversation_passages (
    passage_id uuid PRIMARY KEY,
    source_id uuid NOT NULL REFERENCES sources,
    source_revision bigint NOT NULL,
    source_hash text NOT NULL,
    profile_id text NOT NULL REFERENCES conversation_index_profiles,
    generation_id uuid NOT NULL,
    ordinal integer NOT NULL,
    char_start integer NOT NULL CHECK(char_start>=0),
    char_end integer NOT NULL CHECK(char_end>char_start),
    excerpt_hash text NOT NULL,
    embedding_input_hash text NOT NULL,
    embedding vector(1024) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_id,profile_id,generation_id,ordinal)
);
CREATE INDEX conversation_passages_source ON conversation_passages(source_id,profile_id,generation_id);

CREATE FUNCTION enqueue_conversation_index(target_turn uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
    INSERT INTO public.conversation_index_jobs(source_id,profile_id,source_revision,source_hash)
    SELECT x.source_id,p.profile_id,x.revision,x.content_hash
    FROM public.sources x JOIN public.turns t USING(turn_id)
    CROSS JOIN public.conversation_index_profiles p
    WHERE t.turn_id=target_turn AND p.enabled AND x.recall_state='active'
      AND t.status IN ('completed','waiting_user','resolved','failed','cancelled','interrupted')
      AND (x.role='user' OR (x.role='assistant' AND x.source_id=t.final_response_ref))
    ON CONFLICT(source_id,profile_id) DO NOTHING;
$$;
REVOKE ALL ON FUNCTION enqueue_conversation_index(uuid) FROM PUBLIC;
CREATE FUNCTION conversation_index_turn_trigger() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM public.conversation_index_profiles WHERE enabled) THEN
        PERFORM public.enqueue_conversation_index(NEW.turn_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER conversation_index_on_turn AFTER INSERT OR UPDATE OF status,final_response_ref ON turns
FOR EACH ROW EXECUTE FUNCTION conversation_index_turn_trigger();
CREATE TRIGGER conversation_index_on_source AFTER INSERT OR UPDATE OF turn_id ON sources
FOR EACH ROW EXECUTE FUNCTION conversation_index_turn_trigger();

CREATE FUNCTION lock_conversation_index_grants(target_source uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
    PERFORM 1 FROM public.user_company_grants WHERE user_id=(
        SELECT user_id FROM public.sources WHERE source_id=target_source
    ) FOR SHARE;
END $$;
REVOKE ALL ON FUNCTION lock_conversation_index_grants(uuid) FROM PUBLIC;

INSERT INTO schema_migrations(version) VALUES(15);
