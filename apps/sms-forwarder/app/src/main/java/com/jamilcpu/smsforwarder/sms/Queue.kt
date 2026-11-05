package com.jamilcpu.smsforwarder.sms

import com.jamilcpu.smsforwarder.data.OutgoingMessage

const val BATCH_SIZE = 100

/** Upper bound for the encoded JSON body of one upload (the server limit is 64 KiB). */
const val MAX_BATCH_BYTES = 49152

/** Durable buffer between the sweep and the upload. Keyed by the SMS provider `_id`. */
interface Queue {
    /** Adds messages as pending; a message whose providerId is already queued (in any state) is ignored. */
    suspend fun enqueueAll(items: List<OutgoingMessage>)

    /** Pending messages, oldest by dateMs first. */
    suspend fun nextBatch(limit: Int = BATCH_SIZE): List<OutgoingMessage>

    suspend fun markSent(ids: List<String>)

    suspend fun markFailed(ids: List<String>)

    suspend fun pendingCount(): Int

    /** The subset of [ids] already queued in any state (pending, sent or failed). */
    suspend fun known(ids: List<String>): Set<String>

    /** Drops every row. Used on unpair and on a new pairing (old keys are no longer meaningful). */
    suspend fun clear()
}
