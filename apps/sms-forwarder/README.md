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

## Build (on the primary node)

The workstation has no Android SDK. Sync the project to the node and run Gradle there (JDK 21,
SDK at `/opt/android-sdk`):

```
rsync -a --delete --exclude build --exclude .gradle apps/sms-forwarder/ <user>@<node>:~/build/sms-forwarder/
ssh <user>@<node> 'cd ~/build/sms-forwarder && echo sdk.dir=/opt/android-sdk > local.properties \
  && chmod +x gradlew && ./gradlew --no-daemon testDebugUnitTest assembleDebug'
```

`local.properties` is git-ignored. The debug APK is at `app/build/outputs/apk/debug/app-debug.apk`.
