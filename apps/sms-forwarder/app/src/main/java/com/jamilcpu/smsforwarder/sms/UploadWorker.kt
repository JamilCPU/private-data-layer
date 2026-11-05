package com.jamilcpu.smsforwarder.sms

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.jamilcpu.smsforwarder.data.OutgoingMessage
import com.jamilcpu.smsforwarder.data.SettingsStore
import com.jamilcpu.smsforwarder.data.UploadApi
import com.jamilcpu.smsforwarder.data.Watermark
import com.jamilcpu.smsforwarder.data.buildPinnedUploadClient
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.firstOrNull

class UploadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val settings = SettingsStore(applicationContext)
        val config = settings.flow.firstOrNull() ?: return Result.success()
        if (!settings.status.first().tokenValid) return Result.success()

        val client = buildPinnedUploadClient(config)
        try {
            val api = UploadApi(client, config)
            val runner = UploadRunner(
                queue = RoomQueue(QueueDatabase.get(applicationContext).dao()),
                uploader = { api.post(it) },
                state = SettingsRunnerState(settings),
                sweeper = ResolverSweeper(applicationContext),
                clock = System::currentTimeMillis,
            )
            return when (runner.run()) {
                RunOutcome.Retry -> Result.retry()
                RunOutcome.Done, RunOutcome.Unauthorized -> Result.success()
            }
        } finally {
            client.close()
        }
    }

    companion object {
        private const val PERIODIC_NAME = "sms-sweep"
        private const val NOW_NAME = "sms-sweep-now"
        private const val PERIOD_MINUTES = 15L

        private fun network() = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        /** Safe to call on every launch: KEEP leaves an existing schedule untouched. */
        fun schedulePeriodic(context: Context) {
            val request = PeriodicWorkRequestBuilder<UploadWorker>(PERIOD_MINUTES, TimeUnit.MINUTES)
                .setConstraints(network())
                .build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork(PERIODIC_NAME, ExistingPeriodicWorkPolicy.KEEP, request)
        }

        fun enqueueNow(context: Context) {
            val request = OneTimeWorkRequestBuilder<UploadWorker>().setConstraints(network()).build()
            WorkManager.getInstance(context)
                .enqueueUniqueWork(NOW_NAME, ExistingWorkPolicy.REPLACE, request)
        }

        fun cancelAll(context: Context) {
            val wm = WorkManager.getInstance(context)
            wm.cancelUniqueWork(PERIODIC_NAME)
            wm.cancelUniqueWork(NOW_NAME)
        }
    }
}

/** Sweeps the SMS provider and attaches contact names. */
class ResolverSweeper(private val context: Context) : Sweeper {
    override suspend fun sweep(after: Watermark): List<OutgoingMessage> {
        val resolver = context.contentResolver
        return SmsReader.readSince(resolver, after.dateMs, after.providerId).map {
            OutgoingMessage(
                providerId = it.id.toString(),
                dateMs = it.dateMs,
                address = it.address,
                contact = ContactResolver.name(resolver, it.address),
                direction = it.direction,
                body = it.body,
                threadId = it.threadId?.toString(),
            )
        }
    }
}

class SettingsRunnerState(private val settings: SettingsStore) : RunnerState {
    // A paired app always has a watermark; the fallback is "now" so history is never uploaded.
    override suspend fun watermark(): Watermark =
        settings.watermark() ?: Watermark(System.currentTimeMillis(), -1L).also { settings.setWatermark(it) }

    override suspend fun setWatermark(w: Watermark) = settings.setWatermark(w)

    override suspend fun recordSuccess(at: Long, result: String) = settings.recordUpload(at, result, true)

    override suspend fun recordFailure(result: String) = settings.recordFailure(result, true)

    override suspend fun recordUnauthorized() = settings.recordFailure("not paired", false)
}
