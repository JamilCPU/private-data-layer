package com.jamilcpu.smsforwarder.sms

import com.jamilcpu.smsforwarder.data.OutgoingMessage
import com.jamilcpu.smsforwarder.data.UploadResult
import com.jamilcpu.smsforwarder.data.encodedSize
import com.jamilcpu.smsforwarder.data.Watermark

fun interface BatchUploader {
    suspend fun post(messages: List<OutgoingMessage>): UploadResult
}

/** Reads texts after the watermark (with contact names), oldest first, at most [SWEEP_LIMIT]. */
fun interface Sweeper {
    suspend fun sweep(after: Watermark): List<OutgoingMessage>
}

/** The slice of settings the runner needs. */
interface RunnerState {
    suspend fun watermark(): Watermark

    suspend fun setWatermark(w: Watermark)

    suspend fun recordSuccess(at: Long, result: String)

    suspend fun recordFailure(result: String)

    suspend fun recordUnauthorized()
}

/** `sent` and `failed` rows are kept as a dedupe record for this long (by message date). */
const val PRUNE_AFTER_MS = 7L * 24 * 60 * 60 * 1000

enum class RunOutcome {
    /** Everything swept has been uploaded (possibly nothing to do). */
    Done,

    /** The store could not be reached, errored, or rate-limited us; ask WorkManager to retry later. */
    Retry,

    /** The store rejected the token (401); no retry until the user pairs again. */
    Unauthorized,

    /** READ_SMS was revoked; nothing to retry until the user grants it again. */
    MissingPermission,
}

/** Sweep, enqueue, then upload batches oldest first until the queue is empty or something fails. */
class UploadRunner(
    private val queue: Queue,
    private val uploader: BatchUploader,
    private val state: RunnerState,
    private val sweeper: Sweeper,
    private val clock: () -> Long,
) {
    private class Totals(var accepted: Int = 0, var duplicates: Int = 0, var failed: Int = 0)

    suspend fun run(): RunOutcome {
        val totals = Totals()
        var outcome = RunOutcome.Done
        while (outcome == RunOutcome.Done) {
            val before = state.watermark()
            val swept = try {
                sweeper.sweep(before)
            } catch (e: SecurityException) {
                state.recordFailure("missing SMS permission")
                return RunOutcome.MissingPermission
            }
            queue.enqueueAll(swept)
            outcome = drain(totals)
            val moved = state.watermark() != before
            if (swept.size < SWEEP_LIMIT || !moved) break
        }
        prune()
        if (outcome == RunOutcome.Done && totals.accepted + totals.duplicates + totals.failed > 0) {
            val rejected = if (totals.failed > 0) ", ${totals.failed} rejected" else ""
            state.recordSuccess(clock(), "uploaded ${totals.accepted}, ${totals.duplicates} already stored$rejected")
        }
        return outcome
    }

    /** Drops finished rows older than [PRUNE_AFTER_MS], but never ones the look-back could still revisit. */
    private suspend fun prune() {
        val lookbackStart = state.watermark().dateMs - LOOKBACK_MS
        queue.pruneOlderThan(minOf(clock() - PRUNE_AFTER_MS, lookbackStart))
    }

    private suspend fun drain(totals: Totals): RunOutcome {
        while (true) {
            val batch = capBySize(queue.nextBatch(BATCH_SIZE), MAX_BATCH_BYTES)
            if (batch.isEmpty()) return RunOutcome.Done
            val stop = send(batch, totals)
            if (stop != null) return stop
        }
    }

    /** Posts [batch]; returns an outcome only when the run must stop. */
    private suspend fun send(batch: List<OutgoingMessage>, totals: Totals): RunOutcome? =
        when (val r = uploader.post(batch)) {
            is UploadResult.Accepted -> {
                queue.markSent(batch.map { it.providerId })
                advance(batch)
                totals.accepted += r.accepted
                totals.duplicates += r.duplicates
                null
            }
            UploadResult.Unauthorized -> {
                state.recordUnauthorized()
                RunOutcome.Unauthorized
            }
            UploadResult.RateLimited -> {
                state.recordFailure("failed: rate limited")
                RunOutcome.Retry
            }
            is UploadResult.Failed -> {
                state.recordFailure("failed: ${r.reason}")
                RunOutcome.Retry
            }
            is UploadResult.Rejected -> isolate(batch, r.status, totals)
        }

    /**
     * The store refused the payload itself (413 or 422). A single item is poison: mark it failed so it is
     * never retried. A bigger batch is split in half and each half retried, which isolates it.
     */
    private suspend fun isolate(batch: List<OutgoingMessage>, status: Int, totals: Totals): RunOutcome? {
        if (batch.size == 1) {
            queue.markFailed(listOf(batch[0].providerId))
            totals.failed++
            state.recordFailure("rejected a message (HTTP $status)")
            return null
        }
        val mid = batch.size / 2
        return send(batch.subList(0, mid), totals) ?: send(batch.subList(mid, batch.size), totals)
    }

    private suspend fun advance(batch: List<OutgoingMessage>) {
        var w = state.watermark()
        for (m in batch) {
            val id = keyId(m.providerId) ?: continue
            w = w.advancedTo(m.dateMs, id)
        }
        state.setWatermark(w)
    }
}

/** Longest prefix of [batch] whose encoded JSON array fits in [maxBytes] (at least one item). */
fun capBySize(batch: List<OutgoingMessage>, maxBytes: Int): List<OutgoingMessage> {
    var total = 2 // [ ]
    var n = 0
    for (m in batch) {
        val add = encodedSize(m) + if (n > 0) 1 else 0
        if (n > 0 && total + add > maxBytes) break
        total += add
        n++
    }
    return batch.subList(0, n)
}
