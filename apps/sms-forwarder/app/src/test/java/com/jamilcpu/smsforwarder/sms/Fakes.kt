package com.jamilcpu.smsforwarder.sms

import com.jamilcpu.smsforwarder.data.OutgoingMessage
import com.jamilcpu.smsforwarder.data.UploadResult
import com.jamilcpu.smsforwarder.data.Watermark

fun msg(id: Long, dateMs: Long = id * 1000) =
    OutgoingMessage(id.toString(), dateMs, "+100", null, "in", "body $id", "1")

class FakeQueue : Queue {
    val rows = LinkedHashMap<String, Pair<OutgoingMessage, String>>()

    override suspend fun enqueueAll(items: List<OutgoingMessage>) {
        items.forEach { rows.putIfAbsent(it.providerId, it to STATE_PENDING) }
    }

    override suspend fun nextBatch(limit: Int): List<OutgoingMessage> =
        rows.values.filter { it.second == STATE_PENDING }
            .map { it.first }
            .sortedWith(compareBy({ it.dateMs }, { it.providerId.toLong() }))
            .take(limit)

    override suspend fun markSent(ids: List<String>) = set(ids, STATE_SENT)

    override suspend fun markFailed(ids: List<String>) = set(ids, STATE_FAILED)

    private fun set(ids: List<String>, state: String) =
        ids.forEach { id -> rows[id]?.let { rows[id] = it.first to state } }

    override suspend fun pendingCount(): Int = rows.values.count { it.second == STATE_PENDING }

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
