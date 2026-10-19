package io.github.qpalzm963.fishingmvp

import android.content.Context

/** Small persisted settings shared by the activity, overlay and service. */
object Prefs {
    private const val NAME = "fishing_mvp"
    private const val KEY_CAPTURE_WIDTH = "capture_width"
    private const val KEY_AUTO_RETRY = "auto_retry"
    private const val KEY_STATS_EXPANDED = "stats_expanded"

    /** Capture widths offered in the UI; smaller = lower QTE latency, less detail. */
    val CAPTURE_WIDTHS = intArrayOf(540, 720, 1080)
    const val DEFAULT_CAPTURE_WIDTH = 720

    private fun prefs(context: Context) = context.getSharedPreferences(NAME, Context.MODE_PRIVATE)

    fun captureWidth(context: Context): Int =
        prefs(context).getInt(KEY_CAPTURE_WIDTH, DEFAULT_CAPTURE_WIDTH)

    fun setCaptureWidth(context: Context, value: Int) {
        prefs(context).edit().putInt(KEY_CAPTURE_WIDTH, value).apply()
    }

    /** Retry a failed round on its own; on by default. */
    fun autoRetry(context: Context): Boolean = prefs(context).getBoolean(KEY_AUTO_RETRY, true)

    fun setAutoRetry(context: Context, value: Boolean) {
        prefs(context).edit().putBoolean(KEY_AUTO_RETRY, value).apply()
    }

    /** Whether the overlay's run statistics are unfolded. */
    fun statsExpanded(context: Context): Boolean = prefs(context).getBoolean(KEY_STATS_EXPANDED, true)

    fun setStatsExpanded(context: Context, value: Boolean) {
        prefs(context).edit().putBoolean(KEY_STATS_EXPANDED, value).apply()
    }
}
