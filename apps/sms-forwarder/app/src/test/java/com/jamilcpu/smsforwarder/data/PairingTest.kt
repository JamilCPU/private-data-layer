package com.jamilcpu.smsforwarder.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PairingTest {
    private val fp = "ab".repeat(32)

    @Test
    fun validUriParses() {
        val s = parsePairUri("pdl-sms://pair?url=https%3A%2F%2Fsms.lab%3A8443&token=tok&fp=$fp")
        assertEquals(StoreSettings("https://sms.lab:8443", "tok", fp), s)
    }

    @Test
    fun wrongSchemeOrHostIsNull() {
        assertNull(parsePairUri("pa://pair?url=https%3A%2F%2Fsms.lab&token=t&fp=$fp"))
        assertNull(parsePairUri("pdl-sms://other?url=https%3A%2F%2Fsms.lab&token=t&fp=$fp"))
    }

    @Test
    fun badFingerprintIsNull() {
        assertNull(parsePairUri("pdl-sms://pair?url=https%3A%2F%2Fsms.lab&token=t&fp=abcd"))
        assertNull(parsePairUri("pdl-sms://pair?url=https%3A%2F%2Fsms.lab&token=t&fp=${"zz".repeat(32)}"))
    }

    @Test
    fun nonHttpsUrlIsNull() {
        assertNull(parsePairUri("pdl-sms://pair?url=http%3A%2F%2Fsms.lab&token=t&fp=$fp"))
    }

    @Test
    fun missingFieldOrGarbageIsNull() {
        assertNull(parsePairUri("pdl-sms://pair?url=https%3A%2F%2Fsms.lab&fp=$fp"))
        assertNull(parsePairUri("pdl-sms://pair?token=t&fp=$fp"))
        assertNull(parsePairUri("not a uri"))
    }

    @Test
    fun encodedTokenDecodedAndFingerprintLowercased() {
        val s = parsePairUri("pdl-sms://pair?url=https%3A%2F%2Fsms.lab&token=a%2Bb%2Fc%3D%20d&fp=${fp.uppercase()}")
        assertEquals("a+b/c= d", s?.token)
        assertEquals(fp, s?.caFingerprint)
    }

    @Test
    fun displayHostOmitsToken() {
        val s = StoreSettings("https://sms.lab:8443/", "secret", fp)
        assertEquals("sms.lab:8443", s.displayHost())
    }
}
