import pytest

from pdl_sms_store import store_factory
from pdl_sms_store.memory_store import MemoryStore
from pdl_sms_store.postgres_store import PostgresStore
from pdl_sms_store.store import StoreConfigError

CREDS = {"PGUSER": "reader", "PGPASSWORD": "pw"}


def test_dsn_defaults_and_quoting():
    assert store_factory.dsn_from_env(CREDS) == (
        "postgresql://reader:pw@postgres.lab.svc.cluster.local:5432/lab"
    )
    env = {"PGUSER": "us@er", "PGPASSWORD": "p:w", "PGHOST": "db.test", "PGPORT": "1"}
    assert store_factory.dsn_from_env(env) == "postgresql://us%40er:p%3Aw@db.test:1/lab"


@pytest.mark.parametrize("missing", ["PGUSER", "PGPASSWORD"])
def test_missing_credentials(missing):
    env = {k: v for k, v in CREDS.items() if k != missing}
    with pytest.raises(StoreConfigError, match=missing):
        store_factory.dsn_from_env(env)


def test_memory_and_unknown_kind():
    assert isinstance(store_factory.store_from_env({"SMS_STORE": "memory"}), MemoryStore)
    with pytest.raises(StoreConfigError, match="SMS_STORE"):
        store_factory.store_from_env({"SMS_STORE": "sqlite"})


@pytest.mark.parametrize(
    ("extra", "calls"),
    [({}, 0), ({"SMS_APPLY_MIGRATIONS": "0"}, 0), ({"SMS_APPLY_MIGRATIONS": "1"}, 1)],
)
def test_migrations_only_when_flagged(monkeypatch, extra, calls):
    done = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(store_factory.psycopg, "connect", lambda dsn: FakeConn())
    monkeypatch.setattr(store_factory, "apply_migrations", lambda conn: done.append(conn))
    assert isinstance(store_factory.store_from_env({**CREDS, **extra}), PostgresStore)
    assert len(done) == calls


def test_postgres_from_env_classmethod():
    assert isinstance(PostgresStore.from_env(CREDS), PostgresStore)
