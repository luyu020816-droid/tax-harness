"""Rebuild a derived text index only from current, authorised source heads."""

import psycopg
from app.config import Settings
from app.values import digest
from psycopg.rows import dict_row


def main(*, semantic=False):
    settings = Settings()
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        rows = conn.execute("SELECT * FROM sources WHERE recall_state='active'").fetchall()
        if any(digest(row["content"]) != row["content_hash"] for row in rows):
            raise SystemExit("Source checksum failed. Index was not changed.")
        conn.execute("DELETE FROM history_index")
        conn.execute(
            "INSERT INTO history_index(source_id,source_revision,scope,searchable_text) SELECT s.source_id,s.revision,coalesce((SELECT string_agg(company_id,',' ORDER BY company_id) FROM source_scopes x WHERE x.source_id=s.source_id),'@user'),s.content FROM sources s WHERE s.recall_state='active'"
        )
    print(f"Rebuilt {len(rows)} active source entries; suppressed/deleted sources excluded.")
    if semantic:
        from app.db import Database
        from app.history_jobs import HistoryJobs
        from app.retrieval_engine import LocalRetrieval

        db = Database(settings.database_url)
        db.open()
        try:
            result = HistoryJobs(
                db, settings, LocalRetrieval(settings.knowledge_model_directory)
            ).backfill()
            print(
                f"Queued {result['queued']} semantic source jobs; worker publishes verified passages."
            )
        finally:
            db.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--semantic", action="store_true")
    main(semantic=parser.parse_args().semantic)
