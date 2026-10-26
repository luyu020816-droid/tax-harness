"""Rebuild a derived text index only from current, authorised source heads."""

import psycopg
from app.config import Settings
from app.values import digest
from psycopg.rows import dict_row


def main():
    settings = Settings()
    with psycopg.connect(settings.admin_database_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT * FROM sources WHERE recall_state='active'"
        ).fetchall()
        if any(digest(row["content"]) != row["content_hash"] for row in rows):
            raise SystemExit("Source checksum failed. Index was not changed.")
        conn.execute("DELETE FROM history_index")
        conn.execute(
            "INSERT INTO history_index(source_id,source_revision,scope,searchable_text) SELECT s.source_id,s.revision,coalesce((SELECT string_agg(company_id,',' ORDER BY company_id) FROM source_scopes x WHERE x.source_id=s.source_id),'@user'),s.content FROM sources s WHERE s.recall_state='active'"
        )
    print(
        f"Rebuilt {len(rows)} active source entries; suppressed/deleted sources excluded."
    )


if __name__ == "__main__":
    main()
