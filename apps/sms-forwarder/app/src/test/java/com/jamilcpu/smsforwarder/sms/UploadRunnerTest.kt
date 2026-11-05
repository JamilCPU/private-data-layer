package com.jamilcpu.smsforwarder.sms

import com.jamilcpu.smsforwarder.data.OutgoingMessage
import com.jamilcpu.smsforwarder.data.UploadResult
import com.jamilcpu.smsforwarder.data.Watermark
import com.jamilcpu.smsforwarder.data.encodedSize
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class UploadRunnerTest {
    private val ok = UploadResult.Accepted(100, 0)

    private fun runner(
        queue: FakeQueue,
        up: BatchUploader,
        state: FakeState,
        swept: () -> List<OutgoingMessage>,
    ) = UploadRunner(queue, up, state, { swept() }, { 42L })

    @Test
    fun `duplicate from receiver and sweep is one queued row, reported, and marked sent`() = runTest {
        val queue = FakeQueue()
        queue.enqueueAll(listOf(msg(7))) // an earlier run already queued it
        val up = ScriptedUploader(listOf(UploadResult.Accepted(0, 1)))
        val state = FakeState()
        val outcome = runner(queue, up, state) { listOf(msg(7)) }.run() // the next sweep sees it again

        assertEquals(RunOutcome.Done, outcome)
        assertEquals(1, queue.rows.size)
        assertEquals(1, up.batches.size)
        assertEquals(STATE_SENT, queue.stateOf(key(7)))
        assertEquals("uploaded 0, 1 already stored", state.success)
        assertEquals(Watermark(7000, 7), state.watermark)
    }

    @Test
    fun `300 queued messages upload as three batches of 100 oldest first`() = runTest {
        val queue = FakeQueue()
        // inserted newest first to prove ordering comes from the queue, not insertion
        queue.enqueueAll((300L downTo 1L).map { msg(it) })
        val up = ScriptedUploader(listOf(ok))
        val state = FakeState()
        val outcome = runner(queue, up, state) { emptyList() }.run()

        assertEquals(RunOutcome.Done, outcome)
        assertEquals(listOf(100, 100, 100), up.batches.map { it.size })
        assertEquals((1L..300L).map { key(it) }, up.batches.flatten().map { it.providerId })
        assertEquals(0, queue.pendingCount())
        assertEquals(Watermark(300_000, 300), state.watermark)
    }

    @Test
    fun `401 stops after the first batch and marks not paired without retry`() = runTest {
        val queue = FakeQueue()
        queue.enqueueAll((1L..300L).map { msg(it) })
        val up = ScriptedUploader(listOf(UploadResult.Unauthorized))
        val state = FakeState()
        val outcome = runner(queue, up, state) { emptyList() }.run()

        assertEquals(RunOutcome.Unauthorized, outcome)
        assertEquals(1, up.batches.size)
        assertTrue(state.unauthorized)
        assertEquals(300, queue.pendingCount())
        assertEquals(Watermark(0, -1), state.watermark)
    }

    @Test
    fun `offline failure keeps the rest pending and asks for retry`() = runTest {
        val queue = FakeQueue()
        queue.enqueueAll((1L..300L).map { msg(it) })
        val up = ScriptedUploader(listOf(ok, UploadResult.Failed("Cannot reach the store (X)")))
        val state = FakeState()
        val outcome = runner(queue, up, state) { emptyList() }.run()

        assertEquals(RunOutcome.Retry, outcome)
        assertEquals(2, up.batches.size)
        assertEquals(200, queue.pendingCount())
        assertEquals("failed: Cannot reach the store (X)", state.failure)
        assertNull(state.success)
        assertFalse(state.unauthorized)
        assertEquals(Watermark(100_000, 100), state.watermark)
    }

    @Test
    fun `full sweep triggers another sweep from the advanced watermark`() = runTest {
        val queue = FakeQueue()
        val state = FakeState()
        val up = ScriptedUploader(listOf(UploadResult.Accepted(100, 0)))
        val sweeper = Sweeper { w ->
            val from = maxOf(w.providerId, 0L) + 1
            (from..minOf(from + SWEEP_LIMIT - 1, 600L)).map { msg(it) }
        }
        val outcome = UploadRunner(queue, up, state, sweeper) { 1L }.run()

        assertEquals(RunOutcome.Done, outcome)
        assertEquals(600, up.batches.sumOf { it.size })
        assertEquals(Watermark(600_000, 600), state.watermark)
    }

    @Test
    fun `413 halves the batch until it fits`() = runTest {
        val queue = FakeQueue()
        queue.enqueueAll((1L..100L).map { msg(it) })
        val up = BatchUploader { b ->
            if (b.size > 25) UploadResult.Rejected(413) else UploadResult.Accepted(b.size, 0)
        }
        val state = FakeState()
        val outcome = runner(queue, up, state) { emptyList() }.run()

        assertEquals(RunOutcome.Done, outcome)
        assertEquals(0, queue.pendingCount())
        assertTrue(queue.rows.values.all { it.second == STATE_SENT })
        assertEquals("uploaded 100, 0 already stored", state.success)
    }

    @Test
    fun `422 bisects to the one poison item, fails it and sends the rest`() = runTest {
        val queue = FakeQueue()
        queue.enqueueAll((1L..50L).map { msg(it) })
        val poison = key(17)
        val up = BatchUploader { b ->
            if (b.any { it.providerId == poison }) UploadResult.Rejected(422) else UploadResult.Accepted(b.size, 0)
        }
        val state = FakeState()
        val outcome = runner(queue, up, state) { emptyList() }.run()

        assertEquals(RunOutcome.Done, outcome)
        assertEquals(STATE_FAILED, queue.stateOf(poison))
        assertEquals(49, queue.rows.values.count { it.second == STATE_SENT })
        assertEquals(0, queue.pendingCount())
        assertEquals("uploaded 49, 0 already stored, 1 rejected", state.success)
    }

    @Test
    fun `a single item rejected with 413 is failed`() = runTest {
        val queue = FakeQueue()
        queue.enqueueAll(listOf(msg(1)))
        val up = ScriptedUploader(listOf(UploadResult.Rejected(413)))
        val outcome = runner(queue, up, FakeState()) { emptyList() }.run()
        assertEquals(RunOutcome.Done, outcome)
        assertEquals(STATE_FAILED, queue.stateOf(key(1)))
    }

    @Test
    fun `429 stops the run and asks for retry`() = runTest {
        val queue = FakeQueue()
        queue.enqueueAll((1L..300L).map { msg(it) })
        val up = ScriptedUploader(listOf(ok, UploadResult.RateLimited))
        val state = FakeState()
        val outcome = runner(queue, up, state) { emptyList() }.run()
        assertEquals(RunOutcome.Retry, outcome)
        assertEquals(2, up.batches.size)
        assertEquals(200, queue.pendingCount())
        assertEquals("failed: rate limited", state.failure)
    }

    @Test
    fun `batches are capped by encoded size as well as count`() = runTest {
        val queue = FakeQueue()
        val big = "x".repeat(20_000)
        queue.enqueueAll((1L..5L).map { msg(it).copy(body = big) })
        val up = ScriptedUploader(listOf(UploadResult.Accepted(1, 0)))
        runner(queue, up, FakeState()) { emptyList() }.run()
        assertEquals(listOf(2, 2, 1), up.batches.map { it.size })
        assertTrue(up.batches.all { b -> b.sumOf { encodedSize(it) } < MAX_BATCH_BYTES })
    }

    @Test
    fun `missing SMS permission is recorded and ends the run quietly`() = runTest {
        val state = FakeState()
        val outcome = runner(FakeQueue(), ScriptedUploader(listOf(ok)), state) { throw SecurityException("READ_SMS") }.run()
        assertEquals(RunOutcome.MissingPermission, outcome)
        assertEquals("missing SMS permission", state.failure)
    }

    @Test
    fun `nothing to do records nothing`() = runTest {
        val state = FakeState()
        val outcome = runner(FakeQueue(), ScriptedUploader(listOf(ok)), state) { emptyList() }.run()
        assertEquals(RunOutcome.Done, outcome)
        assertNull(state.success)
    }
}
