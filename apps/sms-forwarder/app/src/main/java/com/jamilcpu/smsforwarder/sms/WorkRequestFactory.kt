package com.jamilcpu.smsforwarder.sms

import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequest
import androidx.work.OneTimeWorkRequestBuilder
import java.util.concurrent.TimeUnit

/** Shape of a one-time upload run: its unique name, start delay and what to do about a pending run. */
data class OneTimePlan(val uniqueName: String, val initialDelaySeconds: Long, val policy: ExistingWorkPolicy)

object WorkRequestFactory {
    /**
     * SMS_RECEIVED can arrive before the default SMS app has written the row to the provider, so the
     * run starts a few seconds later. APPEND_OR_REPLACE queues behind an in-flight run instead of
     * cancelling it, so a burst of texts does not keep restarting the upload.
     */
    const val RECEIVER_DELAY_SECONDS = 5L

    val receiverPlan = OneTimePlan("sms-sweep-now", RECEIVER_DELAY_SECONDS, ExistingWorkPolicy.APPEND_OR_REPLACE)

    /** The "Sync now" button: immediate, replacing any pending manual run. */
    val manualPlan = OneTimePlan("sms-sweep-manual", 0L, ExistingWorkPolicy.REPLACE)

    fun build(plan: OneTimePlan): OneTimeWorkRequest =
        OneTimeWorkRequestBuilder<UploadWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .apply { if (plan.initialDelaySeconds > 0) setInitialDelay(plan.initialDelaySeconds, TimeUnit.SECONDS) }
            .build()
}
