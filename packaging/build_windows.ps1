#requires -Version 5.1

<#
.SYNOPSIS
    Build a self-contained Windows x64 onedir package for fishing-mvp.

.DESCRIPTION
    This script is intentionally Windows/PowerShell-only.  It creates an
    isolated build venv, builds the existing fishing-mvp CLI with PyInstaller,
    downloads the pinned official scrcpy v4.1 Windows archive, verifies its
    SHA256, and assembles a runtime directory containing both distributions.

    The generated fishing-mvp.cmd prepends its own directory to PATH.  That
    makes the bundled scrcpy.exe and adb.exe resolve without requiring either
    tool to be installed globally on the target machine.

.PARAMETER OutputDirectory
    Destination directory for the assembled portable package.  The script
    refuses to overwrite existing runtime/config artifacts.

.PARAMETER PythonCommand
    Optional Python launcher or executable.  By default py.exe is preferred,
    then python.exe.  Python 3.10 or newer is required.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [Alias("OutputDir")]
    [ValidateNotNullOrEmpty()]
    [string]$OutputDirectory,

    [Parameter(Mandatory = $false)]
    [string]$PythonCommand = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "packaging/build_windows.ps1 must be run on Windows with PowerShell; no build was attempted."
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$repoRootComparable = $repoRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
$outputRootComparable = $outputRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)

if ([string]::Equals($repoRootComparable, $outputRootComparable, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDirectory must not be the repository root."
}
if ([string]::Equals([System.IO.Path]::GetPathRoot($outputRoot), $outputRootComparable + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDirectory must not be a filesystem root."
}

$specPath = Join-Path $repoRoot "packaging\fishing_mvp.spec"
$manifestPath = Join-Path $repoRoot "packaging\runtime-manifest.json"
$configPath = Join-Path $repoRoot "config\default.yaml"
$launcherPaths = @(
    (Join-Path $repoRoot "portable\START.bat"),
    (Join-Path $repoRoot "portable\STOP.bat"),
    (Join-Path $repoRoot "portable\使用說明.txt")
)
foreach ($requiredPath in @($specPath, $manifestPath, $configPath) + $launcherPaths) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required build input is missing: $requiredPath"
    }
}

# Never silently merge into an existing package.  This avoids stale DLLs or
# stale PyInstaller files surviving a rebuild and avoids deleting user data.
$protectedArtifacts = @(
    (Join-Path $outputRoot "runtime"),
    (Join-Path $outputRoot "config"),
    (Join-Path $outputRoot "runtime-manifest.json")
) | Where-Object { Test-Path -LiteralPath $_ }
if (@($protectedArtifacts).Count -gt 0) {
    throw "OutputDirectory already contains package artifacts; choose a new directory: $($protectedArtifacts -join ', ')"
}

$scrcpyVersion = "4.1"
$scrcpyArchiveName = "scrcpy-win64-v4.1.zip"
$scrcpyUrl = "https://github.com/Genymobile/scrcpy/releases/download/v4.1/$scrcpyArchiveName"
$scrcpySha256 = "5b12172b3264b2889f4583ee64752ce832e29bc8b1089dca81093459697165db"
$pyInstallerVersion = "6.13.0"

$workRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("fishing-mvp-build-" + [Guid]::NewGuid().ToString("N"))
$locationWasPushed = $false

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $false)]
        [string[]]$ArgumentList = @()
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($ArgumentList -join ' ')"
    }
}

try {
    New-Item -ItemType Directory -Path $workRoot -Force | Out-Null

    $pythonInfo = $null
    if (-not [string]::IsNullOrWhiteSpace($PythonCommand)) {
        $pythonInfo = Get-Command $PythonCommand -ErrorAction Stop
    }
    else {
        $pythonInfo = Get-Command "py.exe" -ErrorAction SilentlyContinue
        if ($null -eq $pythonInfo) {
            $pythonInfo = Get-Command "python.exe" -ErrorAction Stop
        }
    }

    $launcherArguments = @()
    if ($pythonInfo.Name -match "^py(\.exe)?$") {
        $launcherArguments = @("-3")
    }

    $venvPath = Join-Path $workRoot "venv"
    $venvArguments = @($launcherArguments + @("-m", "venv", $venvPath))
    Invoke-Checked -FilePath $pythonInfo.Source -ArgumentList $venvArguments

    $buildPython = Join-Path $venvPath "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
        throw "The Windows build virtual environment did not produce: $buildPython"
    }

    Invoke-Checked -FilePath $buildPython -ArgumentList @(
        "-c",
        "import sys; raise SystemExit('Python 3.10 or newer is required') if sys.version_info < (3, 10) else None"
    )
    Invoke-Checked -FilePath $buildPython -ArgumentList @(
        "-c",
        "import struct; raise SystemExit('A 64-bit Python interpreter is required') if struct.calcsize('P') != 8 else None"
    )
    $pythonVersion = (& $buildPython --version 2>&1 | Out-String).Trim()
    Write-Host "Using $pythonVersion"

    # Keep the build tool pinned.  The application itself is installed from
    # this checkout so the spec consumes exactly the code being packaged.
    $projectInstallSpec = "{0}[scrcpy]" -f $repoRoot
    Invoke-Checked -FilePath $buildPython -ArgumentList @(
        "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-cache-dir",
        ("pyinstaller=={0}" -f $pyInstallerVersion),
        $projectInstallSpec
    )

    Push-Location $repoRoot
    $locationWasPushed = $true

    $pyInstallerDist = Join-Path $workRoot "pyinstaller-dist"
    $pyInstallerBuild = Join-Path $workRoot "pyinstaller-build"
    $runtimeHookPath = Join-Path $workRoot "runtime_tool_path_hook.py"
    $runtimeHookContent = @'
"""Prefer the official tools shipped beside a frozen fishing-mvp executable."""

from __future__ import annotations

import os
from pathlib import Path
import sys


if getattr(sys, "frozen", False):
    bundled_tool_directory = Path(sys.executable).resolve().parent / "scrcpy"
    if bundled_tool_directory.is_dir():
        current_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(bundled_tool_directory) + os.pathsep + current_path
'@
    Set-Content -LiteralPath $runtimeHookPath -Value $runtimeHookContent -Encoding ASCII

    $previousRuntimeHook = [System.Environment]::GetEnvironmentVariable("FISHING_MVP_RUNTIME_HOOK", "Process")
    $env:FISHING_MVP_RUNTIME_HOOK = $runtimeHookPath
    try {
        Invoke-Checked -FilePath $buildPython -ArgumentList @(
            "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", $pyInstallerDist,
            "--workpath", $pyInstallerBuild,
            $specPath
        )
    }
    finally {
        if ($null -eq $previousRuntimeHook) {
            Remove-Item Env:FISHING_MVP_RUNTIME_HOOK -ErrorAction SilentlyContinue
        }
        else {
            $env:FISHING_MVP_RUNTIME_HOOK = $previousRuntimeHook
        }
    }

    $builtRuntime = Join-Path $pyInstallerDist "FishingMVP"
    $builtExecutable = Join-Path $builtRuntime "FishingMVP.exe"
    if (-not (Test-Path -LiteralPath $builtExecutable -PathType Leaf)) {
        throw "PyInstaller completed without producing: $builtExecutable"
    }

    $archivePath = Join-Path $workRoot $scrcpyArchiveName
    Write-Host "Downloading official scrcpy $scrcpyVersion from $scrcpyUrl"
    Invoke-WebRequest -Uri $scrcpyUrl -OutFile $archivePath -UseBasicParsing

    $actualSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -ne $scrcpySha256) {
        throw "scrcpy archive SHA256 mismatch. Expected $scrcpySha256 but received $actualSha256"
    }
    Write-Host "Verified scrcpy archive SHA256: $actualSha256"

    $scrcpyExtracted = Join-Path $workRoot "scrcpy-extracted"
    Expand-Archive -LiteralPath $archivePath -DestinationPath $scrcpyExtracted -Force
    $scrcpyExecutable = Get-ChildItem -LiteralPath $scrcpyExtracted -Filter "scrcpy.exe" -File -Recurse | Select-Object -First 1
    if ($null -eq $scrcpyExecutable) {
        throw "Verified scrcpy archive does not contain scrcpy.exe"
    }
    $officialRuntimeRoot = $scrcpyExecutable.Directory.FullName

    $requiredOfficialFiles = @("scrcpy.exe", "adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")
    foreach ($requiredOfficialFile in $requiredOfficialFiles) {
        $requiredPath = Join-Path $officialRuntimeRoot $requiredOfficialFile
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Verified scrcpy archive is missing required official file: $requiredOfficialFile"
        }
    }
    $serverFile = Get-ChildItem -LiteralPath $officialRuntimeRoot -Filter "scrcpy-server*" -File | Select-Object -First 1
    if ($null -eq $serverFile) {
        throw "Verified scrcpy archive is missing scrcpy-server"
    }

    $runtimeDirectory = Join-Path $outputRoot "runtime"
    $scrcpyDirectory = Join-Path $runtimeDirectory "scrcpy"
    $configDirectory = Join-Path $outputRoot "config"
    New-Item -ItemType Directory -Path $runtimeDirectory, $scrcpyDirectory, $configDirectory -Force | Out-Null

    # Copy the complete PyInstaller onedir output, including _internal and the
    # bundled default config, then copy every file shipped by scrcpy. Keeping
    # the official distribution intact avoids accidentally omitting a codec DLL.
    Get-ChildItem -LiteralPath $builtRuntime -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $runtimeDirectory -Recurse -Force
    }
    Get-ChildItem -LiteralPath $officialRuntimeRoot -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $scrcpyDirectory -Recurse -Force
    }

    foreach ($runtimeFile in $requiredOfficialFiles + @($serverFile.Name)) {
        $runtimePath = Join-Path $scrcpyDirectory $runtimeFile
        if (-not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) {
            throw "Assembled runtime is missing: runtime\scrcpy\$runtimeFile"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $runtimeDirectory "FishingMVP.exe") -PathType Leaf)) {
        throw "Assembled runtime is missing: runtime\FishingMVP.exe"
    }

    # The launcher is the supported entrypoint for a self-contained package:
    # it makes all bundled native tools discoverable to the existing CLI.
    $launcherContent = @"
@echo off
setlocal
set "PATH=%~dp0scrcpy;%PATH%"
"%~dp0FishingMVP.exe" %*
exit /b %ERRORLEVEL%
"@
    Set-Content -LiteralPath (Join-Path $runtimeDirectory "fishing-mvp.cmd") -Value $launcherContent -Encoding ASCII

    $runtimeReadmeContent = @"
This directory is the self-contained fishing-mvp runtime.

Use fishing-mvp.cmd as the entrypoint. It prepends the scrcpy subdirectory to
PATH so the bundled scrcpy.exe and adb.exe are used without a system install.
The Python application is FishingMVP.exe; its PyInstaller support files are
kept beside it.
"@
    Set-Content -LiteralPath (Join-Path $runtimeDirectory "README.txt") -Value $runtimeReadmeContent -Encoding UTF8

    # Keep an operator-editable copy beside runtime.  The CLI automatically
    # layers config\user.yaml over this baseline when that file exists.
    Copy-Item -LiteralPath $configPath -Destination (Join-Path $configDirectory "default.yaml") -Force
    $configReadmeContent = @"
Put operator-owned configuration files under config\.

The included default.yaml is the package baseline. Create config\user.yaml
for operator-owned overrides; the CLI layers it automatically over the
baseline. You can also pass another YAML file explicitly with --config.
"@
    Set-Content -LiteralPath (Join-Path $configDirectory "README.txt") -Value $configReadmeContent -Encoding UTF8

    foreach ($launcherPath in $launcherPaths) {
        Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $outputRoot ([System.IO.Path]::GetFileName($launcherPath))) -Force
    }
    Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $outputRoot "runtime-manifest.json") -Force

    Write-Host ""
    Write-Host "Portable package created: $outputRoot"
    Write-Host "Entry point: $(Join-Path $runtimeDirectory 'fishing-mvp.cmd')"
    Write-Host "Config template: $(Join-Path $configDirectory 'default.yaml')"
}
finally {
    if ($locationWasPushed) {
        Pop-Location
    }
    if (Test-Path -LiteralPath $workRoot) {
        Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
