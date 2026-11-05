package com.jamilcpu.smsforwarder.sms

import com.jamilcpu.smsforwarder.data.OutgoingMessage
import com.jamilcpu.smsforwarder.data.Watermark

/** Rows that become visible late (sent texts flipping type, out-of-order inserts) land this far behind. */
const val LOOKBACK_MS = 24L * 60 * 60 * 1000

fun interface PageReader {
    suspend fun read(sinceDateMs: Long, afterId: Long): SmsPage
}

/**
 * Scans from [LOOKBACK_MS] before the watermark (never before the first-pairing floor), page by page.
 * Rows the queue already knows are skipped and do not count toward the 500-new cap or the stop
 * condition; the scan ends on a short page or once [SWEEP_LIMIT] new rows are collected.
 */
class LookbackSweeper(
    private val pages: PageReader,
    private val queue: Queue,
    private val contact: suspend (String) -> String?,
) : Sweeper {
    override suspend fun sweep(after: Watermark): List<OutgoingMessage> {
        var sinceDate = maxOf(after.dateMs - LOOKBACK_MS, after.floorMs)
        var afterId = -1L
        val fresh = ArrayList<OutgoingMessage>()
        while (fresh.size < SWEEP_LIMIT) {
            val page = pages.read(sinceDate, afterId)
            val known = queue.known(page.rows.map { it.key })
            for (raw in page.rows) {
                if (fresh.size >= SWEEP_LIMIT) break
                if (raw.key !in known) fresh += raw.toOutgoing(contact(raw.address))
            }
            if (page.scanned < SWEEP_LIMIT) break
            sinceDate = page.lastDateMs
            afterId = page.lastId
        }
        return fresh
    }
}

/** Store limits: contact 128, address 64, thread_id 64 characters; longer values would be a 422. */
const val MAX_CONTACT_CHARS = 128
const val MAX_ADDRESS_CHARS = 64
const val MAX_THREAD_ID_CHARS = 64

/** At most [max] UTF-16 units, never ending on half of a surrogate pair. */
fun clampText(s: String, max: Int): String {
    if (s.length <= max) return s
    val end = if (s[max - 1].isHighSurrogate()) max - 1 else max
    return s.substring(0, end)
}

internal fun RawSms.toOutgoing(contact: String?) = OutgoingMessage(
    providerId = key,
    dateMs = dateMs,
    address = clampText(address, MAX_ADDRESS_CHARS),
    contact = contact?.let { clampText(it, MAX_CONTACT_CHARS) },
    direction = direction,
    body = body,
    threadId = threadId?.toString()?.let { clampText(it, MAX_THREAD_ID_CHARS) },
)
