-- Raw messages and execution events are durable; projections remain replaceable.
CREATE TABLE conversation_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions,
    user_id text NOT NULL REFERENCES users,
    turn_id uuid NOT NULL REFERENCES turns,
    source_id uuid REFERENCES sources,
    run_event_seq bigint,
    kind text NOT NULL,
    origin text NOT NULL DEFAULT 'live',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((source_id IS NULL) <> (run_event_seq IS NULL)),
    UNIQUE(source_id),
    UNIQUE(turn_id,run_event_seq)
);
CREATE INDEX conversation_events_session ON conversation_events(session_id,event_id);
CREATE INDEX conversation_events_user_time ON conversation_events(user_id,created_at,event_id);
ALTER TABLE summaries ADD COLUMN projection_version text;
ALTER TABLE summaries ADD COLUMN covered_event_id bigint;

-- Backfill exact existing records, without inventing missing tool calls or times.
INSERT INTO conversation_events(session_id,user_id,turn_id,source_id,kind,origin,created_at)
SELECT t.session_id,s.user_id,t.turn_id,s.source_id,s.role,'legacy_imported',s.created_at
FROM sources s JOIN turns t USING(turn_id)
ORDER BY s.created_at,s.source_seq,s.source_id;
INSERT INTO conversation_events(session_id,user_id,turn_id,run_event_seq,kind,origin,created_at)
SELECT t.session_id,t.user_id,t.turn_id,e.event_seq,e.kind,'legacy_imported',e.created_at
FROM run_events e JOIN turns t USING(turn_id) ORDER BY e.created_at,e.turn_id,e.event_seq;

CREATE FUNCTION record_conversation_source() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
    IF NEW.turn_id IS NOT NULL THEN
        INSERT INTO public.conversation_events(session_id,user_id,turn_id,source_id,kind,created_at)
        SELECT t.session_id,NEW.user_id,NEW.turn_id,NEW.source_id,NEW.role,NEW.created_at
        FROM public.turns t WHERE t.turn_id=NEW.turn_id AND t.user_id=NEW.user_id
        ON CONFLICT(source_id) DO NOTHING;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER conversation_source AFTER INSERT OR UPDATE OF turn_id ON sources
FOR EACH ROW EXECUTE FUNCTION record_conversation_source();

CREATE FUNCTION record_conversation_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
    INSERT INTO public.conversation_events(session_id,user_id,turn_id,run_event_seq,kind,created_at)
    SELECT t.session_id,t.user_id,NEW.turn_id,NEW.event_seq,NEW.kind,NEW.created_at
    FROM public.turns t WHERE t.turn_id=NEW.turn_id;
    RETURN NEW;
END $$;
CREATE TRIGGER conversation_event AFTER INSERT ON run_events
FOR EACH ROW EXECUTE FUNCTION record_conversation_event();

CREATE FUNCTION protect_conversation_content() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR (NEW.content,NEW.content_hash,NEW.role,NEW.user_id,NEW.source_seq)
       IS DISTINCT FROM (OLD.content,OLD.content_hash,OLD.role,OLD.user_id,OLD.source_seq) THEN
        RAISE EXCEPTION 'Conversation content is immutable' USING ERRCODE='42501';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER conversation_content BEFORE UPDATE OR DELETE ON sources
FOR EACH ROW EXECUTE FUNCTION protect_conversation_content();
INSERT INTO schema_migrations(version) VALUES(13);
