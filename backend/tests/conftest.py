import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.cli import initialize
from app.config import Settings
from app.db import Database
from app.identity import Identity, create_session
from app.sources import add_source, next_sequence
from app.values import uid


@pytest.fixture(scope="session")
def settings():
    result = Settings()
    if not result.test_database_url or "tax_harness_test" not in result.test_database_url:
        pytest.fail("Configure the isolated tax_harness_test database before running tests.")
    initialize(result, test=True)
    return result


@pytest.fixture
def admin(settings):
    with psycopg.connect(
        make_conninfo(settings.admin_database_url, dbname="tax_harness_test"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        conn.execute("TRUNCATE users,companies,import_batches,rule_versions CASCADE")
        yield conn


@pytest.fixture
def db(settings, admin):
    initialize(settings, test=True)
    instance = Database(settings.test_database_url)
    instance.open()
    yield instance
    instance.close()


@pytest.fixture
def source_factory(db):
    def create(text, user="U001", company="C001"):
        auth = Identity(user)
        with db.transaction() as conn:
            sequence = next_sequence(conn, auth)
            return add_source(
                conn, auth, text, source_seq=sequence, companies=[company] if company else []
            )

    return create


@pytest.fixture
def execution_factory(db, source_factory):
    def create(text, user="U001", company="C001"):
        from app.execution import Execution

        auth = Identity(user)
        session = create_session(db, auth, company)
        source = source_factory(text, user, company)
        turn_id, work_id, run_id = uid(), uid(), uid()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO turns(turn_id,session_id,user_id,client_request_id,request_hash,source_seq,text,status,active_run_generation) VALUES(%s,%s,%s,%s,'test',%s,%s,'running',1)",
                (turn_id, session["session_id"], user, uid(), source["source_seq"], text),
            )
            conn.execute(
                "UPDATE sources SET turn_id=%s WHERE source_id=%s", (turn_id, source["source_id"])
            )
            conn.execute(
                "INSERT INTO work_items(work_id,user_id,source_id,kind,state,lease_until,fencing_token) VALUES(%s,%s,%s,'turn','running',now()+interval '90 seconds',1)",
                (work_id, user, source["source_id"]),
            )
            conn.execute(
                "INSERT INTO runs(run_id,turn_id,generation,state) VALUES(%s,%s,1,'running')",
                (run_id, turn_id),
            )
            epoch = conn.execute(
                "SELECT memory_epoch FROM users WHERE user_id=%s", (user,)
            ).fetchone()["memory_epoch"]
        return Execution(
            auth,
            turn_id,
            str(session["session_id"]),
            str(source["source_id"]),
            source["source_seq"],
            work_id,
            run_id,
            1,
            1,
            epoch,
        )

    return create
