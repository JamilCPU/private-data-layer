import pytest

from pdl_email import cli_sync, store_factory
from pdl_email.memory_store import MemoryStore
from pdl_email.postgres_store import PostgresStore
from pdl_email.store import StoreConfigError, SyncResult

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
    assert isinstance(store_factory.store_from_env({"EMAIL_STORE": "memory"}), MemoryStore)


def test_unknown_store_kind():
    with pytest.raises(StoreConfigError, match="EMAIL_STORE"):
        store_factory.store_from_env({"EMAIL_STORE": "sqlite"})


@pytest.mark.parametrize(
    ("env_extra", "expected_calls"),
    [({}, 0), ({"EMAIL_APPLY_MIGRATIONS": "0"}, 0), ({"EMAIL_APPLY_MIGRATIONS": "1"}, 1),
     ({"EMAIL_APPLY_MIGRATIONS": "TRUE"}, 1)],
)  # fmt: skip
def test_postgres_store_applies_migrations_only_when_flagged(
    monkeypatch, env_extra, expected_calls
):
    calls = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(store_factory.psycopg, "connect", lambda dsn: FakeConn())
    monkeypatch.setattr(store_factory, "apply_migrations", lambda conn: calls.append(conn))
    store = store_factory.store_from_env({**CREDS, **env_extra})
    assert isinstance(store, PostgresStore)
    assert len(calls) == expected_calls


def test_postgres_from_env_classmethod():
    assert isinstance(PostgresStore.from_env(CREDS), PostgresStore)


def test_cli_postgres_path_calls_store_from_env(monkeypatch, tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("s3cret\n")
    for k, v in {
        "EMAIL_IMAP_HOST": "h",
        "EMAIL_IMAP_USER": "u",
        "EMAIL_IMAP_PASSWORD_FILE": str(pw),
        "EMAIL_STORE": "postgres",
    }.items():
        monkeypatch.setenv(k, v)
    mem, got = MemoryStore(), {}
    monkeypatch.setattr(cli_sync, "store_from_env", lambda e: mem)
    monkeypatch.setattr(cli_sync, "source_factory", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_sync, "sync_once", lambda s, st, **k: got.update(st=st) or SyncResult(0, 0, False)
    )
    assert cli_sync.main([]) == 0
    assert got["st"] is mem
