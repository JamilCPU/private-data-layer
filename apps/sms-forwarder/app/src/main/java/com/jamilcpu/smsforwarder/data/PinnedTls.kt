package com.jamilcpu.smsforwarder.data

import java.security.GeneralSecurityException
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
fun certFingerprint(cert: Certificate): String = sha256Hex(cert.encoded)

fun parseCaCertificate(der: ByteArray): X509Certificate =
    CertificateFactory.getInstance("X.509").generateCertificate(der.inputStream()) as X509Certificate

/**
 * Trusts a served chain (leaf, leaf + intermediates, or leaf + the CA itself) only if it validates
 * with [anchor] as the sole trust anchor: PKIX with signature checks, revocation off.
 */
class PinnedCaTrustManager(private val anchor: X509Certificate) : X509TrustManager {
    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        if (chain.isNullOrEmpty()) throw CertificateException("Empty certificate chain")
        anchor.checkValidity()
        val path = chain.toList().filterNot { it == anchor }
        if (path.isEmpty()) throw CertificateException("The server sent only the CA certificate")
        validate(path)
    }

    private fun validate(path: List<X509Certificate>) {
        val certPath = CertificateFactory.getInstance("X.509").generateCertPath(path)
        val params = PKIXParameters(setOf(TrustAnchor(anchor, null))).apply { isRevocationEnabled = false }
        try {
            CertPathValidator.getInstance("PKIX").validate(certPath, params)
        } catch (e: GeneralSecurityException) {
            throw CertificateException("Chain does not validate against the paired CA", e)
        }
    }

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        throw CertificateException("Client certificates are not supported")
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf(anchor)
}

/** OkHttp client trusting only the paired CA; hostname verification stays OkHttp's default. */
fun pinnedCaOkHttp(caDer: ByteArray): OkHttpClient {
    val trustManager = PinnedCaTrustManager(parseCaCertificate(caDer))
    val context = SSLContext.getInstance("TLS").apply {
        init(null, arrayOf(trustManager), SecureRandom())
    }
    return OkHttpClient.Builder().sslSocketFactory(context.socketFactory, trustManager).build()
}
