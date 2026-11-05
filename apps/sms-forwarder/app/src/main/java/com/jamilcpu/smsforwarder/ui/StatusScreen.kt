package com.jamilcpu.smsforwarder.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.text.DateFormat
import java.util.Date

/** What the status screen shows. The token is deliberately not part of this. */
data class StatusState(
    val pairedHost: String? = null,
    val lastUploadAt: Long? = null,
    val lastResult: String? = null,
    val tokenValid: Boolean = true,
    val queuedCount: Int = 0,
    val permissionsGranted: Boolean = false,
    val pairError: String? = null,
)

/** Starts an immediate upload of queued messages. Wired to UploadWorker.enqueueNow. */
fun interface SyncTrigger {
    fun syncNow()
}

const val PERMISSION_RATIONALE =
    "SMS Forwarder reads incoming and existing text messages (RECEIVE_SMS, READ_SMS) so it can send " +
        "them to your own message store, and reads your contacts (READ_CONTACTS) to attach a name to " +
        "each number. Nothing is sent anywhere except the store you paired with."

@Composable
fun StatusScreen(
    state: StatusState,
    onPair: (String) -> Unit,
    onSync: () -> Unit,
    onUnpair: () -> Unit,
    onRequestPermissions: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("SMS Forwarder", style = MaterialTheme.typography.headlineSmall)
        StatusLines(state)
        if (!state.permissionsGranted) PermissionCard(onRequestPermissions)
        if (state.pairedHost == null) PairCard(state.pairError, onPair) else PairedActions(onSync, onUnpair)
    }
}

@Composable
private fun StatusLines(state: StatusState) {
    Text(state.pairedHost?.let { "Paired with $it" } ?: "Not paired")
    Text("Last upload: ${state.lastUploadAt?.let(::formatTime) ?: "never"}")
    Text("Last result: ${state.lastResult ?: "none"}")
    if (!state.tokenValid) Text("The store rejected the token. Pair again.", color = MaterialTheme.colorScheme.error)
    Text("Queued messages: ${state.queuedCount}")
}

@Composable
private fun PermissionCard(onRequest: () -> Unit) {
    Text(PERMISSION_RATIONALE, style = MaterialTheme.typography.bodyMedium)
    Button(onClick = onRequest) { Text("Grant permissions") }
}

@Composable
private fun PairCard(error: String?, onPair: (String) -> Unit) {
    var text by rememberSaveable { mutableStateOf("") }
    OutlinedTextField(
        value = text,
        onValueChange = { text = it },
        label = { Text("Pairing URI (pdl-sms://pair?...)") },
        modifier = Modifier.fillMaxWidth(),
    )
    if (error != null) Text(error, color = MaterialTheme.colorScheme.error)
    Button(onClick = { onPair(text) }, enabled = text.isNotBlank()) { Text("Pair") }
}

@Composable
private fun PairedActions(onSync: () -> Unit, onUnpair: () -> Unit) {
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Button(onClick = onSync) { Text("Sync now") }
        OutlinedButton(onClick = onUnpair) { Text("Unpair") }
    }
}

private fun formatTime(ms: Long): String =
    DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date(ms))
