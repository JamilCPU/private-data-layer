package com.jamilcpu.smsforwarder.sms

import androidx.work.ExistingWorkPolicy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

class WorkRequestFactoryTest {
    @Test
    fun `receiver runs are delayed 5 seconds and append so bursts do not cancel an in-flight run`() {
        val p = WorkRequestFactory.receiverPlan
        assertEquals(5L, p.initialDelaySeconds)
        assertEquals(ExistingWorkPolicy.APPEND_OR_REPLACE, p.policy)
    }

    @Test
    fun `sync now is immediate, replaces, and uses its own unique name`() {
        val p = WorkRequestFactory.manualPlan
        assertEquals(0L, p.initialDelaySeconds)
        assertEquals(ExistingWorkPolicy.REPLACE, p.policy)
        assertNotEquals(WorkRequestFactory.receiverPlan.uniqueName, p.uniqueName)
        assertEquals("sms-sweep-manual", p.uniqueName)
    }
}
