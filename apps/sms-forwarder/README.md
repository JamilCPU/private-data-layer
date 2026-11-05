# SMS Forwarder

Android app that forwards the phone's text messages (incoming and existing) to your own
`sms-store` service over pinned TLS. This is the app skeleton: pairing, pinned TLS, settings,
upload client and a status screen. The SMS receiver and background sync arrive in a later task.

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

## Build (on the primary node)

The workstation has no Android SDK. Sync the project to the node and run Gradle there (JDK 21,
SDK at `/opt/android-sdk`):

```
rsync -a --delete --exclude build --exclude .gradle apps/sms-forwarder/ <user>@<node>:~/build/sms-forwarder/
ssh <user>@<node> 'cd ~/build/sms-forwarder && echo sdk.dir=/opt/android-sdk > local.properties \
  && chmod +x gradlew && ./gradlew --no-daemon testDebugUnitTest assembleDebug'
```

`local.properties` is git-ignored. The debug APK is at `app/build/outputs/apk/debug/app-debug.apk`.
