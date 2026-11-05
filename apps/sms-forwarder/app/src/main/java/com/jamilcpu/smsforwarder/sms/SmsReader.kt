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
)

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
     * per call, so the caller sweeps again after advancing the watermark.
     */
    fun readSince(resolver: ContentResolver, sinceDateMs: Long, lastProviderId: Long?): List<RawSms> {
        val args = arrayOf(sinceDateMs.toString(), sinceDateMs.toString(), (lastProviderId ?: -1L).toString())
        val out = ArrayList<RawSms>()
        var scanned = 0
        resolver.query(Telephony.Sms.CONTENT_URI, PROJECTION, SELECTION, args, SORT)?.use { c ->
            while (scanned < SWEEP_LIMIT && c.moveToNext()) {
                scanned++
                val raw = rowToRaw(
                    id = c.getLong(0),
                    threadId = if (c.isNull(1)) null else c.getLong(1),
                    address = c.getString(2),
                    dateMs = c.getLong(3),
                    type = c.getInt(4),
                    body = c.getString(5),
                )
                if (raw != null) out += raw
            }
        }
        return out
    }
}
