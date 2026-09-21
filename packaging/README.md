# Windows portable packaging contract

This directory packages the existing
`python -m fishing_mvp` / `fishing-mvp` CLI as a Windows x64 PyInstaller
onedir runtime and does not change gameplay or detector behavior.

Run `build_windows.ps1` on Windows PowerShell 5.1+ (or PowerShell 7) from a
fresh checkout. The script creates an isolated build environment, pins
PyInstaller 6.13.0, downloads the official `scrcpy-win64-v4.1.zip`, verifies
SHA256 `5b12172b3264b2889f4583ee64752ce832e29bc8b1089dca81093459697165db`,
and fails before assembling anything if a prerequisite or archive check fails.
The pinned PyInstaller contract supports Python 3.10 through 3.13; the CI job
passes its setup-python 3.12 executable explicitly so the Windows `py.exe`
launcher cannot silently select a newer incompatible interpreter.
Application and native Python dependencies are installed with the exact pins in
`constraints-windows.txt`; the resolved set is repeated in
`runtime-manifest.json` for package inspection.

```powershell
.\packaging\build_windows.ps1 -OutputDirectory .\build\fishing-mvp-windows
```

The output contract is:

```text
build/fishing-mvp-windows/
  START.bat
  STOP.bat
  使用說明.txt
  runtime/
    FishingMVP.exe       # PyInstaller onedir executable
    fishing-mvp.cmd       # supported self-contained launcher
    scrcpy/               # complete official scrcpy archive contents
      adb.exe
      scrcpy.exe
      scrcpy-server
      AdbWinApi.dll
      AdbWinUsbApi.dll
      ...                 # remaining official codec/support files
    config/default.yaml   # bundled PyInstaller default
  config/
    default.yaml          # packaged baseline
    user.yaml             # optional operator override
    README.txt
  runtime-manifest.json
```

Use root `START.bat` for the operator flow. The batch wrapper calls frozen
`portable-start` in the same console and pauses on every exit path, including
missing-runtime errors. The interactive Python flow defaults to one round,
validates 1–999, allows correction/retry, discovers exactly one authorized
device and checks its foreground package before starting live full-auto with
scrcpy required. It shows Chinese stage/round progress instead of raw JSON.

`STOP.bat` calls `portable-stop`. Each active run publishes its PID and a unique
session token. Stop verifies the actual process executable path using Windows
QueryFullProcessImageNameW and writes a token-scoped request. The live loop
checks that request before capture and again before input, then closes its
source and saves its summary. STOP waits up to ten seconds for cleanup; if it
is still waiting, it reports that accurately and never kills unrelated tasks.
A Windows file lock prevents concurrent launchers; the OS releases it after
abnormal termination. Stale session records are cleared only by a new lock owner.

Each attempt keeps its own `run/sessions/<timestamp>-<id>/` containing
`diagnostics.log`, `live_summary.json` when available, detections and snapshots.
A retry starts a new set of the requested rounds and never overwrites prior
attempts. If the process is forcibly terminated or the window is closed, cleanup
and summary writing cannot be guaranteed; use STOP or Ctrl+C.

`runtime\fishing-mvp.cmd` remains the developer CLI entrypoint. Both entrypoints
resolve bundled tools without a system-wide scrcpy/ADB installation. The build
script never overwrites existing runtime/config/manifest artifacts; use a new
output directory for each build.

The assembled smoke contract runs `runtime-smoke` through the frozen executable
to import PyAV, create an H.264 decoder, and load the packaged config. It also
runs frozen `discover-device` with bundled ADB and expects the controlled
`no_devices` response on a device-free CI runner.
`smoke_portable_ui.py` also exercises frozen cancellation, invalid-round retry,
the no-device recovery screen, idle STOP, both actual batch wrappers and a
missing-runtime bootstrap error under a path containing spaces and Chinese.

Examples:

```powershell
.\runtime\fishing-mvp.cmd probe
.\runtime\fishing-mvp.cmd analyze-video --input C:\path\capture.mp4 --output-dir outputs\capture
.\runtime\fishing-mvp.cmd live --serial YOUR_SERIAL --capture auto --config .\config\default.yaml
```

The build also copies `START.bat`, `STOP.bat`, and `使用說明.txt` to the package
root. The official scrcpy distribution is copied intact so its native dependencies
and `scrcpy-server` stay version-matched. Keep its included notices/licenses
with the portable package when redistributing it. Windows device smoke tests
remain CI-only until a clean Windows host runs the generated ZIP. Manual Release
dispatch is accepted only when the selected ref is `main`; versioned tag pushes
are the other release path.
