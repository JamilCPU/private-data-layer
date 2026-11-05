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
        q.markSent(listOf(key(1)))
        q.enqueueAll(listOf(msg(1)))
        assertEquals(1, q.rows.size)
        assertEquals(0, q.pendingCount())
    }

    @Test
    fun `nextBatch is oldest first, limited, and skips sent and failed`() = runTest {
        val q = FakeQueue()
        q.enqueueAll(listOf(msg(3, 300), msg(1, 100), msg(2, 200), msg(4, 400)))
        q.markSent(listOf(key(1, 100)))
        q.markFailed(listOf(key(2, 200)))
        assertEquals(listOf(key(3, 300)), q.nextBatch(1).map { it.providerId })
        assertEquals(listOf(key(3, 300), key(4, 400)), q.nextBatch(100).map { it.providerId })
        assertEquals(2, q.pendingCount())
    }

    @Test
    fun `known reports rows in any state and clear forgets them`() = runTest {
        val q = FakeQueue()
        q.enqueueAll(listOf(msg(1), msg(2)))
        q.markSent(listOf(key(1)))
        assertEquals(setOf(key(1), key(2)), q.known(listOf(key(1), key(2), key(3))))
        q.clear()
        assertEquals(emptySet<String>(), q.known(listOf(key(1))))
    }

    @Test
    fun `reused id with a different date is a different row`() = runTest {
        val q = FakeQueue()
        q.enqueueAll(listOf(msg(5, 1000), msg(5, 2000)))
        assertEquals(2, q.rows.size)
    }
}
