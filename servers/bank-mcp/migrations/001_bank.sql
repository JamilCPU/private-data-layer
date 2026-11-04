-- bank schema: accounts, transactions, import ledger.
-- The Lab Data Store normally provisions schema "bank" and the owner role; the guard
-- below only creates it on a bare database (CREATE SCHEMA IF NOT EXISTS would demand
-- CREATE privilege on the database even when the schema exists).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'bank') THEN
        CREATE SCHEMA bank;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS bank.accounts (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    institution text,
    currency    text NOT NULL
);

CREATE TABLE IF NOT EXISTS bank.transactions (
    id          text PRIMARY KEY,
    account_id  text NOT NULL REFERENCES bank.accounts (id),
    posted_on   date NOT NULL,
    amount      numeric(14, 2) NOT NULL,
    description text NOT NULL,
    category    text,
    source_file text NOT NULL
);

CREATE INDEX IF NOT EXISTS transactions_posted_on_idx ON bank.transactions (posted_on);
CREATE INDEX IF NOT EXISTS transactions_category_idx ON bank.transactions (category);
CREATE INDEX IF NOT EXISTS transactions_account_posted_idx
    ON bank.transactions (account_id, posted_on);

CREATE TABLE IF NOT EXISTS bank.imports (
    file_hash   text PRIMARY KEY,
    filename    text NOT NULL,
    imported_on date NOT NULL,
    count       integer NOT NULL
);

-- Read-only access for the query role, when the Lab Data Store has provisioned it. Default
-- privileges apply to objects created by the current (owner) role, which runs migrations.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bank_reader') THEN
        GRANT USAGE ON SCHEMA bank TO bank_reader;
        GRANT SELECT ON ALL TABLES IN SCHEMA bank TO bank_reader;
        ALTER DEFAULT PRIVILEGES IN SCHEMA bank GRANT SELECT ON TABLES TO bank_reader;
    END IF;
END
$$;
