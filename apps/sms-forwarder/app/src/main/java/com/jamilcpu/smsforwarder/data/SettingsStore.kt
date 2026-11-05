package com.jamilcpu.smsforwarder.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

/** Encrypts the store token at rest. Behind an interface so JVM tests can fake the Keystore. */
interface TokenCipher {
    fun encrypt(plain: String): String

    /** Returns null when the value cannot be decrypted (for example the key was lost). */
    fun decrypt(encoded: String): String?
}

/** AES/GCM with a non-exportable key held in the Android Keystore. Output is Base64(iv + ciphertext). */
class KeystoreTokenCipher(private val alias: String = "pdl_sms_token_key") : TokenCipher {
    private fun key(): SecretKey {
        val ks = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (ks.getKey(alias, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                alias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(KEY_BITS)
                .build(),
        )
        return generator.generateKey()
    }

    override fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION).apply { init(Cipher.ENCRYPT_MODE, key()) }
        val out = cipher.iv + cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
        return Base64.encodeToString(out, Base64.NO_WRAP)
    }

    override fun decrypt(encoded: String): String? =
        try {
            val bytes = Base64.decode(encoded, Base64.NO_WRAP)
            val cipher = Cipher.getInstance(TRANSFORMATION).apply {
                init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(TAG_BITS, bytes, 0, IV_BYTES))
            }
            String(cipher.doFinal(bytes, IV_BYTES, bytes.size - IV_BYTES), Charsets.UTF_8)
        } catch (_: Exception) {
            null
        }

    private companion object {
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val KEY_BITS = 256
        const val TAG_BITS = 128
        const val IV_BYTES = 12
    }
}

private val Context.smsDataStore: DataStore<Preferences> by preferencesDataStore(name = "sms_settings")

private val URL_KEY = stringPreferencesKey("url")
private val TOKEN_KEY = stringPreferencesKey("token_enc")
private val FP_KEY = stringPreferencesKey("ca_fingerprint")
private val LAST_AT_KEY = longPreferencesKey("last_upload_at")
private val LAST_RESULT_KEY = stringPreferencesKey("last_result")
private val TOKEN_VALID_KEY = booleanPreferencesKey("token_valid")
private val WM_DATE_KEY = longPreferencesKey("watermark_date_ms")
private val WM_ID_KEY = longPreferencesKey("watermark_provider_id")

/**
 * Last forwarded SMS: texts strictly after (dateMs, providerId) have not been uploaded yet.
 * Initialised to the pairing time so existing history is never uploaded on first pairing.
 */
data class Watermark(val dateMs: Long, val providerId: Long) {
    fun advancedTo(dateMs: Long, providerId: Long): Watermark =
        if (dateMs > this.dateMs || (dateMs == this.dateMs && providerId > this.providerId)) {
            Watermark(dateMs, providerId)
        } else {
            this
        }
}

/** Outcome of the most recent upload attempt, for the status screen. */
data class UploadStatus(
    val lastUploadAt: Long? = null,
    val lastResult: String? = null,
    val tokenValid: Boolean = true,
)

class SettingsStore(
    context: Context,
    private val cipher: TokenCipher = KeystoreTokenCipher(),
) {
    private val store = context.applicationContext.smsDataStore

    val flow: Flow<StoreSettings?> = store.data.map { prefs ->
        val url = prefs[URL_KEY]
        val fp = prefs[FP_KEY]
        val token = prefs[TOKEN_KEY]?.let(cipher::decrypt)
        if (url == null || fp == null || token == null) null else StoreSettings(url, token, fp)
    }

    val status: Flow<UploadStatus> = store.data.map { prefs ->
        UploadStatus(
            lastUploadAt = prefs[LAST_AT_KEY],
            lastResult = prefs[LAST_RESULT_KEY],
            tokenValid = prefs[TOKEN_VALID_KEY] ?: true,
        )
    }

    suspend fun save(s: StoreSettings) {
        val encrypted = cipher.encrypt(s.token)
        store.edit {
            it[URL_KEY] = s.url
            it[TOKEN_KEY] = encrypted
            it[FP_KEY] = s.caFingerprint
            it[TOKEN_VALID_KEY] = true
            if (it[WM_DATE_KEY] == null) {
                it[WM_DATE_KEY] = System.currentTimeMillis()
                it[WM_ID_KEY] = -1L
            }
        }
    }

    /** Null until the first pairing. Survives re-pairing (so a gap is caught up) but not [clear]. */
    suspend fun watermark(): Watermark? {
        val prefs = store.data.first()
        val date = prefs[WM_DATE_KEY] ?: return null
        return Watermark(date, prefs[WM_ID_KEY] ?: -1L)
    }

    suspend fun setWatermark(w: Watermark) {
        store.edit {
            it[WM_DATE_KEY] = w.dateMs
            it[WM_ID_KEY] = w.providerId
        }
    }

    /** Records a failed attempt without touching the last-successful-upload time. */
    suspend fun recordFailure(result: String, tokenValid: Boolean) {
        store.edit {
            it[LAST_RESULT_KEY] = result
            it[TOKEN_VALID_KEY] = tokenValid
        }
    }

    suspend fun recordUpload(at: Long, result: String, tokenValid: Boolean) {
        store.edit {
            it[LAST_AT_KEY] = at
            it[LAST_RESULT_KEY] = result
            it[TOKEN_VALID_KEY] = tokenValid
        }
    }

    suspend fun clear() {
        store.edit { it.clear() }
    }
}
