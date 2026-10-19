package io.github.qpalzm963.fishingmvp

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityWindowInfo
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Input path without root: taps are injected with [dispatchGesture].
 *
 * The service also reports the package of the focused *application* window so
 * the automation only acts while the user-selected game is in the foreground.
 * Our own floating overlay is a system-type window and is ignored here.
 */
class TapAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "FishingA11y"

        @Volatile
        var instance: TapAccessibilityService? = null
            private set

        /** Last foreground app that is not this app; offered as a target suggestion. */
        @Volatile
        var lastExternalPackage: String? = null
            private set
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private val lock = Any()

    @Volatile
    private var cachedPackage: String? = null

    @Volatile
    private var cachedAtMs = 0L

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        refreshForeground()
        Log.i(TAG, "connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        when (event?.eventType) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED,
            AccessibilityEvent.TYPE_WINDOWS_CHANGED -> refreshForeground()
            else -> Unit
        }
    }

    override fun onInterrupt() = Unit

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    /** Foreground package, refreshed when the cached value is older than [maxAgeMs]. */
    fun foregroundPackage(maxAgeMs: Long = 300): String? {
        if (SystemClock.uptimeMillis() - cachedAtMs > maxAgeMs) refreshForeground()
        return cachedPackage
    }

    fun refreshForeground(): String? = synchronized(lock) {
        val pkg = try {
            resolveForeground()
        } catch (exc: Exception) {
            Log.w(TAG, "foreground lookup failed", exc)
            null
        }
        cachedPackage = pkg
        cachedAtMs = SystemClock.uptimeMillis()
        if (pkg != null && pkg != packageName) lastExternalPackage = pkg
        pkg
    }

    private fun resolveForeground(): String? {
        var fallback: String? = null
        for (window in windows) {
            if (window.type != AccessibilityWindowInfo.TYPE_APPLICATION) continue
            val pkg = window.root?.packageName?.toString() ?: continue
            if (window.isFocused || window.isActive) return pkg
            if (fallback == null) fallback = pkg
        }
        return rootInActiveWindow?.packageName?.toString() ?: fallback
    }

    /**
     * Inject a single tap at real-screen pixel coordinates and block until the
     * system reports completion.  Must not be called on the main thread.
     */
    fun tap(x: Float, y: Float, durationMs: Long, timeoutMs: Long = 1000): Boolean {
        check(Looper.myLooper() != Looper.getMainLooper()) { "tap() must not block the main thread" }
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs.coerceAtLeast(1))
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        val latch = CountDownLatch(1)
        var completed = false
        val accepted = dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) {
                completed = true
                latch.countDown()
            }

            override fun onCancelled(gestureDescription: GestureDescription?) {
                latch.countDown()
            }
        }, mainHandler)
        if (!accepted) return false
        if (!latch.await(timeoutMs, TimeUnit.MILLISECONDS)) return false
        return completed
    }
}
