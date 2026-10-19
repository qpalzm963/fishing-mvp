package io.github.qpalzm963.fishingmvp

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Rect
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.Switch
import android.widget.TextView

/**
 * Small draggable floating panel: a run switch (on = start, off = stop), the
 * Dry-run/LIVE switch and a status line.  The window is not focusable so it never
 * steals input focus from the game, and taps inside its bounds are refused.
 * LIVE resets to off after every session.
 */
@SuppressLint("SetTextI18n", "UseSwitchCompatOrMaterialCode")
class OverlayController(
    private val context: Context,
    /** Returns false when the session could not start, so the run switch snaps back off. */
    private val onStart: (live: Boolean) -> Boolean,
    private val onStop: () -> Unit,
    private val onClose: () -> Unit,
) {
    private val windowManager = context.getSystemService(WindowManager::class.java)
    private val params = WindowManager.LayoutParams(
        WindowManager.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
        PixelFormat.TRANSLUCENT,
    ).apply {
        gravity = Gravity.TOP or Gravity.START
        x = 24
        y = 160
    }

    @Volatile
    var bounds: Rect? = null
        private set

    private var shown = false
    private var settingRunning = false
    private val root: LinearLayout
    private val runSwitch: Switch
    private val liveSwitch: Switch
    private val statusView: TextView
    private val statsView: TextView

    init {
        val dp = context.resources.displayMetrics.density
        fun px(v: Int) = (v * dp).toInt()
        root = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(px(8), px(6), px(8), px(6))
            background = GradientDrawable().apply {
                setColor(Color.argb(215, 20, 24, 32))
                cornerRadius = px(10).toFloat()
            }
        }
        fun label(text: String, size: Float = 13f) = TextView(context).apply {
            this.text = text
            setTextColor(Color.WHITE)
            textSize = size
        }
        fun button(text: String, click: () -> Unit) = Button(context).apply {
            this.text = text
            textSize = 13f
            minWidth = 0
            minimumWidth = 0
            minHeight = 0
            minimumHeight = 0
            setPadding(px(10), px(4), px(10), px(4))
            setOnClickListener { click() }
        }
        fun row() = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }

        val header = row()
        val handle = label("≡ Fishing MVP", 14f).apply { setPadding(0, 0, px(12), 0) }
        header.addView(handle)
        header.addView(button("✕") { onClose() })
        root.addView(header)

        liveSwitch = Switch(context).apply {
            text = "試跑模式：只看不點"
            setTextColor(Color.LTGRAY)
            isChecked = false
            setOnCheckedChangeListener { _, checked ->
                text = if (checked) "正式模式：會真的點擊" else "試跑模式：只看不點"
                setTextColor(if (checked) Color.rgb(255, 90, 90) else Color.LTGRAY)
            }
        }
        root.addView(liveSwitch)
        // The run switch sits above LIVE and reads it at the moment it is switched on.
        runSwitch = Switch(context).apply {
            text = "自動釣魚"
            setTextColor(Color.WHITE)
            isChecked = false
            setOnCheckedChangeListener { _, checked ->
                if (settingRunning) return@setOnCheckedChangeListener
                if (checked) {
                    if (!onStart(liveSwitch.isChecked)) setRunning(false)
                } else {
                    onStop()
                }
            }
        }
        root.addView(runSwitch, root.indexOfChild(liveSwitch))

        statusView = label("", 12f).apply { maxWidth = px(220) }
        root.addView(statusView)

        statsView = label("", 12f).apply {
            typeface = Typeface.MONOSPACE
            setTextColor(Color.rgb(190, 225, 255))
            setPadding(0, px(6), 0, 0)
            visibility = View.GONE
        }
        root.addView(statsView)

        handle.setOnTouchListener(DragListener())
        root.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> updateBounds() }
    }

    private fun updateBounds() {
        if (!shown) {
            bounds = null
            return
        }
        val location = IntArray(2)
        root.getLocationOnScreen(location)
        // A small margin so near-miss gestures cannot hit the panel either.
        val margin = (8 * context.resources.displayMetrics.density).toInt()
        bounds = Rect(
            location[0] - margin,
            location[1] - margin,
            location[0] + root.width + margin,
            location[1] + root.height + margin,
        )
    }

    fun show() {
        if (shown) return
        windowManager.addView(root, params)
        shown = true
    }

    fun hide() {
        if (!shown) return
        shown = false
        bounds = null
        runCatching { windowManager.removeView(root) }
    }

    fun setRunning(running: Boolean) {
        settingRunning = true
        runSwitch.isChecked = running
        settingRunning = false
        liveSwitch.isEnabled = !running
        if (!running) liveSwitch.isChecked = false // back to dry-run after every session
    }

    fun setStatus(text: String) {
        statusView.text = text
    }

    /** Run statistics block; an empty string hides it. */
    fun setStats(text: String) {
        statsView.text = text
        statsView.visibility = if (text.isEmpty()) View.GONE else View.VISIBLE
    }

    private inner class DragListener : View.OnTouchListener {
        private var startX = 0
        private var startY = 0
        private var touchX = 0f
        private var touchY = 0f

        @SuppressLint("ClickableViewAccessibility")
        override fun onTouch(v: View, event: MotionEvent): Boolean {
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    startX = params.x
                    startY = params.y
                    touchX = event.rawX
                    touchY = event.rawY
                }
                MotionEvent.ACTION_MOVE -> {
                    params.x = startX + (event.rawX - touchX).toInt()
                    params.y = startY + (event.rawY - touchY).toInt()
                    if (shown) windowManager.updateViewLayout(root, params)
                }
                MotionEvent.ACTION_UP -> root.post { updateBounds() }
            }
            return true
        }
    }
}
