package com.madarwala.attendance

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/** Reminders without any cloud service: about every 15 minutes the phone asks the attendance server what to show
 * (check-in / check-out reminders, HR's decisions on leave and work from home) and shows each item once. */
object Reminders {
    private const val CHANNEL = "reminders"
    private const val WORK = "reminders"

    fun start(context: Context) {
        val nm = context.getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Attendance reminders", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Check-in and check-out reminders, and replies to your leave requests"
            })
        }
        val req = PeriodicWorkRequestBuilder<CheckWorker>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.KEEP, req)
    }

    fun stop(context: Context) = WorkManager.getInstance(context).cancelUniqueWork(WORK)

    fun allowed(context: Context) = Build.VERSION.SDK_INT < 33 ||
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    /** Ask the server and show anything new. */
    suspend fun check(context: Context) {
        val api = Api(context)
        if (api.token == null || !allowed(context)) return
        val prefs = context.getSharedPreferences("shown", Context.MODE_PRIVATE)
        val items = api.getArr("/api/app/notifications")
        val today = todayIso()
        for (i in 0 until items.length()) {
            val n = items.getJSONObject(i)
            val key = n.getString("key")
            if (prefs.contains(key)) continue
            show(context, key.hashCode(), n.getString("title"), n.getString("body"))
            prefs.edit().putString(key, today).apply()
        }
        // forget keys older than a week
        val old = prefs.all.filter { (it.value as? String ?: "") < weekAgo() }.keys
        if (old.isNotEmpty()) prefs.edit().apply { old.forEach { remove(it) } }.apply()
    }

    private fun weekAgo(): String {
        val c = java.util.Calendar.getInstance(); c.add(java.util.Calendar.DAY_OF_MONTH, -7)
        return String.format(java.util.Locale.ENGLISH, "%04d-%02d-%02d", c.get(java.util.Calendar.YEAR), c.get(java.util.Calendar.MONTH) + 1, c.get(java.util.Calendar.DAY_OF_MONTH))
    }

    @android.annotation.SuppressLint("MissingPermission")
    private fun show(context: Context, id: Int, title: String, body: String) {
        val open = PendingIntent.getActivity(context, 0, Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP), PendingIntent.FLAG_IMMUTABLE)
        val n = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(context).notify(id, n)
    }
}

class CheckWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        try {
            Reminders.check(applicationContext)
        } catch (e: ApiError) {
            if (e.code == 401) return Result.success()   // logged out: nothing to show
        } catch (e: Exception) {
            return Result.retry()
        }
        return Result.success()
    }
}
