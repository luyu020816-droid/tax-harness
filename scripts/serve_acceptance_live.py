"""Real-model browser acceptance server, restricted to the isolated test database."""

import argparse

import psycopg
import uvicorn
from app.api import create_app
from app.cli import initialize
from app.config import Settings
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=8131, type=int)
    parser.add_argument("--reset-test-data", action="store_true")
    parser.add_argument(
        "--inject-post-save-summary-error",
        action="store_true",
        help="Fault injection immediately after the real update tool has committed",
    )
    args = parser.parse_args()
    settings = Settings()
    if not settings.model_configured:
        raise SystemExit("Real model is not configured; no data changed.")
    if conninfo_to_dict(settings.test_database_url).get("dbname") != "tax_harness_test":
        raise SystemExit("Only tax_harness_test is permitted.")
    if args.reset_test_data:
        with psycopg.connect(
            make_conninfo(settings.admin_database_url, dbname="tax_harness_test"),
            autocommit=True,
        ) as conn:
            active = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
            ).fetchone()[0]
            if active:
                raise SystemExit("Other test connections are active; nothing reset.")
            conn.execute(
                "TRUNCATE users,companies,import_batches,rule_versions CASCADE"
            )
    initialize(settings, test=True)
    actual = settings.model_copy(
        update={"database_url": settings.test_database_url, "ingest_database_url": ""}
    )
    print(
        "REAL MODEL browser acceptance; database=tax_harness_test; production database untouched.",
        flush=True,
    )
    app = create_app(actual)
    if args.inject_post_save_summary_error:
        from app.errors import AppError

        execute = app.state.runtime.tools.execute

        def fail_after_real_save(execution, call):
            result = execute(execution, call)
            if call["function"]["name"] == "update_invoice" and result[0].get("ok"):
                print(
                    "FAULT INJECTION: real save committed; injecting post-save SUMMARY_INVALID.",
                    flush=True,
                )
                raise AppError(
                    "SUMMARY_INVALID",
                    "验收故障注入：保存后摘要校验失败，原文保留。",
                    422,
                )
            return result

        app.state.runtime.tools.execute = fail_after_real_save
        print(
            "FAULT INJECTION ENABLED: post-save summary error; business model and save are real.",
            flush=True,
        )
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
