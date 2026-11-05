package com.jamilcpu.smsforwarder.sms

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SmsReaderTest {
    @Test
    fun `inbox maps to in`() {
        val r = rowToRaw(5, 9, "+4912", 1000, 1, "hi")
        assertEquals(RawSms(5, 9, "+4912", 1000, "in", "hi"), r)
    }

    @Test
    fun `provider key combines id and date`() {
        val r = rowToRaw(5, 9, "+4912", 1000, 1, "hi")!!
        assertEquals("5-1000", r.key)
        assertEquals(5L, keyId(r.key))
    }

    @Test
    fun `sent maps to out`() {
        assertEquals("out", rowToRaw(6, null, "+4912", 2000, 2, "yo")?.direction)
    }

    @Test
    fun `draft, outbox, failed and queued types are skipped`() {
        for (type in listOf(0, 3, 4, 5, 6)) assertNull(rowToRaw(1, 1, "+1", 1, type, "x"))
    }

    @Test
    fun `missing address is skipped and missing body becomes empty`() {
        assertNull(rowToRaw(1, 1, null, 1, 1, "x"))
        assertNull(rowToRaw(1, 1, " ", 1, 1, "x"))
        assertEquals("", rowToRaw(1, 1, "+1", 1, 1, null)?.body)
    }
}
