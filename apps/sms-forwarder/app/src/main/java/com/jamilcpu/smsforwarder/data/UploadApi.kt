package com.jamilcpu.smsforwarder.data

import io.ktor.client.HttpClient
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.timeout
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.builtins.ListSerializer

private const val TIMEOUT_MS = 30_000L

/** A Ktor client over [engine]; JSON is handled by UploadApi itself. */
fun buildUploadClient(engine: HttpClientEngine): HttpClient = HttpClient(engine) {
    install(HttpTimeout)
    expectSuccess = false
}

/** The real client: OkHttp engine trusting only the paired CA. */
fun buildPinnedUploadClient(settings: StoreSettings): HttpClient =
    HttpClient(OkHttp) {
        install(HttpTimeout)
        expectSuccess = false
        engine { preconfigured = pinnedCaOkHttp(settings.caFingerprint) }
    }

class UploadApi(private val client: HttpClient, settings: StoreSettings) {
    private val endpoint = settings.url.trimEnd('/') + "/messages"
    private val token = settings.token

    suspend fun post(messages: List<OutgoingMessage>): UploadResult =
        try {
            val body = smsJson.encodeToString(ListSerializer(OutgoingMessage.serializer()), messages)
            map(send(body))
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            UploadResult.Failed("Cannot reach the store (${e.javaClass.simpleName})")
        }

    private suspend fun send(body: String): HttpResponse =
        client.post(endpoint) {
            bearerAuth(token)
            contentType(ContentType.Application.Json)
            setBody(body)
            timeout {
                requestTimeoutMillis = TIMEOUT_MS
                socketTimeoutMillis = TIMEOUT_MS
                connectTimeoutMillis = TIMEOUT_MS
            }
        }

    private suspend fun map(response: HttpResponse): UploadResult =
        when (response.status) {
            HttpStatusCode.OK -> parseReply(response.bodyAsText())
            HttpStatusCode.Unauthorized -> UploadResult.Unauthorized
            else -> UploadResult.Failed("Store replied ${response.status.value}")
        }

    private fun parseReply(text: String): UploadResult =
        try {
            val r = smsJson.decodeFromString(UploadReply.serializer(), text)
            UploadResult.Accepted(r.accepted, r.duplicates)
        } catch (_: Exception) {
            UploadResult.Failed("Unexpected reply from the store")
        }
}
