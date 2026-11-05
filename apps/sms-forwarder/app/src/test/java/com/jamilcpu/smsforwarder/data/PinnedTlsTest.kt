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

    private val hour = 3_600_000L
    private val keyGen = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }

    private fun issue(
        subject: String,
        issuer: Issued?,
        ca: Boolean,
        notBefore: Long = -hour,
        notAfter: Long = hour,
    ): Issued {
        val keys = keyGen.generateKeyPair()
        val signer = issuer?.keys ?: keys
        val issuerName = issuer?.cert?.subjectX500Principal?.name ?: subject
        val now = System.currentTimeMillis()
        val builder = JcaX509v3CertificateBuilder(
            X500Name(issuerName),
            BigInteger.valueOf(now + subject.hashCode()),
            Date(now + notBefore),
            Date(now + notAfter),
            X500Name(subject),
            keys.public,
        ).addExtension(Extension.basicConstraints, true, BasicConstraints(ca))
        val holder = builder.build(JcaContentSignerBuilder("SHA256withRSA").build(signer.private))
        return Issued(JcaX509CertificateConverter().getCertificate(holder), keys)
    }

    private val ca = issue("CN=Pinned CA", null, true)
    private val leaf = issue("CN=sms.lab", ca, false)

    private fun manager(anchor: X509Certificate = ca.cert) = PinnedCaTrustManager(anchor)

    private fun assertRejected(m: PinnedCaTrustManager, vararg chain: X509Certificate) {
        assertThrows(CertificateException::class.java) { m.checkServerTrusted(chain, "RSA") }
    }

    @Test
    fun acceptsLeafOnlyChainSignedByAnchor() {
        manager().checkServerTrusted(arrayOf(leaf.cert), "RSA")
    }

    @Test
    fun acceptsLeafPlusAnchorItself() {
        manager().checkServerTrusted(arrayOf(leaf.cert, ca.cert), "RSA")
    }

    @Test
    fun acceptsChainThroughIntermediate() {
        val inter = issue("CN=Intermediate", ca, true)
        val deep = issue("CN=sms.lab", inter, false)
        manager().checkServerTrusted(arrayOf(deep.cert, inter.cert), "RSA")
    }

    @Test
    fun rejectsLeafSignedByDifferentCa() {
        val otherCa = issue("CN=Other CA", null, true)
        assertRejected(manager(), issue("CN=sms.lab", otherCa, false).cert)
    }

    @Test
    fun rejectsRogueCaWithSameSubjectName() {
        val rogue = issue("CN=Pinned CA", null, true)
        val rogueLeaf = issue("CN=sms.lab", rogue, false)
        assertRejected(manager(), rogueLeaf.cert)
        assertRejected(manager(), rogueLeaf.cert, rogue.cert)
    }

    @Test
    fun rejectsExpiredLeaf() {
        val expired = issue("CN=sms.lab", ca, false, notBefore = -2 * hour, notAfter = -hour)
        assertRejected(manager(), expired.cert)
    }

    @Test
    fun rejectsExpiredAnchor() {
        val oldCa = issue("CN=Old CA", null, true, notBefore = -2 * hour, notAfter = -hour)
        assertRejected(PinnedCaTrustManager(oldCa.cert), issue("CN=sms.lab", oldCa, false).cert)
    }

    @Test
    fun rejectsAnchorAloneAndEmptyChain() {
        assertRejected(manager(), ca.cert)
        assertRejected(manager())
    }

    @Test
    fun clientBuildsFromDer() {
        assertNotNull(pinnedCaOkHttp(ca.cert.encoded))
    }
}
