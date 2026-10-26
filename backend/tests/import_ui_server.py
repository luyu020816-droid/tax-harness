"""Offline browser test server; never points at the live extension database."""

import psycopg
import uvicorn
from psycopg.conninfo import make_conninfo
from test_invoice_imports import invoice, workbook

from app.api import create_app
from app.cli import initialize
from app.config import ROOT, Settings
from app.errors import AppError


class OfflineModel:
    mode = "test"

    async def complete(self, payload):
        raise AppError("MODEL_UNAVAILABLE", "浏览器导入验收不调用模型。", 503)


if __name__ == "__main__":
    config = Settings(_env_file=ROOT / ".env.extended")
    config = config.model_copy(
        update={
            "database_url": config.test_database_url,
            "ingest_database_url": make_conninfo(
                config.ingest_database_url, dbname="tax_harness_extended_test"
            ),
            "model_api_key": "",
            "model_id": "",
            "model_base_url": "",
            "model_context_limit": 0,
        }
    )
    with psycopg.connect(
        make_conninfo(config.admin_database_url, dbname="tax_harness_extended_test"),
        autocommit=True,
    ) as conn:
        if conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
        ).fetchone()[0]:
            raise SystemExit("Stop extension tests before starting the browser test server.")
        conn.execute("TRUNCATE users,companies,import_batches,rule_versions CASCADE")
    initialize(config, test=True, extended=True)
    inputs = ROOT / ".local/test-inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "conflicts.xlsx").write_bytes(
        workbook(
            [
                invoice("F002", "99999999.99"),
                invoice("F009999", "10.00"),
                invoice("F009998", "invalid"),
            ]
        )
    )
    uvicorn.run(create_app(config, model=OfflineModel()), host="127.0.0.1", port=8103)
