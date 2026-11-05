package com.jamilcpu.smsforwarder.sms

import com.jamilcpu.smsforwarder.data.UploadResult
import com.jamilcpu.smsforwarder.data.Watermark
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

class LookbackSweeperTest {
    private val hour = 3_600_000L
    private val now = 1_000 * hour

    private fun sweeper(p: FakeProvider, q: Queue) = LookbackSweeper(p, q) { null }

    @Test
    fun `a row appearing behind the watermark on the next run is still forwarded`() = runTest {
        val provider = FakeProvider()
        provider.add(1, now - 5 * hour)
        provider.add(2, now - hour)
        val queue = FakeQueue()
        val state = FakeState(Watermark(now - 2 * hour, -1, now - 100 * hour))
        val up = ScriptedUploader(listOf(UploadResult.Accepted(1, 0)))
        val runner = UploadRunner(queue, up, state, sweeper(provider, queue)) { now }

        runner.run()
        assertEquals(setOf(key(1, now - 5 * hour), key(2, now - hour)), queue.rows.keys)

        // a sent text flips to type 2 later, dated before the watermark
        provider.add(3, now - 3 * hour)
        runner.run()
        assertEquals(key(3, now - 3 * hour), up.batches.last().single().providerId)
        assertEquals(3, queue.rows.size)
    }

    @Test
    fun `a busy day of 1500 known rows plus 10 new forwards the 10`() = runTest {
        val provider = FakeProvider()
        val queue = FakeQueue()
        for (i in 1L..1500L) {
            provider.add(i, now - 20 * hour + i)
            queue.enqueueAll(listOf(msg(i, now - 20 * hour + i)))
            queue.markSent(listOf(key(i, now - 20 * hour + i)))
        }
        for (i in 1501L..1510L) provider.add(i, now - hour + i)
        val fresh = sweeper(provider, queue).sweep(Watermark(now, -1, 0))
        assertEquals((1501L..1510L).map { key(it, now - hour + it) }, fresh.map { it.providerId })
        assertEquals(4, provider.reads)
    }

    @Test
    fun `new rows are capped at 500 per sweep`() = runTest {
        val provider = FakeProvider()
        for (i in 1L..700L) provider.add(i, now - hour + i)
        val fresh = sweeper(provider, FakeQueue()).sweep(Watermark(now, -1, 0))
        assertEquals(500, fresh.size)
        assertEquals(key(1, now - hour + 1), fresh.first().providerId)
    }

    @Test
    fun `look-back never reaches before the first-pairing floor`() = runTest {
        val provider = FakeProvider()
        provider.add(1, now - 10 * hour) // history before pairing
        provider.add(2, now - hour)
        val fresh = sweeper(provider, FakeQueue()).sweep(Watermark(now - 2 * hour, -1, now - 3 * hour))
        assertEquals(listOf(key(2, now - hour)), fresh.map { it.providerId })
    }

    @Test
    fun `a reused id with a new date is forwarded as a new message`() = runTest {
        val provider = FakeProvider()
        val queue = FakeQueue()
        queue.enqueueAll(listOf(msg(9, now - 3 * hour)))
        provider.add(9, now - hour) // AOSP reused _id 9 after the old row was deleted
        val fresh = sweeper(provider, queue).sweep(Watermark(now, -1, 0))
        assertEquals(listOf(key(9, now - hour)), fresh.map { it.providerId })
    }
}
