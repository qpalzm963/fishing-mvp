package io.github.qpalzm963.fishingmvp

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.PixelFormat
import android.graphics.Point
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.util.DisplayMetrics
import android.util.Log
import android.view.Display
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Foreground service that owns the MediaProjection capture, the floating
 * control overlay and the single Python worker thread.
 *
 * Capture runs at a reduced resolution (Prefs.captureWidth) so the detector
 * stays fast during QTE; actions come back as normalized coordinates and are
 * mapped to the real screen size by [GestureDispatcher].
 */
class AutomationService : Service() {

    companion object {
        private const val TAG = "FishingService"
        const val ACTION_START_CAPTURE = "io.github.qpalzm963.fishingmvp.START_CAPTURE"
        const val ACTION_SHUTDOWN = "io.github.qpalzm963.fishingmvp.SHUTDOWN"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"
        private const val CHANNEL_ID = "automation"
        private const val NOTIFICATION_ID = 42
        private const val NO_FRAME_TIMEOUT_MS = 5000L

        /** No user-set round count: run until stopped, capped by the session's own 1–999 limit. */
        private const val MAX_ROUNDS = 999

        fun clock(ms: Long): String {
            val total = ms / 1000
            return String.format(Locale.US, "%d:%02d:%02d", total / 3600, (total / 60) % 60, total % 60)
        }

        /** One-line version of the statistics, shown while the panel is folded. */
        fun statsSummary(elapsedMs: Long, done: Int): String = "$done 輪 · ${clock(elapsedMs)}"

        /** Compact run statistics for the overlay panel. */
        fun statsText(elapsedMs: Long, done: Int, failed: Int, lastRoundMs: Long?): String {
            fun short(ms: Long): String {
                val seconds = ms / 1000.0
                return if (seconds < 60) String.format(Locale.US, "%.1f 秒", seconds)
                else String.format(Locale.US, "%d:%02d", ms / 60000, (ms / 1000) % 60)
            }
            return listOf(
                "執行時間  " + clock(elapsedMs),
                "完成輪數  $done",
                "平均每輪  " + if (done > 0) short(elapsedMs / done) else "—",
                "上一輪    " + (lastRoundMs?.let(::short) ?: "—"),
                "失手次數  $failed",
            ).joinToString("\n")
        }

        @Volatile
        var isRunning = false
            private set

        fun sessionsRoot(context: Context): File =
            File(context.getExternalFilesDir(null) ?: context.filesDir, "sessions")

        fun userConfig(context: Context): File =
            File(context.getExternalFilesDir(null) ?: context.filesDir, "config/user.yaml")
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private lateinit var python: ExecutorService
    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    @Volatile
    private var imageReader: ImageReader? = null
    private var imageThread: HandlerThread? = null
    private var overlay: OverlayController? = null
    private var captureWidth = 0
    private var captureHeight = 0

    @Volatile
    private var sessionActive = false

    @Volatile
    private var stopRequested = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        python = Executors.newSingleThreadExecutor { runnable -> Thread(runnable, "fishing-python") }
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_SHUTDOWN -> {
                shutdown()
                return START_NOT_STICKY
            }
            ACTION_START_CAPTURE -> startCapture(intent)
        }
        return START_NOT_STICKY
    }

    private fun startCapture(intent: Intent) {
        // Android 14+: the FGS must be in the foreground before getMediaProjection().
        val notification = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        if (projection != null) {
            overlay?.show()
            return
        }
        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED)
        val data: Intent? = if (Build.VERSION.SDK_INT >= 34) {
            intent.getParcelableExtra(EXTRA_RESULT_DATA, Intent::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra(EXTRA_RESULT_DATA)
        }
        if (resultCode != Activity.RESULT_OK || data == null) {
            Log.w(TAG, "screen capture permission missing")
            shutdown()
            return
        }
        val manager = getSystemService(MediaProjectionManager::class.java)
        val mp = manager.getMediaProjection(resultCode, data) ?: run { shutdown(); return }
        projection = mp
        mp.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                Log.i(TAG, "projection stopped by system/user")
                mainHandler.post { shutdown() }
            }
        }, mainHandler)

        val (screenW, screenH) = realScreenSize()
        val metrics = realMetrics()
        captureWidth = Prefs.captureWidth(this).coerceAtMost(screenW).let { it - it % 2 }
        captureHeight = (captureWidth.toLong() * screenH / screenW).toInt().let { it - it % 2 }
        val density = (metrics.densityDpi * captureWidth / screenW.toFloat()).toInt().coerceAtLeast(120)
        val thread = HandlerThread("fishing-capture").also { it.start() }
        imageThread = thread
        val reader = ImageReader.newInstance(captureWidth, captureHeight, PixelFormat.RGBA_8888, 3)
        imageReader = reader
        virtualDisplay = mp.createVirtualDisplay(
            "fishing-mvp-capture",
            captureWidth,
            captureHeight,
            density,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface,
            null,
            Handler(thread.looper),
        )
        isRunning = true
        overlay = OverlayController(this, ::startSession, ::requestStop, ::shutdown).also { it.show() }
        overlay?.setStatus("擷取 ${captureWidth}x$captureHeight（螢幕 ${screenW}x$screenH）\n載入偵測器中…")
        python.execute {
            try {
                Python.getInstance().getModule("fishing_mvp.mobile")
                mainHandler.post { overlay?.setStatus("就緒：先用試跑模式測試（不會點擊）") }
            } catch (exc: Throwable) {
                Log.e(TAG, "python import failed", exc)
                mainHandler.post { overlay?.setStatus("Python 載入失敗：${exc.message}") }
            }
        }
    }

    // ------------------------------------------------------------ session
    private fun startSession(live: Boolean): Boolean {
        if (sessionActive) return false
        val a11y = TapAccessibilityService.instance
        when {
            a11y == null -> { overlay?.setStatus("請先開啟無障礙服務（前景檢查與點擊都需要）"); return false }
            imageReader == null -> { overlay?.setStatus("尚未取得螢幕擷取權限"); return false }
        }
        // Manual trigger: the app in the foreground when Start is pressed becomes this session's
        // locked target (the overlay is a system window and is ignored by the lookup).
        val target = a11y!!.refreshForeground()
        if (target == null || a11y.isNonGamePackage(target)) {
            overlay?.setStatus("請先切到遊戲再打開開關\n目前前景：${target ?: "未知"}")
            return false
        }
        stopRequested = false
        sessionActive = true
        overlay?.setStats("")
        overlay?.setRunning(true)
        python.execute { runSession(target, MAX_ROUNDS, live) }
        return true
    }

    private fun requestStop() {
        stopRequested = true
        overlay?.setStatus("停止中…（不再送出新點擊）")
    }

    private fun runSession(target: String, rounds: Int, live: Boolean) {
        val (screenW, screenH) = realScreenSize()
        val stamp = SimpleDateFormat("yyyyMMdd-HHmmss-SSS", Locale.US).format(Date())
        val dir = File(sessionsRoot(this), stamp).apply { mkdirs() }
        val events = File(dir, "android_events.log")
        fun event(text: String) {
            val line = "${SimpleDateFormat("HH:mm:ss.SSS", Locale.US).format(Date())} $text\n"
            runCatching { events.appendText(line) }
            Log.i(TAG, text)
        }
        runCatching {
            File(dir, "android_session.json").writeText(
                JSONObject()
                    .put("package", target)
                    .put("live", live)
                    .put("rounds", rounds)
                    .put("capture_size", "${captureWidth}x$captureHeight")
                    .put("screen_size", "${screenW}x$screenH")
                    .put("device", "${Build.MANUFACTURER} ${Build.MODEL}")
                    .put("sdk", Build.VERSION.SDK_INT)
                    .put("min_tap_ms", GestureDispatcher.MIN_TAP_MS)
                    .put("auto_retry", Prefs.autoRetry(this))
                    .toString(2)
            )
        }
        event("session start live=$live rounds=$rounds package=$target")
        var summary: String? = null
        var session: PyObject? = null
        val startedMs = SystemClock.elapsedRealtime()
        var roundStartMs = startedMs
        var lastRoundMs: Long? = null
        var doneRounds = 0
        var failedAttempts = 0
        try {
            val dispatcher = if (live) GestureDispatcher(
                targetPackage = target,
                screenWidth = screenW,
                screenHeight = screenH,
                currentScreenSize = ::realScreenSize,
                overlayBounds = { overlay?.bounds },
                stopFlag = { stopRequested },
            ) else null
            val configFile = userConfig(this)
            val module = Python.getInstance().getModule("fishing_mvp.mobile")
            session = module.callAttr(
                "create_session",
                dir.absolutePath,
                rounds,
                live,
                target,
                if (configFile.isFile) configFile.absolutePath else null,
                dispatcher,
                Prefs.autoRetry(this),
            )
            session.callAttr("set_device_size", screenW, screenH)
            var buffer: ByteArray? = null
            var rowStride = 0
            var capturedAt: Double? = null
            var lastNewFrameMs = SystemClock.uptimeMillis()
            var kotlinStop: String? = null
            while (!stopRequested) {
                val loopStart = SystemClock.uptimeMillis()
                if (realScreenSize() != Pair(screenW, screenH)) {
                    kotlinStop = "screen_size_changed"
                    break
                }
                var reused = true
                imageReader?.acquireLatestImage()?.use { image ->
                    val plane = image.planes[0]
                    check(plane.pixelStride == 4) { "unexpected pixel stride ${plane.pixelStride}" }
                    val source = plane.buffer
                    if (buffer?.size != source.remaining()) buffer = ByteArray(source.remaining())
                    source.get(buffer!!)
                    rowStride = plane.rowStride
                    capturedAt = captureTimestampSeconds(image.timestamp)
                    reused = false
                    lastNewFrameMs = SystemClock.uptimeMillis()
                }
                // Static UIs (common after a failed cast) may stop producing
                // VirtualDisplay buffers. The last buffer still matches what is
                // on screen; restamp so AttemptRecovery's freshness gate can
                // accept a waiting/start screen instead of timing out.
                if (reused && buffer != null) {
                    capturedAt = System.nanoTime() / 1_000_000_000.0
                }
                val frame = buffer
                if (frame == null) {
                    // Only guards the first frame: a static screen legitimately produces no
                    // new frames, so later stalls are left to the Python stage timeouts.
                    if (SystemClock.uptimeMillis() - lastNewFrameMs > NO_FRAME_TIMEOUT_MS) {
                        kotlinStop = "no_frames"
                        break
                    }
                    Thread.sleep(10)
                    continue
                }
                val foreground = TapAccessibilityService.instance?.foregroundPackage(300)
                val result = JSONObject(
                    session.callAttr(
                        "process_rgba", frame, captureWidth, captureHeight, rowStride, capturedAt, reused, foreground,
                    ).toString()
                )
                postStatus(result, live, target)
                val done = result.optInt("completed_rounds")
                if (done > doneRounds) {
                    val now = SystemClock.elapsedRealtime()
                    lastRoundMs = now - roundStartMs
                    roundStartMs = now
                    doneRounds = done
                }
                // `attempt` advances on every success and every retry, so the surplus is failed attempts.
                failedAttempts = maxOf(failedAttempts, result.optInt("attempt") - 1 - done)
                val elapsed = SystemClock.elapsedRealtime() - startedMs
                val stats = statsText(elapsed, doneRounds, failedAttempts, lastRoundMs)
                val summary = statsSummary(elapsed, doneRounds)
                mainHandler.post { overlay?.setStats(stats, summary) }
                if (result.optBoolean("stopped")) break
                val intervalMs = (result.optDouble("next_interval_s", 0.1) * 1000).toLong()
                val remaining = loopStart + intervalMs - SystemClock.uptimeMillis()
                if (remaining > 0) Thread.sleep(remaining)
            }
            summary = session.callAttr("stop", kotlinStop ?: "user_stop").toString()
        } catch (exc: Throwable) {
            Log.e(TAG, "session failed", exc)
            event("session error: ${exc.javaClass.simpleName}: ${exc.message}")
            summary = runCatching { session?.callAttr("stop", "android_error")?.toString() }.getOrNull()
        } finally {
            val json = summary?.let { runCatching { JSONObject(it) }.getOrNull() }
            val reason = json?.optString("stop_reason") ?: "android_error"
            val done = json?.optInt("completed_rounds") ?: 0
            val err = json?.optJSONObject("error")?.optString("message")
            event("session stop reason=$reason completed=$done error=$err dir=${dir.absolutePath}")
            sessionActive = false
            val finalElapsed = SystemClock.elapsedRealtime() - startedMs
            val finalStats = statsText(finalElapsed, doneRounds, failedAttempts, lastRoundMs)
            val finalSummary = statsSummary(finalElapsed, doneRounds)
            mainHandler.post {
                overlay?.setRunning(false)
                overlay?.setStats(finalStats, finalSummary)
                overlay?.setStatus(
                    "已停止：${Labels.stopReason(reason)}（完成 $done 輪）" + (err?.let { "\n$it" } ?: "") + "\n紀錄：sessions/${dir.name}"
                )
            }
        }
    }

    private fun postStatus(result: JSONObject, live: Boolean, target: String) {
        val text = buildString {
            append(if (live) "正式" else "試跑")
            append(" · ").append(Labels.state(result.optString("state")))
            if (!result.isNull("analysis_ms")) append(" · ").append(result.optDouble("analysis_ms").toInt()).append("ms")
            val action = result.optJSONObject("action")
            if (action != null) append(if (result.optBoolean("sent")) "\n已點擊：" else "\n打算點：").append(action.optString("reason"))
            if (result.optBoolean("recovering")) append("\n重試復原中…（等待可辨識的開始畫面）")
            append("\n目標：").append(target)
        }
        mainHandler.post { overlay?.setStatus(text) }
    }

    /** Prefer the frame's own CLOCK_MONOTONIC timestamp when it is plausible. */
    private fun captureTimestampSeconds(imageTimestampNs: Long): Double {
        val now = System.nanoTime()
        val age = now - imageTimestampNs
        return if (imageTimestampNs > 0 && age in 0..2_000_000_000L) imageTimestampNs / 1e9 else now / 1e9
    }

    // ------------------------------------------------------------ screen
    private fun defaultDisplay(): Display =
        getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)

    @Suppress("DEPRECATION")
    fun realScreenSize(): Pair<Int, Int> {
        val point = Point()
        defaultDisplay().getRealSize(point)
        return Pair(point.x, point.y)
    }

    @Suppress("DEPRECATION")
    private fun realMetrics(): DisplayMetrics = DisplayMetrics().also { defaultDisplay().getRealMetrics(it) }

    // ------------------------------------------------------------ lifecycle
    private fun buildNotification(): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "自動化擷取", NotificationManager.IMPORTANCE_LOW)
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, AutomationService::class.java).setAction(ACTION_SHUTDOWN),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val open = PendingIntent.getActivity(
            this, 2, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle("Fishing MVP 擷取中")
            .setContentText("用懸浮視窗的開關控制；預設為試跑模式（不會點擊）")
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "結束", stop).build())
            .build()
    }

    private var shutDown = false

    private fun shutdown() {
        if (shutDown) return
        shutDown = true
        stopRequested = true
        overlay?.hide()
        overlay = null
        // The python thread may be reading an Image from the reader right now.
        // Release capture on that same thread, after the running session exits.
        val display = virtualDisplay
        val reader = imageReader
        val thread = imageThread
        val mp = projection
        virtualDisplay = null
        imageReader = null
        imageThread = null
        projection = null
        python.execute {
            display?.release()
            reader?.close()
            thread?.quitSafely()
            mp?.stop()
        }
        isRunning = false
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        if (isRunning) shutdown()
        python.shutdown()
        super.onDestroy()
    }
}
