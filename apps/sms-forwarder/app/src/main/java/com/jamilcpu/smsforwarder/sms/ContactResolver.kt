package com.jamilcpu.smsforwarder.sms

import android.content.ContentResolver
import android.net.Uri
import android.provider.ContactsContract
import android.util.LruCache

object ContactResolver {
    private const val CACHE_SIZE = 256
    private const val NO_MATCH = ""
    private val cache = LruCache<String, String>(CACHE_SIZE)

    /** Contact display name for [address], or null when READ_CONTACTS is missing or nothing matches. */
    fun name(resolver: ContentResolver, address: String): String? {
        cache.get(address)?.let { return it.ifEmpty { null } }
        val found = try {
            lookup(resolver, address)
        } catch (_: SecurityException) {
            return null // permission missing: do not cache, it may be granted later
        }
        cache.put(address, found ?: NO_MATCH)
        return found
    }

    private fun lookup(resolver: ContentResolver, address: String): String? {
        val uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(address))
        return resolver.query(uri, arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME), null, null, null)
            ?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
    }
}
