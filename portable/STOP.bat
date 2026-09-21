@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title Fishing MVP - Stop
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "MVP_EXE=%~dp0runtime\FishingMVP.exe"
if not exist "%MVP_EXE%" goto :missing
"%MVP_EXE%" portable-stop
set "LAUNCH_EXIT=%ERRORLEVEL%"
if not "%LAUNCH_EXIT%"=="0" echo [請確認] 請查看上方提示，或回到原啟動視窗按 Ctrl+C。
goto :finish
:missing
echo [套件不完整] 找不到 runtime\FishingMVP.exe。
echo 請回到原啟動視窗按 Ctrl+C 停止，再重新解壓縮完整套件。
set "LAUNCH_EXIT=2"
:finish
echo.
echo 按任意鍵關閉此視窗。
pause >nul
exit /b %LAUNCH_EXIT%
