# SMS Forwarder

Android app that forwards the phone's text messages (incoming and existing) to your own
`sms-store` service over pinned TLS. This is the app skeleton: pairing, pinned TLS, settings,
upload client, a status screen, and the SMS forwarding pipeline described below.

## Permissions

| Permission | Why |
|---|---|
| `INTERNET` | Upload to the paired store. |
| `RECEIVE_SMS` | Be notified of newly arrived messages. |
| `READ_SMS` | Read the existing message history for the initial sync. |
| `READ_CONTACTS` | Attach a contact name to each phone number. |

Cleartext traffic is disabled; the store must be reached over `https://`.

## Pairing

The store operator produces a link of the form

```
pdl-sms://pair?url=https%3A%2F%2Fsms.lab&token=<device token>&fp=<64 hex chars>&ca=<base64url>
```

- `fp` is the SHA-256 fingerprint of the lab CA certificate (DER).
- `ca` is that CA certificate (DER) as base64url without padding. The app refuses the link unless
  the SHA-256 of `ca` equals `fp`, so `fp` is the integrity check of `ca`.

Build the two values from the CA PEM:

```
openssl x509 -in lab-ca.pem -outform DER | base64 -w0 | tr '+/' '-_' | tr -d '='      # ca
openssl x509 -in lab-ca.pem -outform DER | sha256sum | cut -d' ' -f1                   # fp
```

Percent-encode the values when assembling the link. The link is about 1.9 KB, so paste it (or tap it
as a link); it is too long for a QR code.

Opening a link or pressing Pair never saves anything by itself: a confirmation dialog names the target
host and the first 16 hex characters of the CA fingerprint (and, if already paired, says it replaces
the current pairing with the current host). Only an explicit tap on Pair saves; Cancel discards.
The token is stored encrypted with a non-exportable Android Keystore key; the status screen shows only
the store host, never the token. "Unpair" clears all of it.

TLS trust is limited to that CA: the served chain (leaf only, leaf plus intermediates, or leaf plus the
CA) must validate against it as the sole trust anchor (PKIX with signature checks, no revocation).
Hostname verification is the OkHttp default.

## How forwarding works

- `SmsReceiver` (`SMS_RECEIVED`) only starts an immediate upload run. Every text is read from the SMS
  content provider, so the sweep is the single source of truth and a text seen twice is one queue row.
- **Key format.** `provider_id` is `"<_id>-<date_ms>"`. AOSP reuses `_id` after the newest row is
  deleted, so the date is part of the key (queue primary key and the value sent to the store).
- `UploadWorker` runs every 15 minutes (network required), on every received text, and on "Sync now".
  Runs are serialised by a process-wide lock. A run sweeps, queues, then uploads oldest first.
- **Look-back rule.** The watermark is the newest (`date`, `_id`) the store accepted, but the sweep starts
  24 h before it (never before the first-pairing time), because rows can become visible late (sent texts
  flip from outbox to sent, out-of-order inserts, clock changes). It pages the provider 500 rows at a time
  until a page is short or 500 new rows are found; rows the queue already knows do not count. The watermark
  advances only when a batch is accepted (a `duplicates` reply counts) and survives re-pairing.
- **First pairing** never uploads existing history: the first-pairing time is a floor for the look-back.
- **Batch limits.** At most 100 messages and 48 KiB of encoded JSON per request. On 413 or another 4xx
  (not 401/429) the batch is split in half and each half retried, down to one message. A message the store
  still refuses is marked `failed`: it is kept (so it is not re-queued), never retried, and counted as
  "rejected" in the status. 429 stops the run and WorkManager retries with backoff.
- **Offline / 5xx.** The batch stays pending, status shows `failed: <reason>`, WorkManager retries.
- **After revocation (401).** The token is marked invalid, status shows "not paired", the run stops
  without retrying. Pair again to resume.
- **Unpair or a new pairing empties the queue** (old keys are meaningless under the new pairing); the
  look-back re-queues anything still unsent. Pairing also resets the "last result" line.
- A missing READ_SMS permission is recorded as "missing SMS permission" and the run ends quietly.
- Sent rows stay in the queue table as a dedupe record with the body removed.

## Build (on the primary node)

The workstation has no Android SDK. Sync the project to the node and run Gradle there (JDK 21,
SDK at `/opt/android-sdk`):

```
rsync -a --delete --exclude build --exclude .gradle apps/sms-forwarder/ <user>@<node>:~/build/sms-forwarder/
ssh <user>@<node> 'cd ~/build/sms-forwarder && echo sdk.dir=/opt/android-sdk > local.properties \
  && chmod +x gradlew && ./gradlew --no-daemon testDebugUnitTest assembleDebug'
```

`local.properties` is git-ignored. The debug APK is at `app/build/outputs/apk/debug/app-debug.apk`.
