@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title Fishing MVP
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "MVP_EXE=%~dp0runtime\FishingMVP.exe"
if not exist "%MVP_EXE%" goto :missing
"%MVP_EXE%" portable-start
set "LAUNCH_EXIT=%ERRORLEVEL%"
if not "%LAUNCH_EXIT%"=="0" echo [未完成] 請依上方提示處理；若沒有詳細訊息，請重新完整解壓縮套件。
goto :finish
:missing
echo [套件不完整] 找不到 runtime\FishingMVP.exe。
echo 請先完整解壓縮 ZIP，保留 runtime 與 config 資料夾，再雙擊 START.bat。
set "LAUNCH_EXIT=2"
:finish
echo.
echo 按任意鍵關閉此視窗。
pause >nul
exit /b %LAUNCH_EXIT%
