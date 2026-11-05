package com.jamilcpu.smsforwarder.data

private const val FP_PREFIX_LEN = 16

/** A pairing waiting for the user's explicit confirmation. Nothing is saved until then. */
data class PendingPairing(
    val incoming: StoreSettings,
    val targetHost: String,
    val fingerprintPrefix: String,
    /** Host of the pairing that would be replaced, or null when not yet paired. */
    val replacesHost: String?,
) {
    val message: String
        get() = buildString {
            append("Forward your text messages to $targetHost?\nCA fingerprint: $fingerprintPrefix...")
            if (replacesHost != null) append("\n\nThis replaces the current pairing with $replacesHost.")
        }
}

object PairingDecision {
    /** Every pairing, whether from a VIEW intent or the paste field, goes through confirmation. */
    fun fromIntent(current: StoreSettings?, incoming: StoreSettings): PendingPairing =
        PendingPairing(
            incoming = incoming,
            targetHost = incoming.displayHost(),
            fingerprintPrefix = incoming.caFingerprint.take(FP_PREFIX_LEN),
            replacesHost = current?.displayHost(),
        )
}
