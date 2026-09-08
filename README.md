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

### Windows x64 portable 前置版

Issue #3 的分發前置作業已加入 PyInstaller onedir build、裝置安全偵測與
GitHub Actions。Windows 機器可在 PowerShell 執行：

```powershell
.\packaging\build_windows.ps1 -OutputDirectory .\build\fishing-mvp-windows
```

輸出會包含根目錄 `START.bat`／`STOP.bat`、`config\default.yaml`、可選的
`config\user.yaml`，以及 `runtime\FishingMVP.exe` 與 pinned scrcpy v4.1
官方檔案。雙擊 `START.bat` 後輸入 1–999 輪；它只會在偵測到恰好一台已授權
裝置時啟動，並強制使用 scrcpy，不會靜默 fallback 到 ADB screenshot。
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

兩次 prompt 都從偵測到的 action button 中心產生 tap proposal；離線分析預設不產生 QTE 點擊，QTE 執行需在 live 模式明確加上 `--enable-qte`，或使用下方的 `--full-auto`。QTE live 模式會用最近數幀的 marker 位移估算速度，預測輸入延遲後的位置並提前點擊；沒有穩定速度時才退回當前影格的即時判斷。

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

`--full-auto` 會自動開啟開始、QTE 與結果流程；`--max-rounds N` 預設為 1，完成 N 次「看過 `RESULT` 後回到 `WAITING`」後停止。若未加 `--live`，即使使用 `--full-auto` 也只會產生動作提案。當狀態長時間無法辨識或某個階段超時，程式會安全停止並在 `live_summary.json` 記錄 `stop_reason`，不會盲點。

部分金色魚／獎勵動畫在第一次按下動態偵測到的繼續或關閉控制後，還需要再點一下結果內容才會解除。若 `RESULT` 仍維持，full-auto 會等待 `result_extra_tap_delay_s`，從中央獎勵內容的輪廓動態推導一個額外點擊位置，最多補點一次；仍未離開就安全停止。這不是固定座標，也不會持續重試。

安全條件：

- 使用者必須提供明確 `--serial`；程式不會選擇其他裝置。
- `--package` 若提供，前景 package 會週期性檢查；不一致時會在下一次輸入前停止。週期檢查避免每一次 QTE tap 都被 `dumpsys` 阻塞。
- ADB 斷線、解析度／方向改變、偵測信心不足或狀態未知時停止或不動作。
- 程式不會自動啟動、切換、重啟或 force-stop App。
- `--enable-qte` 採即時單擊事件模型；QTE 速度預測、輸入延遲、點擊間隔與按壓時間可在 YAML 調整。
- `result_extra_tap_enabled` 與 `result_extra_tap_delay_s` 控制獎勵動畫的一次性額外點擊；位置由中央結果內容的輪廓動態產生。
- `--full-auto` 只會操作已在前景且符合 `--package` 的遊戲；它不會替使用者切換 App。
- 完整自動化的各階段 timeout 在 `config/default.yaml` 的 `automation` 區段設定；超時會停止，不會改用固定座標猜測。

預設 detector 在等待／提示／結算階段取樣 10 FPS，在 QTE／QUALITY 階段動態提升到 30 FPS；可用 `--fps` 與 `--qte-fps` 或 YAML 的 `capture_fps`／`qte_capture_fps` 調整。工作影像寬度預設為 480；座標會還原到原始 framebuffer。QTE 事件冷卻預設 0.18 秒，輸入延遲會以來源分開記錄並使用移動中位數；尚未有實測樣本時才使用 `qte_input_latency_s` 初始值。預設以 `input tap`（0ms hold）降低 Android 端落後。

scrcpy 是可選的外部擷取來源：`probe` 會顯示是否可用；使用 `--capture adb` 時不要求 scrcpy 已安裝。

scrcpy 模式會從與桌面 binary 同版本的 scrcpy-server 接收 H.264 影像，PyAV 只負責在本機解碼成 OpenCV frame；不會開啟 scrcpy 視窗，也不會在手機安裝常駐 App。預設保留原生影像尺寸，避免把 normalized 偵測座標映射到錯誤的 framebuffer。
scrcpy 影像是畫面變更驅動的；靜止 UI 會重用最新幀，避免等待畫面因沒有新封包而誤判 timeout。若裝置解析度／方向和串流不相容，live runner 會在送出前停止。
scrcpy live session 同時建立 video 與 control socket；TAP 會傳送 scrcpy 4.1 的 DOWN／UP control event，不再為每次點擊啟動 `adb shell input tap` subprocess。若 scrcpy／control socket 無法建立，`auto` 會整體回退到 ADB screenshot 與 ADB input，兩種來源的延遲樣本不會混用；`scrcpy` 強制模式則會直接報錯。live 的 `live_detections.jsonl` 與 `live_summary.json` 會記錄 video PTS、封包到達／解碼時間、frame age、CV 分析時間、control dispatch 時間，以及下一個影格／離開 QTE 的觀測延遲。
macOS 的 OpenCV 與 PyAV wheel 可能各自攜帶 FFmpeg，啟動時會出現 AVFoundation duplicate-class 警告；本機實測串流仍穩定，若遇到解碼不穩可先改用 `--capture adb`。

## 設定

預設設定在 [`config/default.yaml`](config/default.yaml)。設定的是 HSV／幾何／時序閾值，不是畫面上的絕對點擊位置。可複製後用 `--config` 指定：

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

測試涵蓋 HSV mask、normalized geometry、狀態機穩定幀／unknown grace、action cooldown、QTE 目標區去重、scrcpy packet metadata 與安全模式。影片驗證是以可重現的狀態時間線與 annotated output 為主，並不把未標註影片宣稱為正式 precision／recall benchmark。

`tests/fixtures/` 包含從測試影片抽出的少量 waiting、prompt、QTE、quality、result 影格，直接回歸 action button、prompt、gauge、marker、quality、result 與 continue 偵測；完整影片不需要放進 repository。GitHub Actions 會在 Python 3.10 與 3.12 執行安裝、pytest 與 compileall。

沒有 Android 裝置時仍可完成影片與 fixture 離線驗證；live 模式需要使用者提供已授權的 ADB 裝置。scrcpy 模式另外需要桌面 scrcpy、PyAV 與可用的 H.264 解碼環境，若條件不滿足，`auto` 會回退到 ADB screenshot。
