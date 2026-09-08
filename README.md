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

兩次 prompt 都從偵測到的 action button 中心產生 tap proposal；離線分析預設不產生 QTE 點擊，QTE 執行需在 live 模式明確加上 `--enable-qte`。

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

只有明確加上 `--live` 才會送出 ADB input；QTE 與結算後繼續仍需額外開關：

```bash
python -m fishing_mvp live \
  --serial YOUR_SERIAL \
  --package YOUR.GAME.PACKAGE \
  --live --enable-qte --auto-continue \
  --output-dir outputs/live
```

安全條件：

- 使用者必須提供明確 `--serial`；程式不會選擇其他裝置。
- `--package` 若提供，前景 package 不一致時會在輸入前停止。
- ADB 斷線、解析度／方向改變、偵測信心不足或狀態未知時停止或不動作。
- 程式不會自動啟動、切換、重啟或 force-stop App。
- `--enable-qte` 採單擊事件模型，點擊間隔與按壓時間可在 YAML 調整。

預設 detector 取樣率是 10 FPS（目標每 100 ms 判斷一次）；QTE 事件冷卻預設 0.18 秒，實際頻率仍會受單幀 OpenCV 計算時間限制。

scrcpy 是可選的外部擷取來源：`probe` 會顯示是否可用；使用 `--capture adb` 時不要求 scrcpy 已安裝。

scrcpy 模式會從與桌面 binary 同版本的 scrcpy-server 接收 H.264 影像，PyAV 只負責在本機解碼成 OpenCV frame；不會開啟 scrcpy 視窗，也不會在手機安裝常駐 App。預設保留原生影像尺寸，避免把 normalized 偵測座標映射到錯誤的 framebuffer。
scrcpy 影像是畫面變更驅動的；靜止 UI 會重用最新幀，避免等待畫面因沒有新封包而誤判 timeout。若裝置解析度／方向和串流不相容，live runner 會在送出前停止。
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
