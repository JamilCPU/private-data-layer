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
}

internal val smsJson = Json {
    ignoreUnknownKeys = true
    explicitNulls = true
}
