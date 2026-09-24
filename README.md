# Fishing MVP

Python + OpenCV + ADB 的動態釣魚 UI 偵測 MVP。專案針對錄影中的「釣魚大賽」畫面建立可重跑的視覺狀態機，並提供保守的 ADB 輸入介面。預設只做 dry-run，不會對手機送出點擊。

## 目前已驗證的畫面

測試影片為直向 1080×2340、約 26.95 秒，包含兩輪可觀測流程：

`waiting` → `prompt`（紫色圓形控制與「就是現在！用力拉！！」）→ `casting` → `result`，以及 `waiting` → `qte`（30 秒倒數與水平色帶）→ `quality`（Cool / Great / Perfect）→ `result`。

偵測器不依賴影片的固定點擊座標。它會先以 Hough circle／輪廓找出下方大型圓形 action control，再用該控制的半徑與中心推導 prompt、QTE bar、游標與結算控制的相對搜尋區域。顏色以可由 YAML 調整的 HSV mask 為主，結算的黃色元素使用 normalized area ratio，時序狀態用連續穩定幀抑制閃爍；Template Matching 沒有被設為控制決策的必要條件。

## 安裝

建議使用 Python 3.10 以上的虛擬環境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

安裝完成後即可使用可執行入口：

```bash
fishing-mvp --help
```

要使用低延遲 scrcpy 影像串流，另外安裝 scrcpy 與 PyAV：

```bash
brew install scrcpy
python -m pip install -e '.[dev,scrcpy]'
```

### Windows x64 portable

Windows 分發包含 PyInstaller onedir build、裝置安全偵測與
GitHub Actions。Windows 機器可在 PowerShell 執行：

```powershell
.\packaging\build_windows.ps1 -OutputDirectory .\build\fishing-mvp-windows
```

輸出會包含根目錄 `START.bat`／`STOP.bat`、`config\default.yaml`、可選的
`config\user.yaml`，以及 `runtime\FishingMVP.exe` 與 pinned scrcpy v4.1
官方檔案。雙擊 `START.bat` 後輸入 1–999 輪（Enter 預設 1 輪）；它只會在偵測到恰好一台已授權
裝置、且能辨識目前前景 package 時啟動，並強制使用 scrcpy，不會靜默 fallback
到 ADB screenshot。啟動前也會把 package 傳入 live runner，持續做前景安全檢查。
Windows build 使用 `packaging/constraints-windows.txt` 固定 app/native 依賴，
並由 frozen `FishingMVP.exe runtime-smoke` 實際驗證 packaged config 與 PyAV
H.264 decoder；只有 `main` 的手動 dispatch 或 `v*` tag push 會發布 Release。
啟動、中文階段／輪數進度與結果留在同一個視窗，錯誤可修正後重試。
`STOP.bat` 會要求程式停止新的輸入並保存紀錄；完成後按鍵才關閉視窗。
每次執行與重試紀錄分別保存在 `run/sessions/日期時間-編號/`，不覆蓋舊紀錄。
完整流程與限制請見 [`packaging/README.md`](packaging/README.md) 與
[`portable/使用說明.txt`](portable/使用說明.txt)。

## 離線影片分析

```bash
python -m fishing_mvp analyze-video \
  --input /Users/vince.huang/Downloads/1000018497.mp4 \
  --output-dir outputs/fishing_1000018497
```

除錯模式是同一套分析流程的明確別名：

```bash
python -m fishing_mvp debug \
  --input /Users/vince.huang/Downloads/1000018497.mp4 \
  --output-dir outputs/fishing_1000018497_debug \
  --analysis-fps 12
```

輸出包含：

- `annotated.mp4`：狀態、信心、候選框、QTE 游標／目標色帶與建議點擊。
- `detections.jsonl`：逐幀偵測資料與 normalized action 座標。
- `timeline.csv`：逐幀狀態時間線。
- `summary.json`：影片資訊、狀態統計、狀態轉移與 action proposals。
- `snapshots/`：每次狀態轉移的除錯畫面。

分析影片時，所有 action 都只會被記錄成 proposal，不會執行 ADB 點擊。

本次提供的影片驗證產物在 `outputs/fishing_1000018497_reviewed_final/`。以 12 FPS 取樣的狀態轉移為：

```text
0.31s waiting
6.78s prompt -> 7.05s casting -> 7.97s result -> 10.23s waiting
18.46s prompt -> 18.63s qte <-> quality -> 23.66s result -> 26.31s waiting
```

兩次 prompt 都從偵測到的 action button 中心產生 tap proposal；離線分析預設不產生 QTE 點擊，QTE 執行需在 live 模式明確加上 `--enable-qte`，或使用下方的 `--full-auto`。QTE live 模式會用最近數幀的 marker 位移估算速度，並以輸入落地時間做 ETA 判定；速度為零或抖動過大時，才退回既有的 predicted-position／當前 in-range 判定。

Issue #5 的窄目標流程分成三層：`gauge_raw_target_range` 是原始觀測、`gauge_tracked_target_range` 是時序追蹤、`gauge_safe_click_range` 是 planner 使用的範圍；舊的 `gauge_target_range` 保留為 safe range alias。Gauge 仍在最大 480px 的 work frame 定位，定位後把 box 映射回原始影像，只在 padded gauge ROI 以原始解析度 refine 紅色 marker 與黃色 target，兩個欄位各自失敗時回退到 work-frame 結果，不做全畫面 full-res 搜尋。

Tracker 對 target 收縮採立即收斂，單一較寬觀測不會重新放大 safe range；放大需要受限的連續證據，marker 遮蔽／短暫缺失最多沿用 4 幀，gauge 跳變或缺失過久會 reset。ETA 使用 `target_center`、marker velocity，以及 `frame_age + analysis_duration + dispatch_latency + tap_hold`；timing window 隨 target 寬度縮放，並支援 `[0, 1]` 邊界反彈預測。`detections.jsonl` 的 `features.gauge_refine`、`features.qte` 會記錄 raw／tracked／safe range、marker、velocity、ETA、input ETA、timing window、boundary mode、reflection 與 tap reason。

2026-09-21 錄影的後段 QTE 回歸另修正兩種誤判：Cool 除了藍色面積，還須有橫向排列的字形組件，避免水面特效觸發 QUALITY 而停按；指針僅遮住黃區一側時，若未遮住的邊緣仍一致，最多沿用 `gauge_target_tracking_missing_frames` 幀的完整目標。未被指針遮擋的收縮與目標移動仍立即更新。影片重播只驗證偵測與點擊提案，實機命中仍需以 live 執行確認。

Quality 字樣搜尋區域若裁切為空，該幀會略過 quality 判定；與 action button 位置不合理的 gauge 會被拒絕，避免錯誤位置進入 QTE 點擊。逐幀 `features.quality_roi` 與 `features.gauge_geometry` 保留裁切範圍及拒絕原因，方便定位實機誤判。

使用提供的影片驗證 Issue #5：

```bash
python -m fishing_mvp analyze-video \
  --input /Users/vince.huang/Downloads/1000018497.mp4 \
  --output-dir outputs/issue5_video_validation \
  --analysis-fps 30 --full-auto --no-video
```

## Live / scrcpy / ADB

先手動在裝置上開啟遊戲，確認 ADB serial：

```bash
adb devices -l
python -m fishing_mvp probe --serial YOUR_SERIAL
```

只讀取畫面並輸出建議：

```bash
python -m fishing_mvp live \
  --serial YOUR_SERIAL \
  --package YOUR.GAME.PACKAGE \
  --capture auto \
  --output-dir outputs/live
```

`--capture auto`（預設）會優先使用 scrcpy 串流；找不到 scrcpy 或串流啟動失敗時回退到 ADB screenshot。也可以用 `--capture scrcpy` 強制要求串流，或用 `--capture adb` 明確使用舊的 ADB fallback。

只有明確加上 `--live` 才會送出手機輸入；在 scrcpy 模式下，零按壓時間的 TAP 會走 scrcpy control socket，ADB screenshot／不支援的按壓動作才會走 ADB。QTE 與結算後繼續仍需額外開關：

```bash
python -m fishing_mvp live \
  --serial YOUR_SERIAL \
  --package YOUR.GAME.PACKAGE \
  --live --enable-qte --auto-continue \
  --output-dir outputs/live
```

### 完整自動化

完整自動化會依序處理：

`waiting/start` → `prompt` → `casting` → `qte` → `quality` → `result/結算` → `waiting`

它會從畫面中找出等待／開始控制、QTE action control，以及結果畫面的動態繼續控制；不使用固定點擊座標。結果畫面沒有可辨識的主要繼續控制時，程式會等待狀態自然回到 `waiting`；看過 `RESULT` 並穩定回到 `waiting` 才算一輪完成。

先以 dry-run 預覽完整流程（只記錄 proposal，不會點手機）：

```bash
python -m fishing_mvp live \
  --serial YOUR_SERIAL \
  --package YOUR.GAME.PACKAGE \
  --capture auto \
  --full-auto \
  --max-rounds 1 \
  --output-dir outputs/live_full_auto_preview
```

確認除錯輸出後，實機執行必須同時提供 `--live --full-auto`：

```bash
python -m fishing_mvp live \
  --serial YOUR_SERIAL \
  --package YOUR.GAME.PACKAGE \
  --capture auto \
  --live --full-auto \
  --max-rounds 1 \
  --output-dir outputs/live_full_auto
```

`--full-auto` 會自動開啟開始、QTE 與結果流程；`--max-rounds N` 預設為 1，完成 N 次「看過 `RESULT` 後回到 `WAITING`」後停止。若未加 `--live`，即使使用 `--full-auto` 也只會產生動作提案。預設在狀態長時間無法辨識或某個階段超時時安全停止，並在 `live_summary.json` 記錄 `stop_reason`。

### 失敗後自動重試

Auto Retry 預設關閉。CLI 的 `--config` 或 Windows portable 的 `config/user.yaml` 可加入：

```yaml
automation:
  auto_retry_enabled: true
  max_retry_attempts: 3
  retry_delay_ms: 1000
  retry_recovery_timeout_s: 8.0
```

只適用於 `live --full-auto`（包含 portable）；dry-run 只記錄決策與動作提案。`max_retry_attempts` 是整次執行共用的額外重試額度，成功一輪不補回額度；設為 0 即不重試。失敗不計入完成輪數，重試保留既有成功輪數，因此 N 輪最多有 N + 3 次嘗試。次數及毫秒間隔必須是非負整數，復原等待秒數必須是有限、非負數值。

QTE／QUALITY 未經 RESULT 就穩定回到 WAITING，或整段 QTE 超時，會標記 `qte_miss`。單次 tap 未見成功不立即結束整輪，仍保留原有下一次掃掠的補救。其他活動階段未經 RESULT 回到 WAITING，或已知階段超時，也可進入有限的復原等待；短暫按鈕／gauge 遺失不會立即重試。

開始點擊實際送出後，若 `unconfirmed_waiting_timeout_s`（預設 8 秒）內仍停在 WAITING，會標記 `start_unconfirmed`。關閉 Auto Retry 時停止並在 portable 顯示明確訊息；開啟時只有再次確認安全的開始畫面，才可使用同一份重試額度。診斷事件會保存送出座標、輸入路徑、最後狀態與等待時間。未送出的 dry-run 提案不會觸發此判定。

復原期間不送出任何輸入，也不計入遲到的結算。系統清除 detector、狀態機、QTE marker／target／velocity、pending observation、cooldown、re-arm 與延遲樣本。只有失敗後取得的畫面能確認 WAITING；信心須達原有門檻、開始按鈕可辨識、沒有 prompt／gauge，並連續符合 `stable_frames`，才能在重試間隔屆滿後重新開始。靜止 scrcpy 畫面可重用失敗後解碼的影格，失敗前的影格不能啟動重試。

復原等待從判定失敗時計算，包含 retry delay；`retry_recovery_timeout_s` 應大於 `retry_delay_ms / 1000` 並留出辨識時間，否則會先逾時停止。等待超時記錄 `retry_recovery_timeout`，額度用盡記錄 `max_retry_attempts`。UNKNOWN 超時、ERROR、擷取／連線／權限／輸入錯誤仍停止；STOP／Ctrl+C／執行時間上限也不會觸發重試。

關閉 Auto Retry 時，已送出但未確認的開始點擊會在 8 秒後停止；未經 RESULT 返回 WAITING 仍使用既有的 8 秒等待。`live_detections.jsonl` 的 `retry_events`、`live_summary.json` 的 `retry.events` 與 `[Retry]` log 記錄 attempt、失敗原因、QTE miss 原因、額度、間隔、復原決策及重試後是否成功。Portable 的 log 保存在同次執行的 `diagnostics.log`，自動重試不建立新 session；畫面上的手動重試仍會開始一組新的指定輪數。離線影片分析不執行整輪重試。

部分金色魚／獎勵動畫在第一次按下動態偵測到的繼續或關閉控制後，還需要再確認一次。若 `RESULT` 仍維持，full-auto 會等待 `result_extra_tap_delay_s`，重新偵測當下的繼續／關閉控制並優先點擊它；只有沒有明確控制時，才會使用通過輪廓驗證的中央結果覆蓋層候選。總嘗試次數受 `result_max_attempts` 限制，且只有畫面仍被辨識為結果覆蓋層時才會重試；狀態離開 `RESULT` 後立即停止點擊，避免誤觸釣魚畫面。

安全條件：

- 使用者必須提供明確 `--serial`；程式不會選擇其他裝置。
- `--package` 若提供，前景 package 會週期性檢查；不一致時會在下一次輸入前停止。週期檢查避免每一次 QTE tap 都被 `dumpsys` 阻塞。
- ADB 斷線、解析度／方向改變、偵測信心不足或狀態未知時停止或不動作。
- 程式不會自動啟動、切換、重啟或 force-stop App。
- `--enable-qte` 採即時單擊事件模型；QTE 速度預測、ETA、輸入延遲、點擊間隔與按壓時間可在 YAML 調整。窄目標的 `gauge_target_min_color_pixels`、`gauge_target_min_width_ratio`、`gauge_target_min_width_px` 與 `gauge_target_min_column_coverage` 控制偵測下限；`gauge_marker_max_width_ratio` 避免背景紅色元素被當成 marker 寬度；`gauge_full_res_refine_enabled` 與 `gauge_full_res_refine_padding_ratio` 控制原始解析度 ROI refine；`gauge_target_tracking_frames`、`gauge_target_tracking_max_width_ratio`、`gauge_target_tracking_max_gap_ratio` 與 `gauge_target_tracking_missing_frames` 控制收縮／放大證據與短暫遮蔽 recovery。`qte_eta_enabled` 預設開啟，`qte_boundary_mode` 可選 `auto`、`reflection` 或 `clip`；預測點擊未觀察到實際入框時，會在 `qte_prediction_grace_s` 後重新武裝，保留後續掃掠的補救機會。
- `result_extra_tap_enabled`、`result_extra_tap_delay_s` 與 `result_max_attempts` 控制結算控制的有限重試；每次位置都由當前畫面的控制或結果輪廓動態產生，且不使用固定座標。
- `--full-auto` 只會操作已在前景且符合 `--package` 的遊戲；它不會替使用者切換 App。
- 完整自動化的各階段 timeout 在 `config/default.yaml` 的 `automation` 區段設定；預設超時會停止，啟用 Auto Retry 後也只在確認可開始的穩定畫面重試。

預設 detector 在等待／提示／結算階段取樣 10 FPS，在 QTE／QUALITY 階段動態提升到 30 FPS；可用 `--fps` 與 `--qte-fps` 或 YAML 的 `capture_fps`／`qte_capture_fps` 調整。Gauge 工作影像寬度預設為 480，marker／target 只在映射後的原始 gauge ROI refine；輸出的 normalized 座標仍會還原到原始 framebuffer。QTE 事件冷卻預設 0.18 秒，ETA horizon 會加總 frame age、CV 分析、來源分開的 dispatch latency 與 tap hold；尚未有實測樣本時才使用 `qte_input_latency_s` 初始值。預設以 `input tap`（0ms hold）降低 Android 端落後。

scrcpy 是可選的外部擷取來源：`probe` 會顯示是否可用；使用 `--capture adb` 時不要求 scrcpy 已安裝。

scrcpy 模式會從與桌面 binary 同版本的 scrcpy-server 接收 H.264 影像，PyAV 只負責在本機解碼成 OpenCV frame；不會開啟 scrcpy 視窗，也不會在手機安裝常駐 App。預設保留原生影像尺寸，避免把 normalized 偵測座標映射到錯誤的 framebuffer。
scrcpy 影像是畫面變更驅動的；靜止 UI 會重用最新幀，避免等待畫面因沒有新封包而誤判 timeout。若裝置解析度／方向和串流不相容，live runner 會在送出前停止。
QTE 速度估算優先使用來源影格 PTS，沒有 PTS 時使用解碼時間，再退回分析時間；來源或時間基準改變時會重設歷史。重複影格不加入速度樣本，也不觸發新的 QTE 點擊，待新影格到達才恢復判斷。靜止的等待畫面仍可重用。
scrcpy live session 同時建立 video 與 control socket；TAP 會傳送 scrcpy 4.1 的 DOWN／UP control event，不再為每次點擊啟動 `adb shell input tap` subprocess。若 scrcpy／control socket 無法建立，`auto` 會整體回退到 ADB screenshot 與 ADB input，兩種來源的延遲樣本不會混用；`scrcpy` 強制模式則會直接報錯。live 的 `live_detections.jsonl` 與 `live_summary.json` 會記錄 video PTS、封包到達／解碼時間、frame age、CV 分析時間、control dispatch 時間，以及下一個影格／離開 QTE 的觀測延遲。
macOS 的 OpenCV 與 PyAV wheel 可能各自攜帶 FFmpeg，啟動時會出現 AVFoundation duplicate-class 警告；本機實測串流仍穩定，若遇到解碼不穩可先改用 `--capture adb`。

Frozen runtime 的診斷命令會輸出 JSON，供 Windows package smoke 使用：

```bash
python -m fishing_mvp runtime-smoke
```

它只載入 packaged `default.yaml`，並在目前 executable 的 Python runtime 中建立
H.264 decoder；失敗時回傳 exit code 2，不會嘗試連線或操作 Android 裝置。

Live 模式先送出動作，再產生狀態切換截圖；截圖失敗會記錄警告並繼續。連線、前景、解析度或輸入錯誤仍會停止，先保存 `live_summary.json` 的 `error`、`last_state` 與 `completed_rounds`，再回傳失敗；若儲存空間本身無法寫入，會在終端報告摘要寫入失敗。摘要以暫存檔替換，避免留下半份 JSON。
QTE 的後續影格觀測只接受輸入送出後解碼的新影格，延遲從 dispatch 結束時計算。待處理觀測在離開 QTE／QUALITY 時完成並移除，超過 `qte_timeout_s` 與 `quality_timeout_s` 中較大的值則標記 `timed_out` 並移除；歷史結果仍保留在 actions 摘要中。

## 設定

預設設定在 [`config/default.yaml`](config/default.yaml)。設定的是 HSV／幾何／時序閾值，不是畫面上的絕對點擊位置。可複製後用 `--config` 指定：

啟動前會嚴格驗證合併後的設定：未知欄位、錯誤型別、非有限數值、無效範圍或顛倒的上下限都會直接報錯並指出欄位。布林值請使用 YAML 的 `true`／`false`，數值不要加引號；FPS 至少為 0.5，比例介於 0 到 1。`--fps`／`--qte-fps` 也適用相同驗證。既有設定若含拼字錯誤或未使用的欄位，需先修正才能啟動。

```bash
python -m fishing_mvp analyze-video \
  --input /path/to/video.mp4 \
  --config /path/to/my-config.yaml \
  --output-dir outputs/custom
```

## 測試與限制

```bash
python -m pytest
```

測試涵蓋 HSV mask、normalized geometry、狀態機穩定幀／unknown grace、action cooldown、QTE 目標區去重、shrink-aware replay、full-resolution ROI、ETA 左右向掃掠、零／抖動速度 fallback、邊界反彈、scrcpy packet metadata 與安全模式。影片驗證是以可重現的狀態時間線與 annotated output 為主，並不把未標註影片宣稱為正式 precision／recall benchmark。

`tests/fixtures/` 包含從測試影片抽出的少量 waiting、prompt、QTE、quality、result 影格，直接回歸 action button、prompt、gauge、marker、quality、result 與 continue 偵測；完整影片不需要放進 repository。GitHub Actions 會在 Python 3.10 與 3.12 執行安裝、pytest 與 compileall。

沒有 Android 裝置時仍可完成影片與 fixture 離線驗證；live 模式需要使用者提供已授權的 ADB 裝置。scrcpy 模式另外需要桌面 scrcpy、PyAV 與可用的 H.264 解碼環境，若條件不滿足，`auto` 會回退到 ADB screenshot。
