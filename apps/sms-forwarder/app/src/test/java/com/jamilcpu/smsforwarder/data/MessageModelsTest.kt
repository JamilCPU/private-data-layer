package com.jamilcpu.smsforwarder.data

import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class MessageModelsTest {
    private fun encode(m: OutgoingMessage): JsonObject {
        val text = smsJson.encodeToString(ListSerializer(OutgoingMessage.serializer()), listOf(m))
        return (smsJson.parseToJsonElement(text) as JsonArray)[0].jsonObject
    }

    @Test
    fun serialisesContractKeys() {
        val o = encode(OutgoingMessage("42", 1_700_000_000_000, "+15550100", "Ann", "in", "hi", "7"))
        assertEquals(
            setOf("provider_id", "date_ms", "address", "contact", "direction", "body", "thread_id"),
            o.keys,
        )
        assertEquals("42", o["provider_id"]?.jsonPrimitive?.content)
        assertEquals("1700000000000", o["date_ms"]?.jsonPrimitive?.content)
    }

    @Test
    fun nullableFieldsAreExplicitNull() {
        val o = encode(OutgoingMessage("1", 1, "x", null, "out", "b", null))
        assertTrue(o["contact"] is JsonNull)
        assertTrue(o["thread_id"] is JsonNull)
    }
}
