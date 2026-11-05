package com.jamilcpu.smsforwarder.data

import java.util.Base64
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PairingTest {
    private val caDer = ByteArray(64) { (it * 7).toByte() }
    private val fp = sha256Hex(caDer)
    private val ca = Base64.getUrlEncoder().withoutPadding().encodeToString(caDer)
    private val base = "pdl-sms://pair?url=https%3A%2F%2Fsms.lab%3A8443&token=tok&fp=$fp&ca=$ca"

    @Test
    fun validUriParses() {
        val s = parsePairUri(base)
        assertEquals(StoreSettings("https://sms.lab:8443", "tok", fp, caDer), s)
        assertArrayEquals(caDer, s?.caDer)
    }

    @Test
    fun missingCaIsNull() {
        assertNull(parsePairUri("pdl-sms://pair?url=https%3A%2F%2Fsms.lab&token=t&fp=$fp"))
    }

    @Test
    fun caNotMatchingFingerprintIsNull() {
        val other = Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(64) { 9 })
        assertNull(parsePairUri("pdl-sms://pair?url=https%3A%2F%2Fsms.lab&token=t&fp=$fp&ca=$other"))
        assertNull(parsePairUri("pdl-sms://pair?url=https%3A%2F%2Fsms.lab&token=t&fp=${"ab".repeat(32)}&ca=$ca"))
    }

    @Test
    fun undecodableCaIsNull() {
        assertNull(parsePairUri("pdl-sms://pair?url=https%3A%2F%2Fsms.lab&token=t&fp=$fp&ca=!!!"))
    }

    @Test
    fun wrongSchemeOrHostIsNull() {
        assertNull(parsePairUri(base.replace("pdl-sms://", "pa://")))
        assertNull(parsePairUri(base.replace("//pair", "//other")))
    }

    @Test
    fun badFingerprintIsNull() {
        assertNull(parsePairUri(base.replace(fp, "abcd")))
        assertNull(parsePairUri(base.replace(fp, "zz".repeat(32))))
    }

    @Test
    fun nonHttpsOrHostlessUrlIsNull() {
        assertNull(parsePairUri(base.replace("https%3A", "http%3A")))
        assertNull(parsePairUri(base.replace("https%3A%2F%2Fsms.lab%3A8443", "https%3A%2F%2F")))
        assertNull(parsePairUri(base.replace("https%3A%2F%2Fsms.lab%3A8443", "https%3A%2F%2F%3A8443")))
    }

    @Test
    fun duplicateParameterIsNull() {
        assertNull(parsePairUri("$base&token=other"))
        assertNull(parsePairUri("$base&url=https%3A%2F%2Fevil.example"))
    }

    @Test
    fun missingFieldOrGarbageIsNull() {
        assertNull(parsePairUri(base.replace("&token=tok", "")))
        assertNull(parsePairUri(base.replace("url=https%3A%2F%2Fsms.lab%3A8443&", "")))
        assertNull(parsePairUri("not a uri"))
    }

    @Test
    fun encodedTokenDecodedAndFingerprintLowercased() {
        val s = parsePairUri(base.replace("token=tok", "token=a%2Bb%2Fc%3D%20d").replace(fp, fp.uppercase()))
        assertEquals("a+b/c= d", s?.token)
        assertEquals(fp, s?.caFingerprint)
    }

    @Test
    fun displayHostOmitsToken() {
        val s = StoreSettings("https://sms.lab:8443/", "secret", fp, caDer)
        assertEquals("sms.lab:8443", s.displayHost())
    }
}
