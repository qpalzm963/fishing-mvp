@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Fishing MVP portable launcher.  Every path is relative to this file.
set "PORTABLE_ROOT=%~dp0"
if "%PORTABLE_ROOT:~-1%"=="\" set "PORTABLE_ROOT=%PORTABLE_ROOT:~0,-1%"
set "RUNTIME_DIR=%PORTABLE_ROOT%\runtime"
set "SCRCPY_DIR=%RUNTIME_DIR%\scrcpy"
set "MVP_EXE=%RUNTIME_DIR%\FishingMVP.exe"
set "ADB_PATH=%SCRCPY_DIR%\adb.exe"
set "SCRCPY_PATH=%SCRCPY_DIR%\scrcpy.exe"
set "SCRCPY_SERVER=%SCRCPY_DIR%\scrcpy-server"
set "CONFIG_PATH=%PORTABLE_ROOT%\config\user.yaml"
set "RUN_DIR=%PORTABLE_ROOT%\run"
set "PID_FILE=%RUN_DIR%\FishingMVP.pid"
set "DISCOVERY_JSON=%RUN_DIR%\discover-device.json"
set "DISCOVERY_ERR=%RUN_DIR%\discover-device.err"

rem Keep the bundled tools first, but only for this launcher process tree.
set "PATH=%SCRCPY_DIR%;%PATH%"

if not exist "%MVP_EXE%" (
    echo [錯誤] 找不到 portable runtime\FishingMVP.exe。
    echo        請確認 ZIP 已完整解壓縮。
    exit /b 2
)
if not exist "%ADB_PATH%" (
    echo [錯誤] 找不到 bundled scrcpy\adb.exe。
    echo        本啟動器不會改用系統 PATH 的 ADB。
    exit /b 2
)
if not exist "%SCRCPY_PATH%" (
    echo [錯誤] 找不到 bundled scrcpy\scrcpy.exe。
    echo        本啟動器需要 scrcpy，不能改用 ADB screenshot。
    exit /b 2
)
if not exist "%SCRCPY_SERVER%" (
    echo [錯誤] 找不到 matching scrcpy\scrcpy-server。
    echo        請重新下載完整的 Windows x64 portable ZIP。
    exit /b 2
)

"%ADB_PATH%" version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] bundled ADB 無法啟動。
    exit /b 2
)

if not exist "%RUN_DIR%\" mkdir "%RUN_DIR%" >nul 2>&1
if not exist "%RUN_DIR%\" (
    echo [錯誤] 無法建立 portable run 目錄："%RUN_DIR%"
    exit /b 2
)

call :ensure_not_running
if errorlevel 1 exit /b %errorlevel%

set "MAX_ROUNDS="
set /p "MAX_ROUNDS=請輸入自動執行輪數（1-999，直接 Enter 不會採用預設值）："
if not defined MAX_ROUNDS goto :invalid_rounds
set "FISHING_MAX_ROUNDS=%MAX_ROUNDS%"
powershell.exe -NoProfile -Command "$s=$env:FISHING_MAX_ROUNDS; if ($s -notmatch '^[0-9]{1,3}$' -or [int]$s -lt 1 -or [int]$s -gt 999) { exit 1 }"
if errorlevel 1 goto :invalid_rounds
set "MAX_ROUNDS_VALUE="
for /f "usebackq delims=" %%N in (`powershell.exe -NoProfile -Command "[int]$env:FISHING_MAX_ROUNDS" 2^>nul`) do set "MAX_ROUNDS_VALUE=%%N"
if not defined MAX_ROUNDS_VALUE goto :invalid_rounds

del /q "%DISCOVERY_JSON%" >nul 2>&1
del /q "%DISCOVERY_ERR%" >nul 2>&1

rem The discover-device subcommand is the stable hand-off contract for the
rem future frozen CLI.  It must return JSON and must not choose on ambiguity.
"%MVP_EXE%" discover-device --adb-path "%ADB_PATH%" --json > "%DISCOVERY_JSON%" 2> "%DISCOVERY_ERR%"
set "DISCOVERY_EXIT=%ERRORLEVEL%"
if not "%DISCOVERY_EXIT%"=="0" goto :discovery_failed

set "FISHING_DISCOVERY_JSON=%DISCOVERY_JSON%"
set "DEVICE_SERIAL="
for /f "usebackq delims=" %%S in (`powershell.exe -NoProfile -Command "$p=Get-Content -Raw -LiteralPath $env:FISHING_DISCOVERY_JSON | ConvertFrom-Json; if ($p.ok -eq $true -and $p.serial -is [string] -and $p.serial.Length -gt 0) { [Console]::WriteLine($p.serial) }" 2^>nul`) do set "DEVICE_SERIAL=%%S"
if not defined DEVICE_SERIAL goto :discovery_failed

set "CONFIG_ARG="
if exist "%CONFIG_PATH%" set "CONFIG_ARG=--config "%CONFIG_PATH%""
set "FISHING_MVP_EXE=%MVP_EXE%"
set "FISHING_ROOT=%PORTABLE_ROOT%"
set "FISHING_RUN_DIR=%RUN_DIR%"
set "FISHING_DEVICE_SERIAL=%DEVICE_SERIAL%"
set "FISHING_MVP_ARGS=live --serial "%DEVICE_SERIAL%" --capture scrcpy --live --full-auto --max-rounds %MAX_ROUNDS_VALUE% --adb-path "%ADB_PATH%" --scrcpy-path "%SCRCPY_PATH%" --output-dir "%RUN_DIR%" %CONFIG_ARG%"

set "MVP_PID="
for /f "usebackq delims=" %%P in (`powershell.exe -NoProfile -Command "$p=Start-Process -FilePath $env:FISHING_MVP_EXE -ArgumentList $env:FISHING_MVP_ARGS -WorkingDirectory $env:FISHING_ROOT -PassThru; $p.Id" 2^>nul`) do set "MVP_PID=%%P"
if not defined MVP_PID (
    echo [錯誤] 無法啟動 FishingMVP.exe。
    echo        請查看 run\discover-device.err 或重新解壓縮。
    exit /b 7
)
> "%PID_FILE%" echo %MVP_PID%
echo 已啟動 Fishing MVP：裝置 %DEVICE_SERIAL%，輪數 %MAX_ROUNDS_VALUE%。
echo 執行路徑：scrcpy + full-auto；不會 fallback 到 ADB screenshot。
echo 如需安全停止，請雙擊 STOP.bat。
exit /b 0

:invalid_rounds
echo [錯誤] 輪數必須是 1 到 999 的整數；輸入無效，程式未啟動。
exit /b 3

:discovery_failed
echo [錯誤] 無法取得唯一一台已授權的 Android 裝置；程式未啟動。
if exist "%DISCOVERY_JSON%" type "%DISCOVERY_JSON%"
if exist "%DISCOVERY_ERR%" type "%DISCOVERY_ERR%"
echo 請確認只有一台裝置、已允許 USB debugging，且狀態不是 offline。
exit /b 6

:ensure_not_running
if not exist "%PID_FILE%" exit /b 0
set "OLD_PID="
set /p "OLD_PID="<"%PID_FILE%"
if not defined OLD_PID (
    del /q "%PID_FILE%" >nul 2>&1
    exit /b 0
)
set "FISHING_OLD_PID=%OLD_PID%"
set "FISHING_MVP_EXE=%MVP_EXE%"
powershell.exe -NoProfile -Command "$s=$env:FISHING_OLD_PID; if ($s -notmatch '^[1-9][0-9]*$') { exit 2 }; try { $p=Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f [int64]$s) -ErrorAction Stop; $expected=[IO.Path]::GetFullPath($env:FISHING_MVP_EXE); if ($p -and $p.ExecutablePath -and ([IO.Path]::GetFullPath($p.ExecutablePath) -ieq $expected)) { exit 0 }; exit 1 } catch { exit 2 }"
set "PID_CHECK=%ERRORLEVEL%"
if "%PID_CHECK%"=="0" (
    echo [錯誤] 已有這個 portable 目錄啟動的 FishingMVP.exe 正在執行。
    echo        如需重啟，請先雙擊 STOP.bat。
    exit /b 4
)
if not "%PID_CHECK%"=="1" (
    echo [錯誤] 無法安全驗證既有 PID 檔案；未啟動新的程序。
    echo        請檢查 portable\run\FishingMVP.pid 後再使用 STOP.bat。
    exit /b 5
)
del /q "%PID_FILE%" >nul 2>&1
exit /b 0
