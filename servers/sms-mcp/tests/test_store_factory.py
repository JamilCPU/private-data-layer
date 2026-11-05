import pytest

from pdl_sms import store_factory
from pdl_sms.memory_store import MemoryStore
from pdl_sms.postgres_store import PostgresStore
from pdl_sms.store import StoreConfigError

CREDS = {"PGUSER": "reader", "PGPASSWORD": "pw"}


def test_dsn_defaults():
    assert store_factory.dsn_from_env(CREDS) == (
        "postgresql://reader:pw@postgres.lab.svc.cluster.local:5432/lab"
    )


def test_dsn_overrides_and_quoting():
    env = {
        "PGUSER": "us@er",
        "PGPASSWORD": "p:w/ d%@",
        "PGHOST": "db.test",
        "PGPORT": "6543",
        "PGDATABASE": "my db",
    }
    assert store_factory.dsn_from_env(env) == (
        "postgresql://us%40er:p%3Aw%2F%20d%25%40@db.test:6543/my%20db"
    )


@pytest.mark.parametrize("missing", ["PGUSER", "PGPASSWORD"])
def test_dsn_missing_credentials_names_variable(missing):
    env = {k: v for k, v in CREDS.items() if k != missing}
    with pytest.raises(StoreConfigError, match=missing):
        store_factory.dsn_from_env(env)


def test_memory_store():
    assert isinstance(store_factory.store_from_env({"SMS_STORE": "memory"}), MemoryStore)


def test_unknown_store_kind():
    with pytest.raises(StoreConfigError, match="SMS_STORE"):
        store_factory.store_from_env({"SMS_STORE": "sqlite"})


def test_postgres_is_default_and_does_not_connect():
    assert isinstance(store_factory.store_from_env(CREDS), PostgresStore)
