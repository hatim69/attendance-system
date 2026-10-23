package com.madarwala.attendance

import android.annotation.SuppressLint
import android.app.DatePickerDialog
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.location.Location
import android.location.LocationManager
import android.net.Uri
import android.os.CancellationSignal
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.location.LocationManagerCompat
import androidx.exifinterface.media.ExifInterface
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.Calendar
import kotlin.coroutines.resume

/** The phone camera app, asked to open the front camera (most phones honour one of these hints). */
class TakeSelfie : ActivityResultContracts.TakePicture() {
    override fun createIntent(context: Context, input: Uri): Intent = super.createIntent(context, input).apply {
        putExtra("android.intent.extras.CAMERA_FACING", 1)
        putExtra("android.intent.extras.LENS_FACING_FRONT", 1)
        putExtra("android.intent.extra.USE_FRONT_CAMERA", true)
    }
}

fun selfieFile(context: Context): File = File(context.cacheDir, "selfie.jpg")

/** The photo shrunk to at most 1280 px, upright, as a JPEG small enough to upload quickly. */
fun selfieBytes(file: File): ByteArray? {
    if (!file.exists() || file.length() == 0L) return null
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeFile(file.path, bounds)
    var sample = 1
    while (maxOf(bounds.outWidth, bounds.outHeight) / sample > 1280) sample *= 2
    val bmp = BitmapFactory.decodeFile(file.path, BitmapFactory.Options().apply { inSampleSize = sample }) ?: return null
    val turn = when (ExifInterface(file.path).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)) {
        ExifInterface.ORIENTATION_ROTATE_90 -> 90f
        ExifInterface.ORIENTATION_ROTATE_180 -> 180f
        ExifInterface.ORIENTATION_ROTATE_270 -> 270f
        else -> 0f
    }
    val upright = if (turn == 0f) bmp else Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, Matrix().apply { postRotate(turn) }, true)
    val out = ByteArrayOutputStream()
    upright.compress(Bitmap.CompressFormat.JPEG, 80, out)
    return out.toByteArray()
}

fun hasLocationPermission(context: Context) =
    ContextCompat.checkSelfPermission(context, android.Manifest.permission.ACCESS_FINE_LOCATION) == android.content.pm.PackageManager.PERMISSION_GRANTED ||
        ContextCompat.checkSelfPermission(context, android.Manifest.permission.ACCESS_COARSE_LOCATION) == android.content.pm.PackageManager.PERMISSION_GRANTED

fun locationOn(context: Context): Boolean {
    val lm = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
    return LocationManagerCompat.isLocationEnabled(lm)
}

/** A fresh location fix from GPS or the network (whichever answers first), or null after 25 seconds. */
@SuppressLint("MissingPermission")
suspend fun currentLocation(context: Context): Location? {
    val lm = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
    val providers = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER).filter {
        try { lm.isProviderEnabled(it) } catch (e: Exception) { false }
    }
    if (providers.isEmpty()) return null
    val fresh = withTimeoutOrNull(25_000) {
        suspendCancellableCoroutine<Location?> { cont ->
            val signal = CancellationSignal()
            var answered = 0
            for (p in providers) {
                LocationManagerCompat.getCurrentLocation(lm, p, signal, ContextCompat.getMainExecutor(context)) { loc ->
                    answered++
                    if (cont.isActive && (loc != null || answered == providers.size)) cont.resume(loc)
                }
            }
            cont.invokeOnCancellation { signal.cancel() }
        }
    }
    if (fresh != null) return fresh
    // no fix in time: a recent last-known position (at most 2 minutes old) is still good enough
    return providers.mapNotNull { try { lm.getLastKnownLocation(it) } catch (e: Exception) { null } }
        .filter { System.currentTimeMillis() - it.time < 120_000 }
        .minByOrNull { it.accuracy }
}

/** The phone's date picker; returns YYYY-MM-DD. */
fun pickDate(context: Context, current: String, onPicked: (String) -> Unit) {
    val c = Calendar.getInstance()
    if (current.length == 10) c.set(current.substring(0, 4).toInt(), current.substring(5, 7).toInt() - 1, current.substring(8, 10).toInt())
    DatePickerDialog(context, { _, y, m, d -> onPicked(String.format(java.util.Locale.ENGLISH, "%04d-%02d-%02d", y, m + 1, d)) },
        c.get(Calendar.YEAR), c.get(Calendar.MONTH), c.get(Calendar.DAY_OF_MONTH)).show()
}

fun todayIso(): String {
    val c = Calendar.getInstance()
    return String.format(java.util.Locale.ENGLISH, "%04d-%02d-%02d", c.get(Calendar.YEAR), c.get(Calendar.MONTH) + 1, c.get(Calendar.DAY_OF_MONTH))
}
