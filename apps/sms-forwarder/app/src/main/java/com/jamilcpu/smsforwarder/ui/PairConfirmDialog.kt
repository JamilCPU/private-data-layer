package com.jamilcpu.smsforwarder.ui

import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import com.jamilcpu.smsforwarder.data.PendingPairing

@Composable
fun PairConfirmDialog(pending: PendingPairing, onConfirm: () -> Unit, onCancel: () -> Unit, busy: Boolean = false) {
    AlertDialog(
        onDismissRequest = { if (!busy) onCancel() },
        title = { Text("Confirm pairing") },
        text = { Text(pending.message) },
        confirmButton = { TextButton(onClick = onConfirm, enabled = !busy) { Text(if (busy) "Pairing…" else "Pair") } },
        dismissButton = { TextButton(onClick = onCancel, enabled = !busy) { Text("Cancel") } },
    )
}
