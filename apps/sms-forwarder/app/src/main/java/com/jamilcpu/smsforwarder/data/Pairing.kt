package com.jamilcpu.smsforwarder.data

import java.net.URI
import java.net.URLDecoder

data class StoreSettings(val url: String, val token: String, val caFingerprint: String)

private const val FINGERPRINT_HEX_LEN = 64
private val HEX_64 = Regex("^[0-9a-fA-F]{$FINGERPRINT_HEX_LEN}$")

/**
 * Parses `pdl-sms://pair?url=<https url>&token=<token>&fp=<sha256 hex>` (values percent-encoded).
 * Returns null for any other scheme/host, a non-https url, a missing field or a bad fingerprint.
 */
fun parsePairUri(uri: String): StoreSettings? {
    val parsed = try {
        URI(uri.trim())
    } catch (_: Exception) {
        return null
    }
    if (parsed.scheme != "pdl-sms" || parsed.host != "pair") return null
    val params = parseQuery(parsed.rawQuery ?: return null) ?: return null
    val url = params["url"]?.takeIf { it.startsWith("https://") } ?: return null
    val token = params["token"]?.takeIf { it.isNotBlank() } ?: return null
    val fp = params["fp"]?.takeIf { HEX_64.matches(it) } ?: return null
    return StoreSettings(url, token, fp.lowercase())
}

private fun parseQuery(query: String): Map<String, String>? {
    val params = mutableMapOf<String, String>()
    for (pair in query.split('&')) {
        val i = pair.indexOf('=')
        if (i <= 0) continue
        val key = decode(pair.substring(0, i)) ?: return null
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
