-- The Lab Data Store normally provisions schema "sms" and the owner role; the guard below
-- only creates it on a bare database (CREATE SCHEMA IF NOT EXISTS would demand CREATE privilege
-- on the database even when the schema exists).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'sms') THEN
        CREATE SCHEMA sms;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS sms.messages (
    id text PRIMARY KEY,
    device text NOT NULL,
    provider_id text NOT NULL,
    thread_id text,
    address text NOT NULL,
    contact text,
    direction text NOT NULL CHECK (direction IN ('in', 'out')),
    body text NOT NULL,
    sent_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'simple',
            coalesce(contact, '') || ' ' || coalesce(address, '') || ' ' || coalesce(body, '')
        )
    ) STORED
);

CREATE INDEX IF NOT EXISTS messages_sent_at_idx ON sms.messages (sent_at DESC);
CREATE INDEX IF NOT EXISTS messages_address_idx ON sms.messages (address);
CREATE INDEX IF NOT EXISTS messages_tsv_idx ON sms.messages USING GIN (tsv);
CREATE UNIQUE INDEX IF NOT EXISTS messages_device_provider_idx
    ON sms.messages (device, provider_id, sent_at);

CREATE TABLE IF NOT EXISTS sms.devices (
    name text PRIMARY KEY,
    last_seen_at timestamptz NOT NULL,
    last_provider_id text
);

-- Read-only access for the query role, when the Lab Data Store has provisioned it. Default
-- privileges apply to objects created by the current (owner) role, which runs migrations.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sms_reader') THEN
        GRANT USAGE ON SCHEMA sms TO sms_reader;
        GRANT SELECT ON ALL TABLES IN SCHEMA sms TO sms_reader;
        ALTER DEFAULT PRIVILEGES IN SCHEMA sms GRANT SELECT ON TABLES TO sms_reader;
    END IF;
END
$$;
