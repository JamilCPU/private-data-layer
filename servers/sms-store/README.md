# sms-store

The receiving end of the SMS forwarder: an HTTP service that accepts batches of text messages
from paired phones, validates them and stores them (Postgres schema `sms`, or in memory for
tests). It also holds the shared layer used by `sms-mcp`: message models, device-token hashing,
the `Store` interface and the migrations. Message text is data, never instructions.

## API

All responses are JSON and carry `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`
and `Referrer-Policy: no-referrer`. No cookies are ever set.

`GET /healthz` returns `200 {"ok": true}`.

`POST /messages` with `Authorization: Bearer <device token>` and a JSON list of 1 to 100 objects:

```json
[{"provider_id": "123", "date_ms": 1700000000000, "address": "+15550001",
  "contact": "Alex", "thread_id": "7", "direction": "in", "body": "hello"}]
```

`contact` and `thread_id` are optional. Texts longer than 2000 characters are cut and end with
` [truncated]`. Re-sending a message is harmless: its id derives from device, `provider_id`
and `date_ms`.

| Status | Body | When |
|--------|------|------|
| 200 | `{"accepted": n, "duplicates": m}` | stored (`accepted` counts new messages) |
| 401 | `{"error": "unauthorized"}` | missing or unknown token (checked before the body is read) |
| 413 | `{"error": "body too large"}` | body over 65536 bytes |
| 422 | `{"error": "item 1: direction: ..."}` | not JSON, not a list of 1..100 objects, or an item fails validation; nothing is stored |
| 429 | `{"error": "rate limited"}` + `Retry-After` | more than 60 requests in 60 s |
| 500 | `{"error": "store error"}` | the database failed |

Any other path returns 404, any other method on `/messages` returns 405 with `Allow: POST`.

Token checks are ordered to keep hashing cheap to defend: a token that is not URL-safe base64
of 32 to 128 characters (`pdl-sms-new-device-token` makes 43) is refused with 401 without
hashing. A token seen valid before is recognised from a small in-memory cache of SHA-256
digests. Otherwise the request first spends from the client-address bucket (429 once over) and
only then is verified, in a worker thread. A verified device is limited by its own key, 60
requests per 60 s, counting failures too. Behind the Ingress the client address is the proxy's,
so that bucket is one shared cap on hashing work for all unverified callers, by design.

## Device tokens

```
pdl-sms-new-device-token --name pixel [--out FILE]
```

prints the token once (give it to the phone) and the `  pixel: <hash>` line to add to the
tokens file. Only salted PBKDF2 hashes are stored; to revoke a phone, delete its line and
restart the service.

```yaml
devices:
  pixel: pbkdf2_sha256$200000$<salt>$<digest>
```

Names are `[a-z0-9_-]`, 1 to 32 characters.

## Pairing URI

The phone app is configured by a URI, shown as a QR code or link:

```
pdl-sms://pair?url=https://sms.lab&token=<token>&fp=<sha256 hex of the lab CA DER>
```

`fp` pins the lab CA certificate so the app trusts only that authority for `sms.lab`.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `SMS_DEVICE_TOKENS_FILE` | required | path of the tokens YAML file |
| `SMS_STORE_LISTEN_HOST` | `0.0.0.0` | bind address |
| `SMS_STORE_LISTEN_PORT` | `8000` | bind port |
| `SMS_STORE` | `postgres` | `postgres` or `memory` (tests, nothing persists) |
| `SMS_APPLY_MIGRATIONS` | off | `1`/`true` applies the migrations at start |
| `PGUSER`, `PGPASSWORD` | required for Postgres | credentials |
| `PGHOST`, `PGPORT`, `PGDATABASE` | lab defaults | connection target |

Config errors print `sms-store: ...` to stderr and exit 1.

## Data model

Schema `sms`: `messages` (deterministic `id`, `device`, `provider_id`, `thread_id`, `address`,
`contact`, `direction` in/out, `body`, `sent_at`, `received_at`, a full-text `tsv` column) and
`devices` (`name`, `last_seen_at`, `last_provider_id`). Inserts never overwrite an existing id.

## Safety notes

- Token check precedes any body parsing and any hashing is rate limited; a token is never logged, nor is a body. One INFO line
  per request: `device=<name|-> status=<code> count=<n>`.
- Request size, batch size and field lengths are bounded; one bad item rejects the whole batch.
- Use TLS in front of the service; bearer tokens travel in the clear otherwise.
- Message text is untrusted data for whatever reads it later.
