package io.github.qpalzm963.fishingmvp

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.graphics.drawable.Drawable
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.LayerDrawable
import android.graphics.drawable.RippleDrawable
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject

/** White text with a thick dark outline, like the game's banner lettering. */
private class OutlinedTextView(context: Context) : TextView(context) {
    var outlineColor = Color.BLACK
    var outlineWidthPx = 0f

    override fun onDraw(canvas: Canvas) {
        val textLayout = layout
        if (outlineWidthPx > 0f && textLayout != null) {
            canvas.save()
            canvas.translate(compoundPaddingLeft.toFloat(), extendedPaddingTop.toFloat())
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = outlineWidthPx
            paint.strokeJoin = Paint.Join.ROUND
            paint.color = outlineColor
            textLayout.draw(canvas)
            paint.style = Paint.Style.FILL
            paint.color = currentTextColor
            canvas.restore()
        }
        super.onDraw(canvas)
    }
}

/**
 * Setup wizard, one gated step at a time: overlay permission, accessibility
 * service, capture width, then the MediaProjection consent that starts the
 * floating controls.  A step cannot be passed until its requirement is met, and
 * a permission that is revoked later sends the user back to its step.
 */
@SuppressLint("SetTextI18n")
class MainActivity : Activity() {

    companion object {
        private const val REQUEST_CAPTURE = 1001
        private const val REQUEST_NOTIFICATIONS = 1002

        private const val STEP_COUNT = 4
        private const val LAST_STEP = STEP_COUNT - 1
        private val STEP_NAMES = listOf("懸浮視窗", "無障礙服務", "擷取設定", "開始")

        // Palette taken by eye from the game's UI: teal water, indigo outlines, candy buttons.
        private val SKY_TOP = Color.parseColor("#47B5D3")
        private val SKY_BOTTOM = Color.parseColor("#1F6A8C")
        private val OUTLINE = Color.parseColor("#2B2D5B")
        private val PANEL = Color.parseColor("#FFF6DD")
        private val INK = Color.parseColor("#2B2D5B")
        private val MUTED = Color.parseColor("#6B6F9A")
        private val ORANGE = Color.parseColor("#FFB020")
        private val ORANGE_EDGE = Color.parseColor("#E0780F")
        private val BLUE = Color.parseColor("#5B6BC0")
        private val BLUE_EDGE = Color.parseColor("#3B4690")
        private val RED = Color.parseColor("#E8453C")
        private val RED_EDGE = Color.parseColor("#B22A25")
        private val LOCKED = Color.parseColor("#B9BCD6")
        private val LOCKED_EDGE = Color.parseColor("#9498B8")
        private val OK = Color.parseColor("#1E9E54")
        private val BAD = Color.parseColor("#D8343A")
        private val WARN = Color.parseColor("#C77700")
        private val CODE_BG = Color.parseColor("#F0E6C8")
        private const val MATCH = ViewGroup.LayoutParams.MATCH_PARENT
        private const val WRAP = ViewGroup.LayoutParams.WRAP_CONTENT
    }

    private enum class Kind { PRIMARY, SECONDARY, DANGER }

    private val handler = Handler(Looper.getMainLooper())
    private val poll = object : Runnable {
        override fun run() {
            refresh()
            handler.postDelayed(this, 1000)
        }
    }

    private lateinit var panels: List<View>
    private lateinit var dots: List<TextView>
    private lateinit var connectors: List<View>
    private lateinit var prevButton: Button
    private lateinit var nextButton: Button
    private lateinit var gateHint: TextView
    private lateinit var overlayStatus: TextView
    private lateinit var a11yStatus: TextView
    private lateinit var serviceStatus: TextView

    private var shownStep = 0
    private var lastOverlayOk = false
    private var lastA11yOk = false

    private fun px(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun rounded(fill: Int, stroke: Int? = null, radius: Int = 12, strokeDp: Int = 3) =
        GradientDrawable().apply {
            setColor(fill)
            cornerRadius = px(radius).toFloat()
            if (stroke != null) setStroke(px(strokeDp), stroke)
        }

    /** A chunky 3D slab: darker edge layer peeking out below the face. */
    private fun chunky(face: Int, edge: Int, radius: Int, depthDp: Int = 4): Drawable =
        LayerDrawable(arrayOf(rounded(edge, OUTLINE, radius), rounded(face, OUTLINE, radius))).apply {
            setLayerInset(1, 0, 0, 0, px(depthDp))
        }

    private fun text(value: String, size: Float = 14f, color: Int = INK, bold: Boolean = false) =
        TextView(this).apply {
            text = value
            textSize = size
            setTextColor(color)
            setPadding(0, px(4), 0, px(4))
            if (bold) setTypeface(typeface, Typeface.BOLD)
        }

    private fun outlined(value: String, size: Float, widthDp: Int = 4) = OutlinedTextView(this).apply {
        text = value
        textSize = size
        setTextColor(Color.WHITE)
        setTypeface(typeface, Typeface.BOLD)
        outlineColor = OUTLINE
        outlineWidthPx = px(widthDp).toFloat()
        gravity = Gravity.CENTER
        setPadding(px(widthDp), px(2), px(widthDp), px(2))
    }

    private fun styleButton(button: Button, kind: Kind, enabled: Boolean = true) {
        val (face, edge, ink) = when {
            !enabled -> Triple(LOCKED, LOCKED_EDGE, MUTED)
            kind == Kind.PRIMARY -> Triple(ORANGE, ORANGE_EDGE, INK)
            kind == Kind.DANGER -> Triple(RED, RED_EDGE, Color.WHITE)
            else -> Triple(BLUE, BLUE_EDGE, Color.WHITE)
        }
        button.isEnabled = enabled
        button.setTextColor(ink)
        button.background = RippleDrawable(
            ColorStateList.valueOf(0x33FFFFFF), chunky(face, edge, 16), rounded(Color.WHITE, radius = 16),
        )
    }

    private fun button(value: String, kind: Kind, click: () -> Unit) = Button(this).apply {
        text = value
        setAllCaps(false)
        textSize = 16f
        setTypeface(typeface, Typeface.BOLD)
        stateListAnimator = null
        minHeight = px(52)
        minimumHeight = px(52)
        setPadding(px(16), px(8), px(16), px(12))
        layoutParams = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = px(10) }
        setOnClickListener { click() }
        styleButton(this, kind)
    }

    private fun statusLine() = text("", 15f, MUTED, bold = true)

    /** One wizard page: a title, an explanation, then whatever the caller adds. */
    private fun page(index: Int, title: String, hint: String): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(text("${index + 1}　$title", 20f, INK, bold = true))
            addView(text(hint, 14f, MUTED).apply { setPadding(0, px(2), 0, px(8)) })
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(px(16), px(12), px(16), px(24))
        }

        // Header banner
        val banner = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            background = chunky(RED, RED_EDGE, 20, depthDp = 6)
            setPadding(px(16), px(14), px(16), px(22))
        }
        banner.addView(outlined("Fishing MVP", 30f, widthDp = 5))
        banner.addView(text("Android 原型 · 一步一步設定", 13f, Color.WHITE).apply { gravity = Gravity.CENTER })
        column.addView(banner, LinearLayout.LayoutParams(MATCH, WRAP).apply { bottomMargin = px(14) })

        // Progress dots joined by connectors
        val dotViews = mutableListOf<TextView>()
        val connectorViews = mutableListOf<View>()
        val progress = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        repeat(STEP_COUNT) { i ->
            val dot = TextView(this).apply {
                gravity = Gravity.CENTER
                textSize = 15f
                setTypeface(typeface, Typeface.BOLD)
                layoutParams = LinearLayout.LayoutParams(px(38), px(38))
            }
            dotViews += dot
            progress.addView(dot)
            if (i < LAST_STEP) {
                val line = View(this).apply {
                    layoutParams = LinearLayout.LayoutParams(0, px(5), 1f).apply {
                        leftMargin = px(4)
                        rightMargin = px(4)
                    }
                }
                connectorViews += line
                progress.addView(line)
            }
        }
        dots = dotViews
        connectors = connectorViews
        column.addView(progress, LinearLayout.LayoutParams(MATCH, WRAP).apply { bottomMargin = px(14) })

        // Step 1: overlay permission
        val overlayPage = page(0, STEP_NAMES[0], "控制視窗要浮在遊戲畫面上面，所以需要這個權限。")
        overlayStatus = statusLine()
        overlayPage.addView(overlayStatus)
        overlayPage.addView(button("前往設定", Kind.PRIMARY) {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        })

        // Step 2: accessibility service
        val a11yPage = page(
            1, STEP_NAMES[1],
            "用來確認目前前景是哪個 App，並在正式模式時送出點擊。不會讀取或上傳畫面以外的內容。",
        )
        a11yStatus = statusLine()
        a11yPage.addView(a11yStatus)
        a11yPage.addView(button("前往無障礙設定", Kind.PRIMARY) {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        })
        a11yPage.addView(text("在清單找到「Fishing MVP 點擊服務」並啟用。Android 13 以上若顯示「受限設定」，先到 App 資訊 → ⋮ → 允許受限設定。", 12f, MUTED))

        // Step 3: capture width (+ optional self test)
        val widthPage = page(2, STEP_NAMES[2], "擷取寬度越小，反應越快，但畫面細節越少。")
        val group = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL }
        val current = Prefs.captureWidth(this)
        Prefs.CAPTURE_WIDTHS.forEach { value ->
            group.addView(RadioButton(this).apply {
                id = View.generateViewId()
                text = "${value}px"
                textSize = 16f
                setTextColor(INK)
                buttonTintList = ColorStateList.valueOf(ORANGE_EDGE)
                isChecked = value == current
                setPadding(px(4), px(8), px(16), px(8))
                setOnCheckedChangeListener { _, checked -> if (checked) Prefs.setCaptureWidth(this@MainActivity, value) }
            })
        }
        widthPage.addView(group)
        widthPage.addView(text("進階：偵測器自我測試", 15f, INK, bold = true).apply { setPadding(0, px(14), 0, 0) })
        widthPage.addView(text("不需要遊戲；確認 Python／OpenCV 能在這支手機上運作。", 12f, MUTED))
        var selfTestRaw = ""
        val selfTestOutput = text("", 12f, INK).apply {
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
            background = rounded(CODE_BG, radius = 8, strokeDp = 0)
            setPadding(px(10), px(8), px(10), px(8))
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = px(10) }
        }
        val copyButton = button("複製完整結果", Kind.SECONDARY) {
            val clipboard = getSystemService(ClipboardManager::class.java)
            clipboard.setPrimaryClip(ClipData.newPlainText("self test", selfTestRaw))
            Toast.makeText(this, "已複製完整結果", Toast.LENGTH_SHORT).show()
        }.apply { visibility = View.GONE }
        widthPage.addView(button("執行自我測試", Kind.SECONDARY) {
            selfTestOutput.visibility = View.VISIBLE
            copyButton.visibility = View.GONE
            selfTestOutput.text = "執行中…（第一次載入 OpenCV 需要幾秒）"
            Thread {
                val result = runCatching { SelfTest.run(applicationContext) }
                    .getOrElse { "失敗：${it.javaClass.simpleName}: ${it.message}" }
                runOnUiThread {
                    selfTestRaw = result
                    selfTestOutput.text = summarizeSelfTest(result)
                    copyButton.visibility = View.VISIBLE
                }
            }.start()
        })
        widthPage.addView(selfTestOutput)
        widthPage.addView(copyButton)

        // Step 4: start
        val startPage = page(3, STEP_NAMES[3], "權限都齊了。授權螢幕擷取後會出現懸浮控制視窗。")
        serviceStatus = statusLine()
        startPage.addView(serviceStatus)
        startPage.addView(text("① 按下面的按鈕授權擷取\n② 切到遊戲\n③ 在懸浮視窗打開「自動釣魚」開關，會以當下前景的 App 為目標", 14f, INK))
        startPage.addView(
            text("先用「試跑模式」：只看畫面、顯示打算點哪裡，不會真的點。確認判斷正確後，再到懸浮視窗打開「正式模式」才會真的點擊；每次停止後會自動回到試跑。", 13f, OK, bold = true)
                .apply { setPadding(0, px(8), 0, px(4)) },
        )
        startPage.addView(button("授權螢幕擷取並顯示懸浮視窗", Kind.PRIMARY) { requestCapture() })
        startPage.addView(button("結束擷取服務", Kind.DANGER) {
            startService(Intent(this, AutomationService::class.java).setAction(AutomationService.ACTION_SHUTDOWN))
        })

        // Cream panel holding whichever step is shown
        val allPanels = listOf(overlayPage, a11yPage, widthPage, startPage)
        panels = allPanels
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(PANEL, OUTLINE, 22, strokeDp = 4)
            setPadding(px(18), px(16), px(18), px(18))
        }
        allPanels.forEach { card.addView(it) }
        column.addView(card, LinearLayout.LayoutParams(MATCH, WRAP).apply { bottomMargin = px(14) })

        // Back / Next
        val nav = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        prevButton = button("上一步", Kind.SECONDARY) { goTo(shownStep - 1) }
        nextButton = button("下一步", Kind.PRIMARY) { goTo(shownStep + 1) }
        nav.addView(prevButton, LinearLayout.LayoutParams(0, WRAP, 1f).apply { rightMargin = px(6) })
        nav.addView(nextButton, LinearLayout.LayoutParams(0, WRAP, 1f).apply { leftMargin = px(6) })
        column.addView(nav)
        gateHint = text("", 13f, Color.WHITE, bold = true).apply { gravity = Gravity.CENTER }
        column.addView(gateHint)

        setContentView(
            ScrollView(this).apply {
                background = GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM, intArrayOf(SKY_TOP, SKY_BOTTOM))
                clipToPadding = false
                isFillViewport = true
                addView(column)
                // targetSdk 35 is edge-to-edge: keep content clear of the status and gesture bars.
                setOnApplyWindowInsetsListener { v, insets ->
                    @Suppress("DEPRECATION")
                    v.setPadding(0, insets.systemWindowInsetTop, 0, insets.systemWindowInsetBottom)
                    insets
                }
            },
        )

        lastOverlayOk = Settings.canDrawOverlays(this)
        lastA11yOk = TapAccessibilityService.instance != null
        shownStep = if (AutomationService.isRunning) LAST_STEP else minOf(2, unlockedStep())

        if (intent?.getBooleanExtra("run_self_test", false) == true) {
            // adb shell am start -n <pkg>/.MainActivity --ez run_self_test true
            Thread { runCatching { SelfTest.run(applicationContext) } }.start()
        }

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFICATIONS)
        }
        refresh()
    }

    override fun onResume() {
        super.onResume()
        handler.post(poll)
    }

    override fun onPause() {
        handler.removeCallbacks(poll)
        super.onPause()
    }

    /** Furthest step the user may be on given the current permissions. */
    private fun unlockedStep(): Int = when {
        !Settings.canDrawOverlays(this) -> 0
        TapAccessibilityService.instance == null -> 1
        else -> LAST_STEP
    }

    private fun goTo(step: Int) {
        shownStep = step.coerceIn(0, unlockedStep())
        render()
    }

    private fun setStatus(view: TextView, color: Int, message: String) {
        view.text = message
        view.setTextColor(color)
    }

    private fun refresh() {
        val overlayOk = Settings.canDrawOverlays(this)
        val a11yConnected = TapAccessibilityService.instance != null
        setStatus(
            overlayStatus,
            if (overlayOk) OK else BAD,
            if (overlayOk) "✓ 懸浮視窗權限已允許" else "✗ 尚未允許懸浮視窗",
        )
        when {
            a11yConnected -> setStatus(a11yStatus, OK, "✓ 無障礙點擊服務已連線")
            isAccessibilityEnabled() -> setStatus(a11yStatus, WARN, "… 已啟用，等待連線")
            else -> setStatus(a11yStatus, BAD, "✗ 尚未啟用無障礙點擊服務")
        }
        if (AutomationService.isRunning) {
            setStatus(serviceStatus, OK, "● 擷取服務執行中")
        } else {
            setStatus(serviceStatus, MUTED, "○ 擷取服務未啟動")
        }

        // Move on by itself the moment the current step's requirement is met.
        if (overlayOk && !lastOverlayOk && shownStep == 0) shownStep = 1
        if (a11yConnected && !lastA11yOk && shownStep == 1) shownStep = 2
        lastOverlayOk = overlayOk
        lastA11yOk = a11yConnected
        // A permission revoked later sends the user back to its step.
        shownStep = shownStep.coerceAtMost(unlockedStep())
        render()
    }

    private fun render() {
        val unlocked = unlockedStep()
        panels.forEachIndexed { i, panel -> panel.visibility = if (i == shownStep) View.VISIBLE else View.GONE }
        dots.forEachIndexed { i, dot ->
            val (face, label) = when {
                i < shownStep -> OK to "✓"
                i == shownStep -> ORANGE to "${i + 1}"
                else -> LOCKED to "${i + 1}"
            }
            dot.text = label
            dot.setTextColor(if (i == shownStep) INK else Color.WHITE)
            dot.background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(face)
                setStroke(px(3), OUTLINE)
            }
        }
        connectors.forEachIndexed { i, line ->
            line.background = rounded(if (i < shownStep) OK else LOCKED, OUTLINE, 3, strokeDp = 1)
        }
        prevButton.visibility = if (shownStep > 0) View.VISIBLE else View.INVISIBLE
        styleButton(prevButton, Kind.SECONDARY)
        nextButton.visibility = if (shownStep < LAST_STEP) View.VISIBLE else View.INVISIBLE
        val canAdvance = shownStep < unlocked
        styleButton(nextButton, Kind.PRIMARY, enabled = canAdvance)
        gateHint.text = when {
            shownStep < LAST_STEP && !canAdvance && shownStep == 0 -> "先允許懸浮視窗才能繼續"
            shownStep < LAST_STEP && !canAdvance && shownStep == 1 -> "先啟用無障礙服務才能繼續"
            else -> ""
        }
    }

    /** Compact view of the self-test JSON; the raw report stays available through the copy button. */
    private fun summarizeSelfTest(raw: String): String {
        val report = runCatching { JSONObject(raw) }.getOrNull() ?: return raw.take(300)
        val bridge = report.optJSONObject("bridge")
        val bridgeOk = report.optBoolean("bridge_ok")
        val allOk = report.optBoolean("ok") && bridgeOk
        return buildString {
            append(if (allOk) "✓ 全部通過" else "✗ 有項目失敗")
            append("（%.1f 秒）\n".format(report.optLong("self_test_ms") / 1000.0))
            append("Python ${report.optString("python")} · numpy ${report.optString("numpy")}\n")
            append("OpenCV ${report.optString("opencv")} · ${report.optString("machine")}\n\n")
            val frames = report.optJSONArray("frames")
            for (i in 0 until (frames?.length() ?: 0)) {
                val frame = frames!!.getJSONObject(i)
                val name = frame.optString("name")
                val label = Labels.state(name).padEnd(4, '　')
                val ok = frame.optString("hint") == name
                append(label).append("  ").append(if (ok) "✓ " else "✗ ")
                when {
                    frame.has("error") -> append(frame.optString("error").take(60))
                    ok -> append("${frame.optDouble("analysis_ms_max").toInt()} ms")
                    else -> append("判成 ${frame.optString("hint")}")
                }
                append('\n')
            }
            append("影像橋接".padEnd(7)).append(if (bridgeOk) "✓ " else "✗ ")
            if (bridge != null) append("${bridge.optInt("frames")} 幀 ${bridge.optLong("total_ms")} ms")
        }
    }

    private fun isAccessibilityEnabled(): Boolean {
        val expected = ComponentName(this, TapAccessibilityService::class.java).flattenToString()
        val enabled = Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        return enabled.split(':').any { it.equals(expected, ignoreCase = true) }
    }

    private fun requestCapture() {
        // The wizard already gates on this, but a permission can be revoked while the page is open.
        val problem = when {
            !Settings.canDrawOverlays(this) -> "請先允許懸浮視窗"
            TapAccessibilityService.instance == null -> "請先啟用無障礙點擊服務"
            else -> null
        }
        if (problem != null) {
            Toast.makeText(this, problem, Toast.LENGTH_LONG).show()
            return
        }
        val manager = getSystemService(MediaProjectionManager::class.java)
        @Suppress("DEPRECATION")
        startActivityForResult(manager.createScreenCaptureIntent(), REQUEST_CAPTURE)
    }

    @Deprecated("Activity result API kept framework-only to avoid AndroidX dependencies")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_CAPTURE) return
        if (resultCode != RESULT_OK || data == null) {
            Toast.makeText(this, "未取得螢幕擷取權限", Toast.LENGTH_LONG).show()
            return
        }
        val intent = Intent(this, AutomationService::class.java)
            .setAction(AutomationService.ACTION_START_CAPTURE)
            .putExtra(AutomationService.EXTRA_RESULT_CODE, resultCode)
            .putExtra(AutomationService.EXTRA_RESULT_DATA, data)
        startForegroundService(intent)
        Toast.makeText(this, "已啟動；請切到遊戲，先用試跑模式測試", Toast.LENGTH_LONG).show()
        moveTaskToBack(true)
    }
}
