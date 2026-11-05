package com.jamilcpu.smsforwarder.data

import java.math.BigInteger
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.Date
import org.bouncycastle.asn1.x500.X500Name
import org.bouncycastle.asn1.x509.BasicConstraints
import org.bouncycastle.asn1.x509.Extension
import org.bouncycastle.cert.jcajce.JcaX509CertificateConverter
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertThrows
import org.junit.Test

class PinnedTlsTest {
    private class Issued(val cert: X509Certificate, val keys: KeyPair)

    private fun keyPair(): KeyPair =
        KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()

    private fun issue(subject: String, issuer: Issued?, ca: Boolean): Issued {
        val keys = keyPair()
        val signer = issuer?.keys ?: keys
        val issuerName = issuer?.cert?.subjectX500Principal?.name ?: subject
        val now = System.currentTimeMillis()
        val builder = JcaX509v3CertificateBuilder(
            X500Name(issuerName),
            BigInteger.valueOf(now),
            Date(now - 60_000),
            Date(now + 3_600_000),
            X500Name(subject),
            keys.public,
        ).addExtension(Extension.basicConstraints, true, BasicConstraints(ca))
        val holder = builder.build(JcaContentSignerBuilder("SHA256withRSA").build(signer.private))
        return Issued(JcaX509CertificateConverter().getCertificate(holder), keys)
    }

    private val ca = issue("CN=Pinned CA", null, true)
    private val leaf = issue("CN=sms.lab", ca, false)
    private val otherCa = issue("CN=Other CA", null, true)
    private val otherLeaf = issue("CN=sms.lab", otherCa, false)

    private fun manager(pin: X509Certificate = ca.cert) = PinnedCaTrustManager(certFingerprint(pin))

    @Test
    fun acceptsLeafPlusPinnedCa() {
        manager().checkServerTrusted(arrayOf(leaf.cert, ca.cert), "RSA")
    }

    @Test
    fun rejectsLeafAlone() {
        assertThrows(CertificateException::class.java) {
            manager().checkServerTrusted(arrayOf(leaf.cert), "RSA")
        }
    }

    @Test
    fun rejectsLeafSignedByDifferentCa() {
        assertThrows(CertificateException::class.java) {
            manager().checkServerTrusted(arrayOf(otherLeaf.cert, otherCa.cert), "RSA")
        }
    }

    @Test
    fun rejectsLeafClaimingPinnedCaWithoutBeingSignedByIt() {
        assertThrows(CertificateException::class.java) {
            manager().checkServerTrusted(arrayOf(otherLeaf.cert, ca.cert), "RSA")
        }
    }

    @Test
    fun rejectsTamperedPin() {
        val tampered = PinnedCaTrustManager("00".repeat(32))
        assertThrows(CertificateException::class.java) {
            tampered.checkServerTrusted(arrayOf(leaf.cert, ca.cert), "RSA")
        }
    }

    @Test
    fun rejectsEmptyChain() {
        assertThrows(CertificateException::class.java) { manager().checkServerTrusted(emptyArray(), "RSA") }
    }

    @Test
    fun clientBuilds() {
        assertNotNull(pinnedCaOkHttp("ab".repeat(32)))
    }
}
