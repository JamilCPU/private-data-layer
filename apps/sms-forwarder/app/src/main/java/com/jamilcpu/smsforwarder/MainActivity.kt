package com.jamilcpu.smsforwarder

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.jamilcpu.smsforwarder.data.SettingsStore
import com.jamilcpu.smsforwarder.data.displayHost
import com.jamilcpu.smsforwarder.data.parsePairUri
import com.jamilcpu.smsforwarder.ui.StatusScreen
import com.jamilcpu.smsforwarder.ui.StatusState
import com.jamilcpu.smsforwarder.sms.QueueDatabase
import com.jamilcpu.smsforwarder.sms.RoomQueue
import com.jamilcpu.smsforwarder.sms.UploadWorker
import com.jamilcpu.smsforwarder.ui.SyncTrigger
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

private val REQUIRED_PERMISSIONS = arrayOf(
    Manifest.permission.RECEIVE_SMS,
    Manifest.permission.READ_SMS,
    Manifest.permission.READ_CONTACTS,
)

class MainActivity : ComponentActivity() {
    private lateinit var settings: SettingsStore
    private var state by mutableStateOf(StatusState())
    private val syncTrigger = SyncTrigger { UploadWorker.enqueueNow(this) }

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { refreshPermissions() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = SettingsStore(this)
        observeSettings()
        refreshPermissions()
        handlePairIntent(intent)
        setContent {
            MaterialTheme {
                Surface {
                    StatusScreen(
                        state = state,
                        onPair = ::pair,
                        onSync = { syncTrigger.syncNow() },
                        onUnpair = ::unpair,
                        onRequestPermissions = { permissionLauncher.launch(REQUIRED_PERMISSIONS) },
                    )
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handlePairIntent(intent)
    }

    private fun observeSettings() {
        lifecycleScope.launch {
            combine(settings.flow, settings.status) { s, status ->
                state.copy(
                    pairedHost = s?.displayHost(),
                    lastUploadAt = status.lastUploadAt,
                    lastResult = status.lastResult,
                    tokenValid = status.tokenValid,
                )
            }.collect {
                state = it.copy(queuedCount = queuedCount())
                if (it.pairedHost != null) UploadWorker.schedulePeriodic(this@MainActivity)
            }
        }
    }

    private suspend fun queuedCount(): Int =
        RoomQueue(QueueDatabase.get(this).dao()).pendingCount()

    private fun unpair() {
        UploadWorker.cancelAll(this)
        lifecycleScope.launch { settings.clear() }
    }

    private fun handlePairIntent(intent: Intent?) {
        val data = intent?.takeIf { it.action == Intent.ACTION_VIEW }?.dataString ?: return
        pair(data)
    }

    private fun pair(uri: String) {
        val parsed = parsePairUri(uri)
        if (parsed == null) {
            state = state.copy(pairError = "That is not a valid pairing link.")
            return
        }
        state = state.copy(pairError = null)
        lifecycleScope.launch { settings.save(parsed) }
    }

    private fun refreshPermissions() {
        val granted = REQUIRED_PERMISSIONS.all {
            ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
        }
        state = state.copy(permissionsGranted = granted)
    }
}
