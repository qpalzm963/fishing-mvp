package io.github.qpalzm963.fishingmvp

/** Plain-language names for the detector's internal state and stop-reason codes. */
object Labels {
    fun state(code: String): String = when (code) {
        "waiting" -> "等待開始"
        "prompt" -> "出現提示"
        "casting" -> "拋竿中"
        "qte" -> "拉力條"
        "quality" -> "品質判定"
        "result" -> "結算畫面"
        "unknown" -> "無法辨識"
        "error" -> "錯誤"
        else -> code
    }

    fun stopReason(code: String): String = when {
        code == "user_stop" -> "你關閉了開關"
        code == "completed_rounds" -> "已完成設定的輪數"
        code == "max_retry_attempts" -> "重試次數用完"
        code == "retry_recovery_timeout" -> "重試時等不到回到起始畫面"
        code == "start_unconfirmed" -> "點了開始但畫面沒反應"
        code == "state_error" || code == "error" -> "發生錯誤"
        code == "screen_size_changed" -> "螢幕尺寸或方向改變"
        code == "no_frames" -> "收不到螢幕畫面"
        code == "android_error" -> "App 發生錯誤"
        code.startsWith("state_timeout:") -> "「${state(code.removePrefix("state_timeout:"))}」停留太久"
        else -> code
    }
}
