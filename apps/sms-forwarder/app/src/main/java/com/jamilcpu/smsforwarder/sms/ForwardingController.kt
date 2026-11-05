package com.jamilcpu.smsforwarder.sms

import android.content.Context
import com.jamilcpu.smsforwarder.data.SettingsStore
import com.jamilcpu.smsforwarder.data.StoreSettings
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** The one lock that serialises upload runs with pairing changes. Shared by the worker and the app. */
object ForwardingLock {
    val mutex = Mutex()
}

/** Outlives any activity, so an unpair or re-pair that started cannot be cancelled half way. */
object AppScope {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
}

/**
 * Unpair and re-pair as one step with respect to upload runs. Work is cancelled first (so an in-flight
 * run releases the lock quickly), then under [lock] the queue is emptied and the settings replaced. A
 * run that was already waiting for the lock reads its settings after acquiring it, so it sees the new
 * pairing and the empty queue, never the old host or token.
 */
class ForwardingController(
    private val lock: Mutex,
    private val cancelWork: () -> Unit,
    private val clearQueue: suspend () -> Unit,
    private val saveSettings: suspend (StoreSettings) -> Unit,
    private val clearSettings: suspend () -> Unit,
    private val schedulePeriodic: () -> Unit,
) {
    suspend fun rePair(settings: StoreSettings) {
        cancelWork()
        lock.withLock {
            clearQueue()
            saveSettings(settings)
            schedulePeriodic()
        }
    }

    suspend fun unpair() {
        cancelWork()
        lock.withLock {
            clearQueue()
            clearSettings()
        }
    }

    companion object {
        fun forContext(context: Context, settings: SettingsStore): ForwardingController {
            val app = context.applicationContext
            val queue = RoomQueue(QueueDatabase.get(app).dao())
            return ForwardingController(
                lock = ForwardingLock.mutex,
                cancelWork = { UploadWorker.cancelAll(app) },
                clearQueue = queue::clear,
                saveSettings = settings::save,
                clearSettings = settings::clear,
                schedulePeriodic = { UploadWorker.schedulePeriodic(app) },
            )
        }
    }
}
