package com.jamilcpu.smsforwarder.sms

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony

/**
 * Only nudges the worker. The sweep reads the SMS provider, so every text carries its provider
 * `_id` and the sweep stays the single source of truth (no second id scheme to deduplicate).
 */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Telephony.Sms.Intents.SMS_RECEIVED_ACTION) {
            UploadWorker.enqueueNow(context)
        }
    }
}
