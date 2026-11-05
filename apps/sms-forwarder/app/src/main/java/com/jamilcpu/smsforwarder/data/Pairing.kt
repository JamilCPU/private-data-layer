package com.jamilcpu.smsforwarder.data

import java.net.URI
import java.net.URLDecoder
import java.security.MessageDigest
import java.util.Base64

/** [caDer] is the lab CA certificate (DER); [caFingerprint] is its SHA-256, verified at pairing. */
class StoreSettings(
    val url: String,
    val token: String,
    val caFingerprint: String,
    val caDer: ByteArray,
) {
    override fun equals(other: Any?): Boolean =
        other is StoreSettings && url == other.url && token == other.token &&
            caFingerprint == other.caFingerprint && caDer.contentEquals(other.caDer)

    override fun hashCode(): Int = listOf(url, token, caFingerprint).hashCode() * 31 + caDer.contentHashCode()

    override fun toString(): String = "StoreSettings(url=$url, caFingerprint=$caFingerprint)"
}

private const val FINGERPRINT_HEX_LEN = 64
private val HEX_64 = Regex("^[0-9a-fA-F]{$FINGERPRINT_HEX_LEN}$")

/**
 * Parses `pdl-sms://pair?url=<https url>&token=<token>&fp=<sha256 hex>&ca=<base64url DER>`
 * (values percent-encoded). Returns null for any other scheme/host, a non-https url or one without
 * a host, a missing or duplicate field, a bad fingerprint, or a `ca` whose SHA-256 differs from `fp`.
 */
fun parsePairUri(uri: String): StoreSettings? {
    val parsed = try {
        URI(uri.trim())
    } catch (_: Exception) {
        return null
    }
    if (parsed.scheme != "pdl-sms" || parsed.host != "pair") return null
    val params = parseQuery(parsed.rawQuery ?: return null) ?: return null
    val url = params["url"]?.takeIf { it.startsWith("https://") && hasHost(it) } ?: return null
    val token = params["token"]?.takeIf { it.isNotBlank() } ?: return null
    val fp = params["fp"]?.takeIf { HEX_64.matches(it) } ?: return null
    val caDer = decodeBase64Url(params["ca"] ?: return null) ?: return null
    if (sha256Hex(caDer) != fp.lowercase()) return null
    return StoreSettings(url, token, fp.lowercase(), caDer)
}

private fun hasHost(url: String): Boolean =
    try {
        !URI(url).host.isNullOrBlank()
    } catch (_: Exception) {
        false
    }

private fun decodeBase64Url(s: String): ByteArray? =
    try {
        Base64.getUrlDecoder().decode(s).takeIf { it.isNotEmpty() }
    } catch (_: IllegalArgumentException) {
        null
    }

fun sha256Hex(bytes: ByteArray): String =
    MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

private fun parseQuery(query: String): Map<String, String>? {
    val params = mutableMapOf<String, String>()
    for (pair in query.split('&')) {
        val i = pair.indexOf('=')
        if (i <= 0) continue
        val key = decode(pair.substring(0, i)) ?: return null
        if (key in params) return null
        params[key] = decode(pair.substring(i + 1)) ?: return null
    }
    return params
}

private fun decode(s: String): String? =
    try {
        URLDecoder.decode(s, "UTF-8")
    } catch (_: IllegalArgumentException) {
        null
    }

/** Host (and port) of the store url, for display. Never includes the token. */
fun StoreSettings.displayHost(): String =
    try {
        URI(url).authority ?: url
    } catch (_: Exception) {
        url
    }
