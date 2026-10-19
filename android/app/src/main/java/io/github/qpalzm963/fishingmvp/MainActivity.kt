package io.github.qpalzm963.fishingmvp

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.EditText
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
    }

    private lateinit var overlayStatus: TextView
    private lateinit var a11yStatus: TextView
    private lateinit var packageInput: EditText
    private lateinit var suggestButton: Button
    private lateinit var serviceStatus: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val dp = resources.displayMetrics.density
        fun px(v: Int) = (v * dp).toInt()
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(px(16), px(16), px(16), px(24))
        }
        fun text(value: String, size: Float = 14f) = TextView(this).apply {
            text = value
            textSize = size
            setPadding(0, px(6), 0, px(6))
        }
        fun button(value: String, click: () -> Unit) = Button(this).apply {
            text = value
            setOnClickListener { click() }
        }

        column.addView(text("Fishing MVP（Android 原型）", 20f))
        column.addView(text(
            "在手機上直接執行 fishing-mvp 偵測：MediaProjection 擷取畫面、無障礙服務送出點擊。" +
                "預設 Dry-run，只記錄提案；在懸浮視窗明確打開 LIVE 才會點擊。"
        ))

        column.addView(text("1. 權限", 16f))
        overlayStatus = text("")
        column.addView(overlayStatus)
        column.addView(button("開啟「顯示在其他應用程式上層」設定") {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        })
        a11yStatus = text("")
        column.addView(a11yStatus)
        column.addView(button("開啟無障礙設定（啟用 Fishing MVP 點擊服務）") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        })

        column.addView(text("2. 目標遊戲 package（只在它位於前景時動作）", 16f))
        packageInput = EditText(this).apply {
            hint = "例如 com.example.game"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            setSingleLine()
            setText(Prefs.targetPackage(this@MainActivity))
        }
        column.addView(packageInput)
        suggestButton = button("") {
            TapAccessibilityService.lastExternalPackage?.let { packageInput.setText(it) }
        }
        column.addView(suggestButton)
        column.addView(button("儲存 package") { savePackage(showToast = true) })

        column.addView(text("3. 擷取寬度（越小 QTE 延遲越低）", 16f))
        val group = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL }
        val current = Prefs.captureWidth(this)
        Prefs.CAPTURE_WIDTHS.forEach { width ->
            group.addView(RadioButton(this).apply {
                id = View.generateViewId()
                text = "${width}px"
                isChecked = width == current
                setOnCheckedChangeListener { _, checked -> if (checked) Prefs.setCaptureWidth(this@MainActivity, width) }
            })
        }
        column.addView(group)

        column.addView(text("4. 開始", 16f))
        column.addView(button("授權螢幕擷取並顯示懸浮視窗") { requestCapture() })
        column.addView(button("結束擷取服務") {
            startService(Intent(this, AutomationService::class.java).setAction(AutomationService.ACTION_SHUTDOWN))
        })
        serviceStatus = text("")
        column.addView(serviceStatus)

        column.addView(text("偵測器自我測試（不需要遊戲）", 16f))
        val selfTestOutput = text("", 11f).apply { setTextIsSelectable(true) }
        column.addView(button("執行自我測試") {
            selfTestOutput.text = "執行中…（第一次載入 OpenCV 需要幾秒）"
            Thread {
                val result = runCatching { SelfTest.run(applicationContext) }
                    .getOrElse { "失敗：${it.javaClass.simpleName}: ${it.message}" }
                runOnUiThread { selfTestOutput.text = result }
            }.start()
        })
        column.addView(selfTestOutput)
        column.addView(text(
            "紀錄位置：${AutomationService.sessionsRoot(this).absolutePath}\n" +
                "自訂設定（可選）：${AutomationService.userConfig(this).absolutePath}",
            12f,
        ).apply { setTextColor(Color.DKGRAY) })

        setContentView(ScrollView(this).apply { addView(column) })

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

    private fun refresh() {
        val overlayOk = Settings.canDrawOverlays(this)
        overlayStatus.text = if (overlayOk) "✓ 懸浮視窗權限已允許" else "✗ 尚未允許懸浮視窗"
        val a11yOk = isAccessibilityEnabled()
        a11yStatus.text = when {
            TapAccessibilityService.instance != null -> "✓ 無障礙點擊服務已連線"
            a11yOk -> "… 無障礙服務已啟用，等待連線"
            else -> "✗ 尚未啟用無障礙點擊服務"
        }
        val suggestion = TapAccessibilityService.lastExternalPackage
        suggestButton.text = if (suggestion != null) "使用最近的前景 App：$suggestion" else "（開啟遊戲後回來，可帶入最近的前景 App）"
        suggestButton.isEnabled = suggestion != null
        serviceStatus.text = if (AutomationService.isRunning) "擷取服務執行中" else "擷取服務未啟動"
    }

    private fun isAccessibilityEnabled(): Boolean {
        val expected = ComponentName(this, TapAccessibilityService::class.java).flattenToString()
        val enabled = Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        return enabled.split(':').any { it.equals(expected, ignoreCase = true) }
    }

    private fun savePackage(showToast: Boolean): String {
        val value = packageInput.text.toString().trim()
        Prefs.setTargetPackage(this, value)
        if (showToast) Toast.makeText(this, "已儲存：$value", Toast.LENGTH_SHORT).show()
        return value
    }

    private fun requestCapture() {
        val target = savePackage(showToast = false)
        val problem = when {
            !Settings.canDrawOverlays(this) -> "請先允許懸浮視窗"
            !isAccessibilityEnabled() -> "請先啟用無障礙點擊服務"
            target.isEmpty() -> "請先輸入目標遊戲 package"
            target == packageName -> "目標 package 不能是本 App"
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
