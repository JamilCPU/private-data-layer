package com.jamilcpu.smsforwarder.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

@Serializable
data class OutgoingMessage(
    @SerialName("provider_id") val providerId: String,
    @SerialName("date_ms") val dateMs: Long,
    val address: String,
    val contact: String?,
    val direction: String,
    val body: String,
    @SerialName("thread_id") val threadId: String?,
)

@Serializable
data class UploadReply(val accepted: Int = 0, val duplicates: Int = 0)

sealed interface UploadResult {
    data class Accepted(val accepted: Int, val duplicates: Int) : UploadResult

    data object Unauthorized : UploadResult

    data class Failed(val reason: String) : UploadResult

    /** The store refused this payload (4xx other than 401 and 429, including 413 too large). */
    data class Rejected(val status: Int) : UploadResult

    /** 429: the store asks us to slow down; stop the run and let WorkManager back off. */
    data object RateLimited : UploadResult
}

internal val smsJson = Json {
    ignoreUnknownKeys = true
    explicitNulls = true
}

/** UTF-8 size of [m] as encoded in an upload body. */
internal fun encodedSize(m: OutgoingMessage): Int =
    smsJson.encodeToString(OutgoingMessage.serializer(), m).toByteArray(Charsets.UTF_8).size
