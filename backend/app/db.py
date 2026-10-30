from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = ConnectionPool(
            dsn,
            min_size=0,
            max_size=10,
            timeout=5,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": True, "connect_timeout": 3},
        )

    def open(self) -> None:
        self.pool.open()

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def transaction(self, *, readonly: bool = False):
        with self.pool.connection() as conn, conn.transaction():
            if readonly:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield conn

    def ready(self) -> bool:
        with self.transaction(readonly=True) as conn:
            versions = {
                row["version"]
                for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            return {5, 7} <= versions
