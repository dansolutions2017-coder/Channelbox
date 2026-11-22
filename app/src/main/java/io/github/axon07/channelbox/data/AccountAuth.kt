package io.github.axon07.channelbox.data

import android.content.Context
import android.os.Build
import io.github.axon07.channelbox.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.UUID
import java.util.concurrent.TimeUnit

/**
 * Sign-in through your own login server (BuildConfig.AUTH_URL).
 *
 * The customer types only a username and password. The login server checks them against your
 * IPTV servers and answers with the server to use. The app keeps that as one Xtream playlist with
 * the fixed id [PLAYLIST_ID], and asks again before every reload, so a customer moved to another
 * server follows automatically. If the login server can't be reached, the saved server keeps working.
 *
 * When AUTH_URL is empty the app works exactly like before (any playlist, typed by hand).
 */
object AccountAuth {
    const val PLAYLIST_ID = "account"
    const val PLAYLIST_NAME = "My TV"

    val enabled: Boolean get() = BuildConfig.AUTH_URL.isNotBlank()

    sealed interface Result {
        data class Ok(val server: String, val expiresAt: String?, val maxConnections: Int?) : Result
        /** The login server said no (wrong password, expired, blocked…): sign the user out. */
        data class Rejected(val code: String, val message: String) : Result
        /** Couldn't ask (no internet, login server down): keep using the saved server. */
        data class Unavailable(val message: String) : Result
    }

    /** Why the user was signed out (shown on the sign-in screen), and the username to fill in. */
    val notice = MutableStateFlow<String?>(null)
    val lastUsername = MutableStateFlow("")

    /** Check the account with the login server at most this often (plus sign-in and manual Reload). */
    private const val CHECK_EVERY_MS = 6 * 60 * 60 * 1000L
    /** After the login server couldn't be reached, wait this long before trying again. */
    private const val RETRY_AFTER_FAIL_MS = 15 * 60 * 1000L
    /** Never ask twice within this window (e.g. sign-in immediately followed by the first load). */
    private const val JUST_CHECKED_MS = 60 * 1000L

    @Volatile private var appContext: Context? = null

    private fun prefs() = appContext?.getSharedPreferences("account", Context.MODE_PRIVATE)

    // Kept across app restarts, so opening the app doesn't trigger a check every time.
    private var lastOkAt: Long
        get() = prefs()?.getLong("last_ok_at", 0L) ?: 0L
        set(v) { prefs()?.edit()?.putLong("last_ok_at", v)?.apply() }
    private var lastFailAt: Long
        get() = prefs()?.getLong("last_fail_at", 0L) ?: 0L
        set(v) { prefs()?.edit()?.putLong("last_fail_at", v)?.apply() }

    fun init(context: Context) {
        appContext = context.applicationContext
    }

    /** A random id made once per install, so the login server can tell devices apart. */
    private fun deviceId(): String {
        val prefs = prefs() ?: return ""
        prefs.getString("device_id", null)?.let { return it }
        val id = UUID.randomUUID().toString()
        prefs.edit().putString("device_id", id).apply()
        return id
    }

    /**
     * Should the app ask the login server now? [force] = a manual Reload: asks unless it just did.
     * Otherwise at most every 6 hours, and not within 15 minutes of a failed attempt.
     * A clock that went backwards counts as "due".
     */
    fun shouldCheck(force: Boolean): Boolean {
        val now = System.currentTimeMillis()
        val sinceOk = now - lastOkAt
        if (sinceOk in 0 until JUST_CHECKED_MS) return false
        if (force) return true
        val sinceFail = now - lastFailAt
        if (sinceFail in 0 until RETRY_AFTER_FAIL_MS) return false
        return sinceOk !in 0 until CHECK_EVERY_MS
    }

    suspend fun login(http: OkHttpClient, username: String, password: String): Result = withContext(Dispatchers.IO) {
        val body = buildJsonObject {
            put("username", username.trim())
            put("password", password)
            put("device_id", deviceId())
            put("device_name", "${Build.MANUFACTURER} ${Build.MODEL}".trim())
            put("app_version", BuildConfig.VERSION_NAME)
        }.toString()
        val request = Request.Builder()
            .url(BuildConfig.AUTH_URL.trimEnd('/') + "/v1/login")
            .post(body.toRequestBody("application/json".toMediaType()))
            .build()
        val client = http.newBuilder().callTimeout(25, TimeUnit.SECONDS).build()
        try {
            client.newCall(request).execute().use { response ->
                val json = runCatching {
                    Json.parseToJsonElement(response.body?.string().orEmpty()) as? JsonObject
                }.getOrNull()
                val message = json?.str("message")
                when {
                    response.isSuccessful && json?.str("ok") == "true" -> {
                        val server = json?.str("server")
                            ?: return@withContext Result.Unavailable("The login server gave no server address")
                        lastOkAt = System.currentTimeMillis()
                        lastFailAt = 0L
                        Result.Ok(server, json.str("expires_at"), json.str("max_connections")?.toIntOrNull())
                    }
                    response.code == 400 || response.code == 401 || response.code == 403 -> {
                        lastOkAt = 0L
                        Result.Rejected(json?.str("error") ?: "rejected", message ?: "Wrong username or password.")
                    }
                    response.code == 429 -> {
                        lastFailAt = System.currentTimeMillis()
                        Result.Unavailable(message ?: "Too many attempts. Please wait a few minutes and try again.")
                    }
                    else -> {
                        lastFailAt = System.currentTimeMillis()
                        Result.Unavailable(message ?: "The login server isn't answering (HTTP ${response.code}).")
                    }
                }
            }
        } catch (e: IOException) {
            lastFailAt = System.currentTimeMillis()
            Result.Unavailable("Couldn't reach the login server. Check the internet connection and try again.")
        }
    }

    private fun JsonObject.str(key: String): String? {
        val v = this[key] as? JsonPrimitive ?: return null
        return if (v is JsonNull) null else v.content
    }
}
