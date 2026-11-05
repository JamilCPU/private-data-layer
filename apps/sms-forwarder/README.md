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
pdl-sms://pair?url=https%3A%2F%2Fsms.lab%3A8443&token=<device token>&fp=<64 hex chars>
```

`fp` is the SHA-256 fingerprint of the store's CA certificate. Tap the link (or paste it into the
"Pairing URI" field and press Pair). The token is stored encrypted with a non-exportable Android
Keystore key; the status screen shows only the store host, never the token. "Unpair" clears all of it.

TLS trust is limited to that CA: the presented chain must contain a certificate with the pinned
fingerprint, and the certificates before it must validate against it (PKIX). Hostname verification
is the OkHttp default.

## How forwarding works

- `SmsReceiver` (`SMS_RECEIVED`) does nothing but start an immediate upload run. Every text is
  therefore read from the SMS content provider and carries its provider `_id` as `provider_id`;
  the sweep is the single source of truth, so a text seen by both the receiver trigger and a
  periodic sweep is one queue row (the queue ignores a repeated `provider_id`).
- `UploadWorker` runs every 15 minutes (network required), on every received text, and on
  "Sync now". A run sweeps the provider for texts after the watermark (up to 500 per sweep,
  repeated while full), resolves contact names, writes them to a Room queue, then uploads
  batches of up to 100, oldest first, until the queue is empty.
- **Watermark rule.** The watermark is the (`date`, `_id`) of the last text the store accepted.
  It is initialised to the pairing time, so existing history is NOT uploaded on first pairing;
  only texts arriving after pairing are forwarded. It advances only after a batch is accepted
  (a `duplicates` reply still counts as accepted) and survives re-pairing, so a gap while the token
  was revoked is caught up. Unpairing clears it.
- **Offline / store errors.** The batch stays pending, the status shows `failed: <reason>`, and
  WorkManager retries with its backoff. The next sweep never loses or duplicates anything.
- **After revocation (401).** The token is marked invalid, the status shows "not paired", the run
  stops without retrying, and nothing more is uploaded until the app is paired again. Queued texts
  are kept and sent after re-pairing.
- Sent rows stay in the queue table as a dedupe record; the server also dedupes on `provider_id`.

## Build (on the primary node)

The workstation has no Android SDK. Sync the project to the node and run Gradle there (JDK 21,
SDK at `/opt/android-sdk`):

```
rsync -a --delete --exclude build --exclude .gradle apps/sms-forwarder/ <user>@<node>:~/build/sms-forwarder/
ssh <user>@<node> 'cd ~/build/sms-forwarder && echo sdk.dir=/opt/android-sdk > local.properties \
  && chmod +x gradlew && ./gradlew --no-daemon testDebugUnitTest assembleDebug'
```

`local.properties` is git-ignored. The debug APK is at `app/build/outputs/apk/debug/app-debug.apk`.
