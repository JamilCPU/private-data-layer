package com.jamilcpu.smsforwarder.sms

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

/** Pins the Queue contract on the in-memory fake that the runner tests rely on. */
class QueueTest {
    @Test
    fun `enqueue ignores duplicates by provider id even after sent`() = runTest {
        val q = FakeQueue()
        q.enqueueAll(listOf(msg(1), msg(1)))
        q.markSent(listOf("1"))
        q.enqueueAll(listOf(msg(1)))
        assertEquals(1, q.rows.size)
        assertEquals(0, q.pendingCount())
    }

    @Test
    fun `nextBatch is oldest first, limited, and skips sent and failed`() = runTest {
        val q = FakeQueue()
        q.enqueueAll(listOf(msg(3, 300), msg(1, 100), msg(2, 200), msg(4, 400)))
        q.markSent(listOf("1"))
        q.markFailed(listOf("2"))
        assertEquals(listOf("3"), q.nextBatch(1).map { it.providerId })
        assertEquals(listOf("3", "4"), q.nextBatch(100).map { it.providerId })
        assertEquals(2, q.pendingCount())
    }
}
