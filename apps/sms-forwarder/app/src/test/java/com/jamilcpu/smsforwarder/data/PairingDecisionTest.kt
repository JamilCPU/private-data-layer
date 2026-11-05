package com.jamilcpu.smsforwarder.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PairingDecisionTest {
    private val fp = "0123456789abcdef".repeat(4)
    private val incoming = StoreSettings("https://sms.lab:8443", "tok", fp, byteArrayOf(1, 2))
    private val current = StoreSettings("https://other.lab", "old", "ab".repeat(32), byteArrayOf(3))

    @Test
    fun firstPairingNamesHostAndFingerprintPrefix() {
        val p = PairingDecision.fromIntent(null, incoming)
        assertEquals("sms.lab:8443", p.targetHost)
        assertEquals("0123456789abcdef", p.fingerprintPrefix)
        assertNull(p.replacesHost)
        assertEquals(incoming, p.incoming)
        assertTrue(p.message.contains("sms.lab:8443"))
        assertTrue(p.message.contains("0123456789abcdef"))
        assertTrue(!p.message.contains("replaces"))
    }

    @Test
    fun rePairingSaysItReplacesTheCurrentHost() {
        val p = PairingDecision.fromIntent(current, incoming)
        assertEquals("other.lab", p.replacesHost)
        assertTrue(p.message.contains("This replaces the current pairing with other.lab"))
    }

    @Test
    fun messageNeverContainsToken() {
        assertTrue(!PairingDecision.fromIntent(current, incoming).message.contains("tok"))
    }
}
