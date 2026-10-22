package com.madarwala.attendance

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/** A refusal from the server ("Wrong employee ID or PIN", "You are about 300 m from the office", ...). */
class ApiError(val code: Int, message: String) : Exception(message)

/** The attendance server's employee API (/api/app). Every call runs off the main thread. */
class Api(val context: Context) {
    private val prefs = context.getSharedPreferences("session", Context.MODE_PRIVATE)
    private val base = BuildConfig.SERVER

    var token: String?
        get() = prefs.getString("token", null)
        set(v) = prefs.edit().putString("token", v).apply()
    var name: String
        get() = prefs.getString("name", "") ?: ""
        set(v) = prefs.edit().putString("name", v).apply()
    var employeeId: String
        get() = prefs.getString("id", "") ?: ""
        set(v) = prefs.edit().putString("id", v).apply()

    fun logout() = prefs.edit().clear().apply()

    private fun open(path: String, method: String): HttpURLConnection {
        val c = URL(base + path).openConnection() as HttpURLConnection
        c.requestMethod = method
        c.connectTimeout = 15000
        c.readTimeout = 30000
        c.setRequestProperty("Accept", "application/json")
        token?.let { c.setRequestProperty("Authorization", "Bearer $it") }
        return c
    }

    private fun read(c: HttpURLConnection): String {
        val code = c.responseCode
        val body = (if (code < 400) c.inputStream else c.errorStream)?.bufferedReader()?.use { it.readText() } ?: ""
        if (code >= 400) {
            val msg = try {
                JSONObject(body).opt("detail").let { if (it is String) it else "Something went wrong ($code)" }
            } catch (e: Exception) {
                if (code == 502 || code == 503) "The server is not reachable right now" else "Something went wrong ($code)"
            }
            throw ApiError(code, msg)
        }
        return body
    }

    private suspend fun call(path: String, method: String = "GET", json: JSONObject? = null): String =
        withContext(Dispatchers.IO) {
            try {
                val c = open(path, method)
                if (json != null) {
                    c.doOutput = true
                    c.setRequestProperty("Content-Type", "application/json")
                    c.outputStream.use { it.write(json.toString().toByteArray()) }
                }
                read(c)
            } catch (e: ApiError) {
                throw e
            } catch (e: java.io.IOException) {
                throw ApiError(0, "No internet connection. Check your data or Wi-Fi and try again.")
            }
        }

    suspend fun getObj(path: String) = JSONObject(call(path))
    suspend fun getArr(path: String) = JSONArray(call(path))
    suspend fun post(path: String, json: JSONObject) = JSONObject(call(path, "POST", json))
    suspend fun delete(path: String) = JSONObject(call(path, "DELETE"))

    suspend fun login(id: String, pin: String) {
        val r = post("/api/app/login", JSONObject().put("employee_id", id.trim()).put("pin", pin.trim()))
        token = r.getString("token")
        name = r.getString("name")
        employeeId = id.trim().trimStart('0').ifEmpty { id.trim() }
    }

    /** Check in / out: location plus a selfie (JPEG). */
    suspend fun punch(lat: Double, lng: Double, accuracy: Float, selfie: ByteArray): JSONObject = withContext(Dispatchers.IO) {
        try {
            val boundary = "----att" + System.currentTimeMillis()
            val c = open("/api/app/punch", "POST")
            c.doOutput = true
            c.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
            c.outputStream.use { out ->
                fun field(n: String, v: String) =
                    out.write("--$boundary\r\nContent-Disposition: form-data; name=\"$n\"\r\n\r\n$v\r\n".toByteArray())
                field("lat", lat.toString())
                field("lng", lng.toString())
                field("accuracy", accuracy.toString())
                out.write(("--$boundary\r\nContent-Disposition: form-data; name=\"selfie\"; filename=\"selfie.jpg\"\r\n" +
                        "Content-Type: image/jpeg\r\n\r\n").toByteArray())
                out.write(selfie)
                out.write("\r\n--$boundary--\r\n".toByteArray())
            }
            JSONObject(read(c))
        } catch (e: ApiError) {
            throw e
        } catch (e: java.io.IOException) {
            throw ApiError(0, "No internet connection. Check your data or Wi-Fi and try again.")
        }
    }

    /** Download a payslip PDF into the cache, to open in a PDF viewer. */
    suspend fun payslip(month: String, dir: File): File = withContext(Dispatchers.IO) {
        val c = open("/api/app/payslip/$month.pdf", "GET")
        c.setRequestProperty("Accept", "application/pdf")
        if (c.responseCode >= 400) read(c)
        val f = File(dir, "payslip-$month.pdf")
        c.inputStream.use { inp -> f.outputStream().use { inp.copyTo(it) } }
        f
    }

    fun q(v: String): String = URLEncoder.encode(v, "UTF-8")
}
