@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Stop only the FishingMVP.exe PID recorded by this portable launcher.
set "PORTABLE_ROOT=%~dp0"
if "%PORTABLE_ROOT:~-1%"=="\" set "PORTABLE_ROOT=%PORTABLE_ROOT:~0,-1%"
set "MVP_EXE=%PORTABLE_ROOT%\runtime\FishingMVP.exe"
set "RUN_DIR=%PORTABLE_ROOT%\run"
set "PID_FILE=%RUN_DIR%\FishingMVP.pid"
set "DISCOVERY_JSON=%RUN_DIR%\discover-device.json"
set "DISCOVERY_ERR=%RUN_DIR%\discover-device.err"

if not exist "%PID_FILE%" (
    echo 沒有找到這個 portable 目錄的執行中 FishingMVP.exe。
    exit /b 0
)

set "MVP_PID="
set /p "MVP_PID="<"%PID_FILE%"
if not defined MVP_PID (
    del /q "%PID_FILE%" >nul 2>&1
    echo PID 檔案是空的，已安全清理。
    exit /b 0
)

set "FISHING_STOP_PID=%MVP_PID%"
set "FISHING_MVP_EXE=%MVP_EXE%"
powershell.exe -NoProfile -Command "$s=$env:FISHING_STOP_PID; if ($s -notmatch '^[1-9][0-9]*$') { exit 2 }; try { $p=Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f [int64]$s) -ErrorAction Stop; $expected=[IO.Path]::GetFullPath($env:FISHING_MVP_EXE); if (-not $p -or -not $p.ExecutablePath -or -not ([IO.Path]::GetFullPath($p.ExecutablePath) -ieq $expected)) { exit 1 }; exit 0 } catch { exit 2 }"
set "PID_CHECK=%ERRORLEVEL%"

if "%PID_CHECK%"=="1" (
    del /q "%PID_FILE%" >nul 2>&1
    echo 找不到仍在執行的 FishingMVP.exe，已清理過期 PID 檔案。
    exit /b 0
)
if not "%PID_CHECK%"=="0" (
    echo [錯誤] 無法安全驗證 PID；未終止任何程序。
    echo        請保留 "%PID_FILE%" 並手動檢查後再處理。
    exit /b 2
)

rem /T is scoped to the verified FishingMVP.exe process tree, never all adb
rem or scrcpy processes on the machine.  Try a graceful termination first.
taskkill /PID "%MVP_PID%" /T >nul 2>&1
if errorlevel 1 taskkill /PID "%MVP_PID%" /T /F >nul 2>&1
if errorlevel 1 (
    echo [錯誤] FishingMVP.exe 停止失敗；PID 檔案已保留供重試。
    exit /b 3
)
del /q "%PID_FILE%" >nul 2>&1
del /q "%DISCOVERY_JSON%" >nul 2>&1
del /q "%DISCOVERY_ERR%" >nul 2>&1
echo 已安全停止這個 portable 目錄啟動的 Fishing MVP。
exit /b 0
