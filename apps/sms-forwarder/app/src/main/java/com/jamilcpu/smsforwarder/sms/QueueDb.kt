package com.jamilcpu.smsforwarder.sms

import android.content.Context
import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import com.jamilcpu.smsforwarder.data.OutgoingMessage

const val STATE_PENDING = "pending"
const val STATE_SENT = "sent"
const val STATE_FAILED = "failed"

@Entity(tableName = "queued_messages")
data class QueuedMessage(
    @PrimaryKey @ColumnInfo(name = "provider_id") val providerId: String,
    @ColumnInfo(name = "date_ms") val dateMs: Long,
    val address: String,
    val contact: String?,
    val direction: String,
    /** Nulled once sent: dedupe needs only the key. */
    val body: String?,
    @ColumnInfo(name = "thread_id") val threadId: String?,
    val state: String = STATE_PENDING,
    val attempts: Int = 0,
)

@Dao
interface QueueDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertAll(items: List<QueuedMessage>)

    @Query(
        "SELECT * FROM queued_messages WHERE state = 'pending' " +
            "ORDER BY date_ms ASC, " +
            "CAST(substr(provider_id, 1, instr(provider_id, '-') - 1) AS INTEGER) ASC LIMIT :limit",
    )
    suspend fun pending(limit: Int): List<QueuedMessage>

    @Query("UPDATE queued_messages SET state = 'sent', body = NULL WHERE provider_id IN (:ids)")
    suspend fun markSent(ids: List<String>)

    @Query("UPDATE queued_messages SET state = 'failed', attempts = attempts + 1 WHERE provider_id IN (:ids)")
    suspend fun markFailed(ids: List<String>)

    @Query("SELECT COUNT(*) FROM queued_messages WHERE state = 'pending'")
    suspend fun pendingCount(): Int

    @Query("SELECT provider_id FROM queued_messages WHERE provider_id IN (:ids)")
    suspend fun known(ids: List<String>): List<String>

    @Query("DELETE FROM queued_messages")
    suspend fun clear()
}

@Database(entities = [QueuedMessage::class], version = 2, exportSchema = false)
abstract class QueueDatabase : RoomDatabase() {
    abstract fun dao(): QueueDao

    companion object {
        @Volatile private var instance: QueueDatabase? = null

        fun get(context: Context): QueueDatabase =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    QueueDatabase::class.java,
                    "sms_queue.db",
                ).fallbackToDestructiveMigration().build().also { instance = it }
            }
    }
}

class RoomQueue(private val dao: QueueDao) : Queue {
    override suspend fun enqueueAll(items: List<OutgoingMessage>) =
        dao.insertAll(items.map { it.toEntity() })

    override suspend fun nextBatch(limit: Int): List<OutgoingMessage> =
        dao.pending(limit).map { it.toOutgoing() }

    override suspend fun markSent(ids: List<String>) = ids.chunked(SQL_CHUNK).forEach { dao.markSent(it) }

    override suspend fun markFailed(ids: List<String>) = ids.chunked(SQL_CHUNK).forEach { dao.markFailed(it) }

    override suspend fun pendingCount(): Int = dao.pendingCount()

    override suspend fun known(ids: List<String>): Set<String> =
        ids.chunked(SQL_CHUNK).flatMap { dao.known(it) }.toSet()

    override suspend fun clear() = dao.clear()

    private companion object {
        const val SQL_CHUNK = 500
    }
}

private fun OutgoingMessage.toEntity() =
    QueuedMessage(providerId, dateMs, address, contact, direction, body, threadId)

private fun QueuedMessage.toOutgoing() =
    OutgoingMessage(providerId, dateMs, address, contact, direction, body.orEmpty(), threadId)
