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
import com.jamilcpu.smsforwarder.data.PairingDecision
import com.jamilcpu.smsforwarder.data.PendingPairing
import com.jamilcpu.smsforwarder.data.SettingsStore
import com.jamilcpu.smsforwarder.data.displayHost
import com.jamilcpu.smsforwarder.data.parsePairUri
import com.jamilcpu.smsforwarder.ui.PairConfirmDialog
import com.jamilcpu.smsforwarder.ui.StatusScreen
import com.jamilcpu.smsforwarder.ui.StatusState
import com.jamilcpu.smsforwarder.sms.AppScope
import com.jamilcpu.smsforwarder.sms.ForwardingController
import com.jamilcpu.smsforwarder.sms.QueueDatabase
import com.jamilcpu.smsforwarder.sms.RoomQueue
import com.jamilcpu.smsforwarder.sms.UploadWorker
import com.jamilcpu.smsforwarder.ui.SyncTrigger
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

private val REQUIRED_PERMISSIONS = arrayOf(
    Manifest.permission.RECEIVE_SMS,
    Manifest.permission.READ_SMS,
    Manifest.permission.READ_CONTACTS,
)

class MainActivity : ComponentActivity() {
    private lateinit var settings: SettingsStore
    private var state by mutableStateOf(StatusState())
    private var pairing by mutableStateOf(false)
    private lateinit var controller: ForwardingController
    private var pending by mutableStateOf<PendingPairing?>(null)
    private val syncTrigger = SyncTrigger { UploadWorker.enqueueNow(this) }

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { refreshPermissions() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = SettingsStore(this)
        controller = ForwardingController.forContext(this, settings)
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
                    pending?.let { p ->
                        PairConfirmDialog(
                            pending = p,
                            onConfirm = { confirmPairing(p) },
                            busy = pairing,
                            onCancel = { pending = null },
                        )
                    }
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
        AppScope.scope.launch { controller.unpair() }
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
        lifecycleScope.launch {
            pending = PairingDecision.fromIntent(settings.flow.first(), parsed)
        }
    }

    private fun confirmPairing(p: PendingPairing) {
        if (pairing) return
        pairing = true
        // Application scope: the save must not be lost if the activity goes away meanwhile.
        AppScope.scope.launch {
            try {
                controller.rePair(p.incoming)
            } finally {
                pairing = false
                pending = null
            }
        }
    }

    private fun refreshPermissions() {
        val granted = REQUIRED_PERMISSIONS.all {
            ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
        }
        state = state.copy(permissionsGranted = granted)
    }
}
