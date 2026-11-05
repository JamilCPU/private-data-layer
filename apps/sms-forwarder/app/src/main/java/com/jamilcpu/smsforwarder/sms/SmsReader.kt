package com.jamilcpu.smsforwarder.sms

import android.content.ContentResolver
import android.provider.Telephony

/** One text from the SMS content provider, direction already mapped. */
data class RawSms(
    val id: Long,
    val threadId: Long?,
    val address: String,
    val dateMs: Long,
    val direction: String,
    val body: String,
) {
    /** `_id` alone is reused by AOSP after the newest row is deleted, so the date is part of the key. */
    val key: String get() = providerKey(id, dateMs)
}

fun providerKey(id: Long, dateMs: Long): String = "$id-$dateMs"

/** The `_id` part of a [providerKey], or null if malformed. */
fun keyId(key: String): Long? = key.substringBefore('-').toLongOrNull()

/** One page of the provider scan. [scanned] counts all rows read, including skipped types. */
data class SmsPage(val rows: List<RawSms>, val scanned: Int, val lastDateMs: Long, val lastId: Long)

const val SWEEP_LIMIT = 500

private const val TYPE_INBOX = 1
private const val TYPE_SENT = 2

/** Pure mapping of one provider row. Returns null for rows that are not plain inbox/sent texts. */
fun rowToRaw(id: Long, threadId: Long?, address: String?, dateMs: Long, type: Int, body: String?): RawSms? {
    val direction = when (type) {
        TYPE_INBOX -> "in"
        TYPE_SENT -> "out"
        else -> return null
    }
    if (address.isNullOrBlank()) return null
    return RawSms(id, threadId, address, dateMs, direction, body.orEmpty())
}

object SmsReader {
    private val PROJECTION = arrayOf("_id", "thread_id", "address", "date", "type", "body")
    private const val SELECTION = "date > ? OR (date = ? AND _id > ?)"
    private const val SORT = "date ASC, _id ASC"

    /**
     * Texts after (sinceDateMs, lastProviderId), oldest first. At most [SWEEP_LIMIT] rows are scanned
     * per call; the returned page carries the cursor to continue from.
     */
    fun readSince(resolver: ContentResolver, sinceDateMs: Long, lastProviderId: Long?): SmsPage {
        val args = arrayOf(sinceDateMs.toString(), sinceDateMs.toString(), (lastProviderId ?: -1L).toString())
        val out = ArrayList<RawSms>()
        var scanned = 0
        var lastDate = sinceDateMs
        var lastId = lastProviderId ?: -1L
        resolver.query(Telephony.Sms.CONTENT_URI, PROJECTION, SELECTION, args, SORT)?.use { c ->
            while (scanned < SWEEP_LIMIT && c.moveToNext()) {
                scanned++
                lastId = c.getLong(0)
                lastDate = c.getLong(3)
                val raw = rowToRaw(
                    id = lastId,
                    threadId = if (c.isNull(1)) null else c.getLong(1),
                    address = c.getString(2),
                    dateMs = lastDate,
                    type = c.getInt(4),
                    body = c.getString(5),
                )
                if (raw != null) out += raw
            }
        }
        return SmsPage(out, scanned, lastDate, lastId)
    }
}
