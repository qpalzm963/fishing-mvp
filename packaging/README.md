# Windows portable packaging contract

This directory is the Issue #3 preparation layer. It packages the existing
`python -m fishing_mvp` / `fishing-mvp` CLI as a Windows x64 PyInstaller
onedir runtime and does not change gameplay or detector behavior.

Run `build_windows.ps1` on Windows PowerShell 5.1+ (or PowerShell 7) from a
fresh checkout. The script creates an isolated build environment, pins
PyInstaller 6.13.0, downloads the official `scrcpy-win64-v4.1.zip`, verifies
SHA256 `5b12172b3264b2889f4583ee64752ce832e29bc8b1089dca81093459697165db`,
and fails before assembling anything if a prerequisite or archive check fails.

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

Use the root `START.bat` for the operator flow. It asks for a strict 1–999
round count, discovers exactly one authorized device, and starts the real
live full-auto path. `runtime\fishing-mvp.cmd` is the developer entrypoint;
both entrypoints resolve bundled tools without a system-wide scrcpy/ADB
installation. The script never overwrites an existing `runtime/`, `config/`,
or manifest artifact, so use a new output directory for each build.

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
remain CI-only until a clean Windows host runs the generated ZIP.
