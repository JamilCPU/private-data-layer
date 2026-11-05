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
import com.jamilcpu.smsforwarder.data.SettingsStore
import com.jamilcpu.smsforwarder.data.UploadApi
import com.jamilcpu.smsforwarder.data.Watermark
import com.jamilcpu.smsforwarder.data.buildPinnedUploadClient
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.firstOrNull
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

class UploadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result =
        // One run at a time in this process, and pairing changes take the same lock. The settings are
        // read inside it, so a run queued behind an unpair or re-pair sees the new state.
        ForwardingLock.mutex.withLock { runLocked() }

    private suspend fun runLocked(): Result {
        val settings = SettingsStore(applicationContext)
        val config = settings.flow.firstOrNull() ?: return Result.success()
        if (!settings.status.first().tokenValid) return Result.success()

        val client = buildPinnedUploadClient(config)
        try {
            val api = UploadApi(client, config)
            val queue = RoomQueue(QueueDatabase.get(applicationContext).dao())
            val runner = UploadRunner(
                queue = queue,
                uploader = { api.post(it) },
                state = SettingsRunnerState(settings),
                sweeper = resolverSweeper(applicationContext, queue),
                clock = System::currentTimeMillis,
            )
            return when (runner.run()) {
                RunOutcome.Retry -> Result.retry()
                RunOutcome.Done, RunOutcome.Unauthorized, RunOutcome.MissingPermission -> Result.success()
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

/** Look-back sweep over the SMS provider; provider and contact queries run on the IO dispatcher. */
fun resolverSweeper(context: Context, queue: Queue): Sweeper {
    val resolver = context.contentResolver
    return LookbackSweeper(
        pages = { since, afterId -> withContext(Dispatchers.IO) { SmsReader.readSince(resolver, since, afterId) } },
        queue = queue,
        contact = { address -> withContext(Dispatchers.IO) { ContactResolver.name(resolver, address) } },
    )
}

class SettingsRunnerState(private val settings: SettingsStore) : RunnerState {
    // A paired app always has a watermark; the fallback is "now" so history is never uploaded.
    override suspend fun watermark(): Watermark =
        settings.watermark() ?: System.currentTimeMillis().let { Watermark(it, -1L, it) }.also { settings.setWatermark(it) }

    override suspend fun setWatermark(w: Watermark) = settings.setWatermark(w)

    override suspend fun recordSuccess(at: Long, result: String) = settings.recordUpload(at, result, true)

    override suspend fun recordFailure(result: String) = settings.recordFailure(result, true)

    override suspend fun recordUnauthorized() = settings.recordFailure("not paired", false)
}
