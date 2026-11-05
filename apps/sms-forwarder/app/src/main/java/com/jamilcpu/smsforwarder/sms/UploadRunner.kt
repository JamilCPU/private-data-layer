package com.jamilcpu.smsforwarder.sms

import com.jamilcpu.smsforwarder.data.OutgoingMessage
import com.jamilcpu.smsforwarder.data.UploadResult
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

enum class RunOutcome {
    /** Everything swept has been uploaded (possibly nothing to do). */
    Done,

    /** The store could not be reached or errored; the rest stays pending, ask WorkManager to retry. */
    Retry,

    /** The store rejected the token (401); no retry until the user pairs again. */
    Unauthorized,
}

/** Sweep, enqueue, then upload batches oldest first until the queue is empty or something fails. */
class UploadRunner(
    private val queue: Queue,
    private val uploader: BatchUploader,
    private val state: RunnerState,
    private val sweeper: Sweeper,
    private val clock: () -> Long,
) {
    suspend fun run(): RunOutcome {
        val totals = Totals()
        var outcome = RunOutcome.Done
        while (outcome == RunOutcome.Done) {
            val before = state.watermark()
            val swept = sweeper.sweep(before)
            queue.enqueueAll(swept)
            outcome = drain(totals)
            val moved = state.watermark() != before
            if (swept.size < SWEEP_LIMIT || !moved) break
        }
        if (outcome == RunOutcome.Done && totals.accepted + totals.duplicates > 0) {
            state.recordSuccess(clock(), "uploaded ${totals.accepted}, ${totals.duplicates} already stored")
        }
        return outcome
    }

    private class Totals(var accepted: Int = 0, var duplicates: Int = 0)

    private suspend fun drain(totals: Totals): RunOutcome {
        while (true) {
            val batch = queue.nextBatch(BATCH_SIZE)
            if (batch.isEmpty()) return RunOutcome.Done
            when (val r = uploader.post(batch)) {
                is UploadResult.Accepted -> {
                    queue.markSent(batch.map { it.providerId })
                    advance(batch)
                    totals.accepted += r.accepted
                    totals.duplicates += r.duplicates
                }
                UploadResult.Unauthorized -> {
                    state.recordUnauthorized()
                    return RunOutcome.Unauthorized
                }
                is UploadResult.Failed -> {
                    state.recordFailure("failed: ${r.reason}")
                    return RunOutcome.Retry
                }
            }
        }
    }

    private suspend fun advance(batch: List<OutgoingMessage>) {
        var w = state.watermark()
        for (m in batch) {
            val id = m.providerId.toLongOrNull() ?: continue
            w = w.advancedTo(m.dateMs, id)
        }
        state.setWatermark(w)
    }
}
