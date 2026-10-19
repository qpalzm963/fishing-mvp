package io.github.qpalzm963.fishingmvp

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.SystemClock
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer

/**
 * On-device check that needs no game: runs the Python detector on bundled
 * fixtures, then pushes one fixture through the same RGBA byte[] bridge the
 * capture loop uses (dry-run session, no dispatcher).
 */
object SelfTest {
    private const val TAG = "FishingSelfTest"
    private val NAMES = listOf("waiting", "prompt", "qte", "quality", "result")

    fun run(context: Context): String {
        if (!Python.isStarted()) Python.start(AndroidPlatform(context))
        val dir = File(context.cacheDir, "fixtures").apply { mkdirs() }
        NAMES.forEach { name ->
            context.assets.open("fixtures/$name.jpg").use { input ->
                File(dir, "$name.jpg").outputStream().use { input.copyTo(it) }
            }
        }
        val module = Python.getInstance().getModule("fishing_mvp.mobile")
        val importStarted = SystemClock.uptimeMillis()
        val report = JSONObject(module.callAttr("self_test", dir.absolutePath, 3).toString())
        report.put("self_test_ms", SystemClock.uptimeMillis() - importStarted)

        // Bridge check: Bitmap (RGBA_8888 bytes) -> byte[] -> process_rgba.
        val bitmap = BitmapFactory.decodeFile(File(dir, "qte.jpg").absolutePath)
            .copy(Bitmap.Config.ARGB_8888, false)
        val bytes = ByteArray(bitmap.byteCount)
        bitmap.copyPixelsToBuffer(ByteBuffer.wrap(bytes))
        val sessionDir = File(context.cacheDir, "selftest-session").apply { deleteRecursively(); mkdirs() }
        val session = module.callAttr("create_session", sessionDir.absolutePath, 1, false, "selftest.package", null, null)
        val bridge = JSONObject()
        val started = SystemClock.uptimeMillis()
        var last: JSONObject? = null
        repeat(5) {
            last = JSONObject(
                session.callAttr(
                    "process_rgba", bytes, bitmap.width, bitmap.height, bitmap.rowBytes,
                    System.nanoTime() / 1e9, false, "selftest.package",
                ).toString()
            )
        }
        bridge.put("frames", 5)
        bridge.put("total_ms", SystemClock.uptimeMillis() - started)
        bridge.put("last", last)
        session.callAttr("stop", "self_test")
        report.put("bridge", bridge)
        val bridgeOk = last?.optString("state") == "qte" && last?.optJSONObject("error") == null
        report.put("bridge_ok", bridgeOk)
        val text = report.toString(2)
        Log.i(TAG, report.toString())
        return text
    }
}
