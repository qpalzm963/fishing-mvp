package io.github.qpalzm963.fishingmvp

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
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

/**
 * Setup screen: permission checklist, target game package, capture width and
 * the button that asks for MediaProjection consent and starts the overlay.
 */
@SuppressLint("SetTextI18n")
class MainActivity : Activity() {

    companion object {
        private const val REQUEST_CAPTURE = 1001
        private const val REQUEST_NOTIFICATIONS = 1002

        private val BG = Color.parseColor("#F2F4F7")
        private val CARD = Color.WHITE
        private val INK = Color.parseColor("#1F2933")
        private val MUTED = Color.parseColor("#616E7C")
        private val ACCENT = Color.parseColor("#1A73E8")
        private val LINE = Color.parseColor("#D5DAE0")
        private val OK = Color.parseColor("#1E8E3E")
        private val BAD = Color.parseColor("#D93025")
        private val WARN = Color.parseColor("#B06000")
        private val CODE_BG = Color.parseColor("#F2F4F7")
        private const val MATCH = ViewGroup.LayoutParams.MATCH_PARENT
        private const val WRAP = ViewGroup.LayoutParams.WRAP_CONTENT
    }

    private lateinit var overlayStatus: TextView
    private lateinit var a11yStatus: TextView
    private lateinit var serviceStatus: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val dp = resources.displayMetrics.density
        fun px(v: Int) = (v * dp).toInt()
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(px(16), px(12), px(16), px(24))
        }
        fun rounded(fill: Int, stroke: Int? = null, radius: Int = 12) = GradientDrawable().apply {
            setColor(fill)
            cornerRadius = px(radius).toFloat()
            if (stroke != null) setStroke(px(1), stroke)
        }
        fun text(value: String, size: Float = 14f, color: Int = INK) = TextView(this).apply {
            text = value
            textSize = size
            setTextColor(color)
            setPadding(0, px(4), 0, px(4))
        }
        fun button(value: String, primary: Boolean, click: () -> Unit) = Button(this).apply {
            text = value
            setAllCaps(false) // package names are case-sensitive; never upper-case them
            textSize = 15f
            stateListAnimator = null
            minHeight = px(48)
            minimumHeight = px(48)
            setTextColor(if (primary) Color.WHITE else ACCENT)
            val base = if (primary) rounded(ACCENT) else rounded(CARD, LINE)
            background = RippleDrawable(ColorStateList.valueOf(0x22000000), base, null)
            layoutParams = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = px(8) }
            setOnClickListener { click() }
        }
        fun card(title: String, hint: String? = null): LinearLayout {
            val inner = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                background = rounded(CARD)
                elevation = px(1).toFloat()
                setPadding(px(16), px(14), px(16), px(16))
            }
            inner.addView(text(title, 17f).apply { setTypeface(typeface, Typeface.BOLD) })
            if (hint != null) inner.addView(text(hint, 12f, MUTED))
            column.addView(inner, LinearLayout.LayoutParams(MATCH, WRAP).apply { bottomMargin = px(12) })
            return inner
        }
        fun statusLine() = text("", 14f).apply { setTypeface(typeface, Typeface.BOLD) }

        column.addView(
            text(
                "在手機上直接執行 fishing-mvp 偵測：MediaProjection 擷取畫面、無障礙服務送出點擊。",
                13f, MUTED,
            ).apply { setPadding(px(4), 0, px(4), px(10)) },
        )

        val safety = card("預設 Dry-run（安全模式）")
        safety.background = rounded(Color.parseColor("#E6F4EA"))
        safety.elevation = 0f
        safety.addView(text("只記錄提案，不會點擊。要在懸浮視窗明確打開 LIVE 才會實際點擊，且每輪結束後自動關回。", 13f, OK))

        val perms = card("1　權限")
        overlayStatus = statusLine()
        perms.addView(overlayStatus)
        perms.addView(button("開啟「顯示在其他應用程式上層」", primary = false) {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        })
        a11yStatus = statusLine().apply { setPadding(0, px(14), 0, px(4)) }
        perms.addView(a11yStatus)
        perms.addView(button("開啟無障礙設定", primary = false) {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        })
        perms.addView(text("在清單中找到「Fishing MVP 點擊服務」並啟用。", 12f, MUTED))

        val width = card("2　擷取寬度", "越小 QTE 延遲越低，但細節越少")
        val group = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL }
        val current = Prefs.captureWidth(this)
        Prefs.CAPTURE_WIDTHS.forEach { value ->
            group.addView(RadioButton(this).apply {
                id = View.generateViewId()
                text = "${value}px"
                isChecked = value == current
                setPadding(px(4), px(8), px(16), px(8))
                setOnCheckedChangeListener { _, checked -> if (checked) Prefs.setCaptureWidth(this@MainActivity, value) }
            })
        }
        width.addView(group)

        val start = card("3　開始", "先授權擷取，切到遊戲後在懸浮視窗按「開始」；會以當下前景的 App 為目標")
        serviceStatus = statusLine()
        start.addView(serviceStatus)
        start.addView(button("授權螢幕擷取並顯示懸浮視窗", primary = true) { requestCapture() })
        start.addView(button("結束擷取服務", primary = false) {
            startService(Intent(this, AutomationService::class.java).setAction(AutomationService.ACTION_SHUTDOWN))
        })

        val test = card("偵測器自我測試", "不需要遊戲；確認 Python／OpenCV 能在這支手機上運作")
        val selfTestOutput = text("", 11f, INK).apply {
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
            background = rounded(CODE_BG, radius = 8)
            setPadding(px(10), px(8), px(10), px(8))
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = px(10) }
        }
        test.addView(button("執行自我測試", primary = false) {
            selfTestOutput.visibility = View.VISIBLE
            selfTestOutput.text = "執行中…（第一次載入 OpenCV 需要幾秒）"
            Thread {
                val result = runCatching { SelfTest.run(applicationContext) }
                    .getOrElse { "失敗：${it.javaClass.simpleName}: ${it.message}" }
                runOnUiThread { selfTestOutput.text = result }
            }.start()
        })
        test.addView(selfTestOutput)

        column.addView(
            text(
                "紀錄位置：${AutomationService.sessionsRoot(this).absolutePath}\n" +
                    "自訂設定（可選）：${AutomationService.userConfig(this).absolutePath}",
                11f, MUTED,
            ).apply { setPadding(px(4), px(4), px(4), 0) },
        )

        setContentView(
            ScrollView(this).apply {
                setBackgroundColor(BG)
                clipToPadding = false
                addView(column)
                // targetSdk 35 is edge-to-edge: content starts under the status bar and action bar.
                // The top inset already includes the action bar; the bottom keeps the last card
                // clear of the gesture bar.
                setOnApplyWindowInsetsListener { v, insets ->
                    @Suppress("DEPRECATION")
                    v.setPadding(0, insets.systemWindowInsetTop, 0, insets.systemWindowInsetBottom)
                    insets
                }
            },
        )

        if (intent?.getBooleanExtra("run_self_test", false) == true) {
            // adb shell am start -n <pkg>/.MainActivity --ez run_self_test true
            Thread { runCatching { SelfTest.run(applicationContext) } }.start()
        }

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFICATIONS)
        }
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun setStatus(view: TextView, color: Int, message: String) {
        view.text = message
        view.setTextColor(color)
    }

    private fun refresh() {
        if (Settings.canDrawOverlays(this)) {
            setStatus(overlayStatus, OK, "✓ 懸浮視窗權限已允許")
        } else {
            setStatus(overlayStatus, BAD, "✗ 尚未允許懸浮視窗")
        }
        when {
            TapAccessibilityService.instance != null -> setStatus(a11yStatus, OK, "✓ 無障礙點擊服務已連線")
            isAccessibilityEnabled() -> setStatus(a11yStatus, WARN, "… 無障礙服務已啟用，等待連線")
            else -> setStatus(a11yStatus, BAD, "✗ 尚未啟用無障礙點擊服務")
        }
        if (AutomationService.isRunning) {
            setStatus(serviceStatus, OK, "● 擷取服務執行中")
        } else {
            setStatus(serviceStatus, MUTED, "○ 擷取服務未啟動")
        }
    }

    private fun isAccessibilityEnabled(): Boolean {
        val expected = ComponentName(this, TapAccessibilityService::class.java).flattenToString()
        val enabled = Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        return enabled.split(':').any { it.equals(expected, ignoreCase = true) }
    }

    private fun requestCapture() {
        val problem = when {
            !Settings.canDrawOverlays(this) -> "請先允許懸浮視窗"
            !isAccessibilityEnabled() -> "請先啟用無障礙點擊服務"
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
        Toast.makeText(this, "已啟動；請切到遊戲，先用 Dry-run 測試", Toast.LENGTH_LONG).show()
        moveTaskToBack(true)
    }
}
