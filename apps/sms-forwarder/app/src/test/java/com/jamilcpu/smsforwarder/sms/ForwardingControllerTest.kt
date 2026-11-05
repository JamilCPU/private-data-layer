package com.jamilcpu.smsforwarder.sms

import com.jamilcpu.smsforwarder.data.StoreSettings
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.yield
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class ForwardingControllerTest {
    private val fp = "ab".repeat(32)
    private fun settings(host: String) = StoreSettings("https://$host", "tok-$host", fp, byteArrayOf(1))

    /** Fake world: records the order of side effects and what a run would observe. */
    private class World {
        val lock = Mutex()
        val events = mutableListOf<String>()
        var current: String? = "old.lab"
        val queue = FakeQueue()

        suspend fun controller() = ForwardingController(
            lock = lock,
            cancelWork = { events += "cancel" },
            clearQueue = {
                yield() // give a waiting run every chance to sneak in
                queue.clear()
                events += "clear"
            },
            saveSettings = {
                yield()
                current = it.url.removePrefix("https://")
                events += "save"
            },
            clearSettings = {
                current = null
                events += "clearSettings"
            },
            schedulePeriodic = { events += "schedule" },
        )

        /** What UploadWorker does: take the lock, then read settings and the queue. */
        suspend fun run(): Pair<String?, Int> = lock.withLock { current to queue.rows.size }
    }

    @Test
    fun `re-pair cancels, clears the queue, saves, then schedules`() = runTest {
        val w = World()
        w.queue.enqueueAll(listOf(msg(1), msg(2)))
        w.controller().rePair(settings("new.lab"))
        assertEquals(listOf("cancel", "clear", "save", "schedule"), w.events)
        assertEquals("new.lab", w.current)
        assertEquals(0, w.queue.rows.size)
    }

    @Test
    fun `a run queued during re-pair sees the new settings and an empty queue`() = runTest {
        val w = World()
        w.queue.enqueueAll(listOf(msg(1), msg(2)))
        val controller = w.controller()
        var observed: Pair<String?, Int>? = null
        launch { controller.rePair(settings("new.lab")) }
        launch { observed = w.run() } // starts while the re-pair holds the lock
        advanceUntilIdle()
        assertEquals("new.lab" to 0, observed)
    }

    @Test
    fun `a run queued during unpair finds no settings`() = runTest {
        val w = World()
        w.queue.enqueueAll(listOf(msg(1)))
        val controller = w.controller()
        var observed: Pair<String?, Int>? = null
        launch { controller.unpair() }
        launch { observed = w.run() }
        advanceUntilIdle()
        assertEquals(listOf("cancel", "clear", "clearSettings"), w.events)
        assertEquals(null to 0, observed)
        assertFalse(w.lock.isLocked)
    }
}
