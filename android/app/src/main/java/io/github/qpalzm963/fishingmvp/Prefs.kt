package io.github.qpalzm963.fishingmvp

import android.content.Context

/** Small persisted settings shared by the activity, overlay and service. */
object Prefs {
    private const val NAME = "fishing_mvp"
    private const val KEY_PACKAGE = "target_package"
    private const val KEY_CAPTURE_WIDTH = "capture_width"
    private const val KEY_ROUNDS = "rounds"

    /** Capture widths offered in the UI; smaller = lower QTE latency, less detail. */
    val CAPTURE_WIDTHS = intArrayOf(540, 720, 1080)
    const val DEFAULT_CAPTURE_WIDTH = 720

    private fun prefs(context: Context) = context.getSharedPreferences(NAME, Context.MODE_PRIVATE)

    fun targetPackage(context: Context): String =
        prefs(context).getString(KEY_PACKAGE, "")?.trim().orEmpty()

    fun setTargetPackage(context: Context, value: String) {
        prefs(context).edit().putString(KEY_PACKAGE, value.trim()).apply()
    }

    fun captureWidth(context: Context): Int =
        prefs(context).getInt(KEY_CAPTURE_WIDTH, DEFAULT_CAPTURE_WIDTH)

    fun setCaptureWidth(context: Context, value: Int) {
        prefs(context).edit().putInt(KEY_CAPTURE_WIDTH, value).apply()
    }

    fun rounds(context: Context): Int = prefs(context).getInt(KEY_ROUNDS, 1).coerceIn(1, 999)

    fun setRounds(context: Context, value: Int) {
        prefs(context).edit().putInt(KEY_ROUNDS, value.coerceIn(1, 999)).apply()
    }
}
