package com.jamilcpu.smsforwarder.sms

import com.jamilcpu.smsforwarder.data.OutgoingMessage
import com.jamilcpu.smsforwarder.data.UploadResult
import com.jamilcpu.smsforwarder.data.Watermark

fun key(id: Long, dateMs: Long = id * 1000) = "$id-$dateMs"

fun msg(id: Long, dateMs: Long = id * 1000) =
    OutgoingMessage(key(id, dateMs), dateMs, "+100", null, "in", "body $id", "1")

class FakeQueue : Queue {
    val rows = LinkedHashMap<String, Pair<OutgoingMessage, String>>()

    override suspend fun enqueueAll(items: List<OutgoingMessage>) {
        items.forEach { rows.putIfAbsent(it.providerId, it to STATE_PENDING) }
    }

    override suspend fun nextBatch(limit: Int): List<OutgoingMessage> =
        rows.values.filter { it.second == STATE_PENDING }
            .map { it.first }
            .sortedWith(compareBy({ it.dateMs }, { keyId(it.providerId) }))
            .take(limit)

    override suspend fun markSent(ids: List<String>) = set(ids, STATE_SENT)

    override suspend fun markFailed(ids: List<String>) = set(ids, STATE_FAILED)

    private fun set(ids: List<String>, state: String) =
        ids.forEach { id -> rows[id]?.let { rows[id] = it.first to state } }

    override suspend fun pendingCount(): Int = rows.values.count { it.second == STATE_PENDING }

    override suspend fun known(ids: List<String>): Set<String> = ids.filter { it in rows }.toSet()

    override suspend fun clear() = rows.clear()

    val pruneCutoffs = mutableListOf<Long>()

    override suspend fun pruneOlderThan(dateMs: Long) {
        pruneCutoffs += dateMs
        rows.entries.removeAll { it.value.second != STATE_PENDING && it.value.first.dateMs < dateMs }
    }

    fun stateOf(id: String): String? = rows[id]?.second
}

class FakeState(var watermark: Watermark = Watermark(0, -1)) : RunnerState {
    var success: String? = null
    var failure: String? = null
    var unauthorized = false

    override suspend fun watermark() = watermark

    override suspend fun setWatermark(w: Watermark) {
        watermark = w
    }

    override suspend fun recordSuccess(at: Long, result: String) {
        success = result
    }

    override suspend fun recordFailure(result: String) {
        failure = result
    }

    override suspend fun recordUnauthorized() {
        unauthorized = true
    }
}

/** Replies from [script] in order; the last reply repeats. Records every batch posted. */
class ScriptedUploader(private val script: List<UploadResult>) : BatchUploader {
    val batches = mutableListOf<List<OutgoingMessage>>()

    override suspend fun post(messages: List<OutgoingMessage>): UploadResult {
        batches += messages
        return script[minOf(batches.size - 1, script.lastIndex)]
    }
}

/** A provider backed by a mutable list, paged like [SmsReader.readSince]. */
class FakeProvider(val rows: MutableList<RawSms> = mutableListOf()) : PageReader {
    var reads = 0

    override suspend fun read(sinceDateMs: Long, afterId: Long): SmsPage {
        reads++
        val sorted = rows.sortedWith(compareBy({ it.dateMs }, { it.id }))
            .filter { it.dateMs > sinceDateMs || (it.dateMs == sinceDateMs && it.id > afterId) }
            .take(SWEEP_LIMIT)
        val last = sorted.lastOrNull()
        return SmsPage(sorted, sorted.size, last?.dateMs ?: sinceDateMs, last?.id ?: afterId)
    }

    fun add(id: Long, dateMs: Long) {
        rows += RawSms(id, 1, "+100", dateMs, "in", "b$id")
    }
}
