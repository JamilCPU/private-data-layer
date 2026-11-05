package com.jamilcpu.smsforwarder.data

import java.security.GeneralSecurityException
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertPathValidator
import java.security.cert.Certificate
import java.security.cert.CertificateException
import java.security.cert.CertificateFactory
import java.security.cert.PKIXParameters
import java.security.cert.TrustAnchor
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager
import okhttp3.OkHttpClient

/** Lowercase hex SHA-256 over the certificate's DER encoding. */
fun certFingerprint(cert: Certificate): String =
    MessageDigest.getInstance("SHA-256").digest(cert.encoded).joinToString("") { "%02x".format(it) }

private fun matches(cert: Certificate, expectedHex: String): Boolean =
    MessageDigest.isEqual(certFingerprint(cert).toByteArray(), expectedHex.lowercase().toByteArray())

/**
 * Trusts a server chain only if some certificate in it has the pinned SHA-256 fingerprint and the
 * certificates presented before it validate (PKIX, no revocation) with that certificate as trust anchor.
 */
class PinnedCaTrustManager(private val fingerprintHex: String) : X509TrustManager {
    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        if (chain.isNullOrEmpty()) throw CertificateException("Empty certificate chain")
        val anchorIndex = chain.indexOfFirst { matches(it, fingerprintHex) }
        if (anchorIndex < 0) throw CertificateException("No certificate matches the paired fingerprint")
        val anchor = chain[anchorIndex]
        if (anchorIndex == 0) {
            anchor.checkValidity()
            return
        }
        validate(chain.take(anchorIndex), anchor)
    }

    private fun validate(path: List<X509Certificate>, anchor: X509Certificate) {
        val certPath = CertificateFactory.getInstance("X.509").generateCertPath(path)
        val params = PKIXParameters(setOf(TrustAnchor(anchor, null))).apply { isRevocationEnabled = false }
        try {
            CertPathValidator.getInstance("PKIX").validate(certPath, params)
        } catch (e: GeneralSecurityException) {
            throw CertificateException("Chain does not validate against the pinned CA", e)
        }
    }

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        throw CertificateException("Client certificates are not supported")
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
}

/** OkHttp client trusting only the paired CA; hostname verification stays OkHttp's default. */
fun pinnedCaOkHttp(fingerprintHex: String): OkHttpClient {
    val trustManager = PinnedCaTrustManager(fingerprintHex)
    val context = SSLContext.getInstance("TLS").apply {
        init(null, arrayOf(trustManager), SecureRandom())
    }
    return OkHttpClient.Builder().sslSocketFactory(context.socketFactory, trustManager).build()
}
