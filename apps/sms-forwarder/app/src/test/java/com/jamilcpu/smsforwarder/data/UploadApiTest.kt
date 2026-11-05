package com.jamilcpu.smsforwarder.data

import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.engine.mock.respondError
import io.ktor.client.engine.mock.toByteArray
import io.ktor.client.request.HttpRequestData
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class UploadApiTest {
    private val settings = StoreSettings("https://sms.lab:8443/", "tok-123", "ab".repeat(32))
    private val msg = OutgoingMessage("9", 5L, "+15550100", null, "in", "hello", "3")

    private fun api(handler: suspend MockRequestHandleScope.(HttpRequestData) -> HttpResponseData): UploadApi {
        val engine = MockEngine { request -> handler(request) }
        return UploadApi(buildUploadClient(engine), settings)
    }

    @Test
    fun sendsJsonWithBearerAndMapsAccepted() = runTest {
        var captured: HttpRequestData? = null
        var body = ByteArray(0)
        val api = api { req ->
            captured = req
            body = req.body.toByteArray()
            respond(
                """{"accepted":1,"duplicates":0}""",
                HttpStatusCode.OK,
                headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val result = api.post(listOf(msg))
        assertEquals(UploadResult.Accepted(1, 0), result)
        val req = requireNotNull(captured)
        assertEquals(HttpMethod.Post, req.method)
        assertEquals("https://sms.lab:8443/messages", req.url.toString())
        assertEquals("Bearer tok-123", req.headers[HttpHeaders.Authorization])
        assertTrue(req.body.contentType.toString().startsWith("application/json"))
        val first = (smsJson.parseToJsonElement(body.decodeToString()) as JsonArray)[0].jsonObject
        assertEquals("9", first["provider_id"]?.jsonPrimitive?.content)
        assertEquals("hello", first["body"]?.jsonPrimitive?.content)
    }

    @Test
    fun unauthorizedIsMapped() = runTest {
        val result = api { respondError(HttpStatusCode.Unauthorized) }.post(listOf(msg))
        assertEquals(UploadResult.Unauthorized, result)
    }

    @Test
    fun otherStatusIsFailed() = runTest {
        val result = api { respondError(HttpStatusCode.TooManyRequests) }.post(listOf(msg))
        assertTrue(result is UploadResult.Failed)
    }

    @Test
    fun exceptionIsFailed() = runTest {
        val result = api { throw java.io.IOException("boom") }.post(listOf(msg))
        assertTrue(result is UploadResult.Failed)
    }

    @Test
    fun garbledOkBodyIsFailed() = runTest {
        val result = api { respond("not json", HttpStatusCode.OK) }.post(listOf(msg))
        assertTrue(result is UploadResult.Failed)
    }
}
