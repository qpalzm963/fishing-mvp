package io.github.qpalzm963.fishingmvp

import android.graphics.Rect

/**
 * Called from Python (`fishing_mvp.mobile.MobileSession`) through Chaquopy.
 *
 * Every tap re-checks: accessibility connected, foreground == target package,
 * screen size unchanged, coordinate inside the screen and outside our overlay.
 * Any violation throws, which stops the session before an input is sent.
 */
class GestureDispatcher(
    private val targetPackage: String,
    private val screenWidth: Int,
    private val screenHeight: Int,
    private val currentScreenSize: () -> Pair<Int, Int>,
    private val overlayBounds: () -> Rect?,
    private val stopFlag: () -> Boolean,
) {
    companion object {
        /** dispatchGesture needs a positive duration; 0ms config maps to this. */
        const val MIN_TAP_MS = 10L
        const val INPUT_PATH = "accessibility_gesture"
    }

    fun stopRequested(): Boolean = stopFlag()

    fun dispatchTap(xNorm: Double, yNorm: Double, holdMs: Int): String {
        if (stopFlag()) throw IllegalStateException("stop requested")
        require(xNorm in 0.0..1.0 && yNorm in 0.0..1.0) { "normalized coordinate out of range: $xNorm,$yNorm" }
        val service = TapAccessibilityService.instance
            ?: throw IllegalStateException("accessibility service is not connected")
        val foreground = service.foregroundPackage(300)
        if (foreground != targetPackage) {
            throw IllegalStateException("foreground package changed: expected=$targetPackage foreground=$foreground")
        }
        val size = currentScreenSize()
        if (size != Pair(screenWidth, screenHeight)) {
            throw IllegalStateException("screen size changed: ${screenWidth}x$screenHeight -> ${size.first}x${size.second}")
        }
        val x = (xNorm * screenWidth).coerceIn(0.0, screenWidth - 1.0).toFloat()
        val y = (yNorm * screenHeight).coerceIn(0.0, screenHeight - 1.0).toFloat()
        overlayBounds()?.let { bounds ->
            if (bounds.contains(x.toInt(), y.toInt())) {
                throw IllegalStateException("tap ($x,$y) would land on the control overlay; move the overlay")
            }
        }
        val duration = if (holdMs > 0) holdMs.toLong() else MIN_TAP_MS
        if (!service.tap(x, y, duration)) throw IllegalStateException("gesture rejected or cancelled at ($x,$y)")
        return INPUT_PATH
    }
}
