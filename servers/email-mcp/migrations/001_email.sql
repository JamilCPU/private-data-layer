CREATE SCHEMA IF NOT EXISTS email;

CREATE TABLE IF NOT EXISTS email.messages (
    id text PRIMARY KEY,
    uid bigint NOT NULL,
    uidvalidity bigint NOT NULL,
    mailbox text NOT NULL,
    from_addr text NOT NULL,
    from_name text,
    to_addrs text[] NOT NULL,
    subject text NOT NULL,
    sent_at timestamptz NOT NULL,
    snippet text NOT NULL,
    body_text text NOT NULL,
    has_attachments boolean NOT NULL,
    message_id_header text,
    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'simple',
            coalesce(subject, '') || ' ' || coalesce(from_addr, '') || ' '
            || coalesce(from_name, '') || ' ' || coalesce(body_text, '')
        )
    ) STORED
);

CREATE INDEX IF NOT EXISTS messages_sent_at_idx ON email.messages (sent_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS messages_mailbox_uid_idx
    ON email.messages (mailbox, uidvalidity, uid);
CREATE INDEX IF NOT EXISTS messages_tsv_idx ON email.messages USING GIN (tsv);

CREATE TABLE IF NOT EXISTS email.sync_state (
    mailbox text PRIMARY KEY,
    uidvalidity bigint NOT NULL,
    last_uid bigint NOT NULL
);
