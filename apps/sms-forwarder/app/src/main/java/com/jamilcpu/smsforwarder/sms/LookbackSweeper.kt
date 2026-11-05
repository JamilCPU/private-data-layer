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

private fun RawSms.toOutgoing(contact: String?) = OutgoingMessage(
    providerId = key,
    dateMs = dateMs,
    address = address,
    contact = contact,
    direction = direction,
    body = body,
    threadId = threadId?.toString(),
)
