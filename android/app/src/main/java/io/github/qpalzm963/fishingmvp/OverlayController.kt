package io.github.qpalzm963.fishingmvp

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Rect
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
 * Small draggable floating panel: rounds (1–999), Dry-run/LIVE switch,
 * Start/Stop and a status line.  The window is not focusable so it never
 * steals input focus from the game, and taps inside its bounds are refused.
 * LIVE resets to off after every session.
 */
@SuppressLint("SetTextI18n", "UseSwitchCompatOrMaterialCode")
class OverlayController(
    private val context: Context,
    private val onStart: (rounds: Int, live: Boolean) -> Unit,
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

    private var rounds = Prefs.rounds(context)
    private var shown = false
    private val root: LinearLayout
    private val roundsView: TextView
    private val liveSwitch: Switch
    private val startButton: Button
    private val stopButton: Button
    private val statusView: TextView

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

        val roundsRow = row()
        roundsRow.addView(label("輪數 "))
        roundsView = label(rounds.toString(), 15f).apply {
            minWidth = px(36)
            gravity = Gravity.CENTER
        }
        val minus = button("−") { setRounds(rounds - 1) }.apply {
            setOnLongClickListener { setRounds(rounds - 10); true }
        }
        val plus = button("+") { setRounds(rounds + 1) }.apply {
            setOnLongClickListener { setRounds(rounds + 10); true }
        }
        roundsRow.addView(minus)
        roundsRow.addView(roundsView)
        roundsRow.addView(plus)
        root.addView(roundsRow)

        liveSwitch = Switch(context).apply {
            text = "實機點擊 LIVE"
            setTextColor(Color.LTGRAY)
            isChecked = false
            setOnCheckedChangeListener { _, checked ->
                setTextColor(if (checked) Color.rgb(255, 90, 90) else Color.LTGRAY)
            }
        }
        root.addView(liveSwitch)

        val controls = row()
        startButton = button("開始") {
            Prefs.setRounds(context, rounds)
            onStart(rounds, liveSwitch.isChecked)
        }
        stopButton = button("停止") { onStop() }.apply { isEnabled = false }
        controls.addView(startButton)
        controls.addView(stopButton)
        root.addView(controls)

        statusView = label("", 12f).apply { maxWidth = px(220) }
        root.addView(statusView)

        handle.setOnTouchListener(DragListener())
        root.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> updateBounds() }
    }

    private fun setRounds(value: Int) {
        rounds = value.coerceIn(1, 999)
        roundsView.text = rounds.toString()
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
        startButton.isEnabled = !running
        stopButton.isEnabled = running
        liveSwitch.isEnabled = !running
        if (!running) liveSwitch.isChecked = false // back to dry-run after every session
    }

    fun setStatus(text: String) {
        statusView.text = text
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
