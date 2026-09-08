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
python -m pip install -r requirements.txt
```

也可以用可執行入口：

```bash
python -m pip install -e '.[dev]'
fishing-mvp --help
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

## Live / ADB

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
  --output-dir outputs/live
```

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

目前 scrcpy 是可選外部工具：`probe` 會顯示是否可用；偵測與 ADB screenshot fallback 不要求 scrcpy 已安裝。

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

測試涵蓋 HSV mask、normalized geometry、狀態機穩定幀／unknown grace、action cooldown 與安全模式。影片驗證是以可重現的狀態時間線與 annotated output 為主，並不把未標註影片宣稱為正式 precision／recall benchmark。

`tests/fixtures/` 包含從測試影片抽出的少量 waiting、prompt、QTE、quality、result 影格，直接回歸 action button、prompt、gauge、marker、quality、result 與 continue 偵測；完整影片不需要放進 repository。GitHub Actions 會在 Python 3.10 與 3.12 執行安裝、pytest 與 compileall。

這次工作環境當下沒有連線 ADB 裝置，且沒有預裝 OpenCV、pytest 或 scrcpy；依賴安裝後可完成離線驗證，live 仍需使用者提供裝置與 package 才能做實機 smoke test。
