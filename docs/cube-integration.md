# Cube synthesis integration — setup & daily use

**Audience:** you. This is the user-facing doc that goes alongside `chest_state.py` in the repo.

This document explains how to set up and use the Cube synthesis integration
that combines `tbh-bot-mvp`'s chest bot with
[`preschian/tbh-presence`](https://github.com/preschian/tbh-presence)'s
BepInEx auto-synthesis plugin.

## What this gives you

- **Chest clicks:** unchanged from v1 of `tbh-bot-mvp`. The orchestrator spawns
  `chest_state.py --click` and lets it do its job.
- **Cube auto-synthesis:** opportunistic. The cube loop runs **only while you
  have the Cube panel open AND you have pressed F8 to arm it**. Closing the
  cube panel or pressing F8 again stops it. There is no scheduled clobbering
  of your screen.
- **One log:** `orchestrator.log` shows both streams.

## What this does NOT do

- **No inventory scanning.** We rely on the BepInEx plugin's `MaxGrade=2`
  safety net instead. See SPEC.md §11 for the long reasoning.
- **No unattended launch.** Run `py orchestrator.py --click` by hand at the
  keyboard every time you want it running. There is no daemon mode.
- **No screen yanking.** The plugin's `AutoOpenCube` is set to `false`. The
  cube does not yank your screen over to the cube panel.

## One-time install (manual)

### Prerequisites

- Windows 10/11
- TBH installed (default: `C:\Program Files\TesseractStudio\TaskBarHero`)
- Python 3.10+ on PATH (use `python`, **not** `py` — `py` is NOT on Admin's
  PowerShell PATH; see the persistent memory note)
- `tbh-meter` installed and writing `live.json` (your existing setup; no change)
- `pip install -r requirements.txt` (your existing setup; no change — cube
  integration adds no new packages)

### Steps

1. **Install BepInEx** (once, manually, on Admin):
   - Download `BepInEx-Unity.IL2CPP-win-x64-*.zip` from
     [builds.bepinex.dev/projects/bepinex_be](https://builds.bepinex.dev/projects/bepinex_be).
   - Extract it into `C:\Program Files\TesseractStudio\TaskBarHero\`
     so that `winhttp.dll` and `BepInEx\` sit next to `TaskBarHero.exe`.
   - Launch TBH once, wait until the main menu shows, close it. (BepInEx
     generates interop assemblies on first launch.)

2. **Get the cube plugin DLL**:
   - From [preschian/tbh-presence releases](https://github.com/preschian/tbh-presence/releases),
     download `TbhCompanion-next.zip` (or just grab `autosynth/prebuilt/TbhAutoSynth-next.dll`).
   - Save it as `C:\Users\Admin\tbh-bot-mvp\prebuilt\TbhAutoSynth-next.dll`
     (create the `prebuilt\` folder if it doesn't exist).

3. **Run the install script**:
   ```powershell
   cd C:\Users\Admin\tbh-bot-mvp
   .\cube_launcher.ps1
   ```
   - This copies the DLL into `BepInEx\plugins\`.
   - Writes the v1 config (`AutoStart=false`, `AutoOpenCube=false`, `MaxGrade=2`).
   - Backs up any existing config to `.bak`.
   - Prints READY and the manual next steps.

### Verify the install

You should see **READINESS = true** for all of these:

- `[ ] TaskBarHero.exe` is **not** running when `cube_launcher.ps1` runs.
- `[ ] BepInEx\winhttp.dll` exists next to `TaskBarHero.exe`.
- `[ ] prebuilt\TbhAutoSynth-next.dll` exists in your repo.
- `[ ] cube_launcher.ps1` exits 0.

## Daily use

### Run by hand at the keyboard

```powershell
cd C:\Users\Admin\tbh-bot-mvp

# 1. Launch TBH. (NOT this script. You, manually.)
# 2. Wait ~10s for BepInEx to load the cube plugin.

# 3. Verify the wireup (no clicks):
py orchestrator.py --dry-run
# Expect: "[boot] READY" with TBH pid + cube status file path. Ctrl-C to stop.

# 4. Inspect the status file in another terminal:
notepad "$env:LOCALAPPDATA\tbh-companion\autosynth-status.json"
# Expect: JSON like {"auto":false,"phase":"Fill","cycles":0,...}.

# 5. Live mode (chest clicks enabled):
py orchestrator.py --click
# Expect: "[boot] READY" then heartbeats every 30s.

# 6. In TBH: open the Cube panel, press F8. Watch the orchestrator log.
```

### Stop everything

- Press **Ctrl-C** in the orchestrator's terminal. Chest script child
  stops; cube plugin state preserved.
- Press **F8** in TBH to disarm the cube loop without closing the orchestrator.
- Close TBH at any time. Orchestrator notes the chest_state child will exit
  (TBH window gone) and keeps polling the cube status.

## Safety nets

- **F8 in TBH = cube arm/disarm.** Single key, single purpose. Press it again
  to stop. Pressing F8 does not stop the chest bot.
- **Ctrl-C in orchestrator terminal = stop chest clicks + stop observing the
  cube.** Does not touch TBH or unload the plugin.
- **MaxGrade=2.** The plugin refuses to synthesize any cube slot holding an
  item above RARE grade. If you drop a LEGENDARY into the cube, the loop
  clears the cube instead of synthesizing. This is the "smart" safety net.
- **CycleIntervalSeconds=300.** Even when armed, the loop sleeps 5 minutes
  between cycles.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `[boot] FAIL: cube plugin status file not found` | BepInEx didn't load the plugin | Check `BepInEx\Log\*.log` for `[Error]` or `[Warning]` lines. The plugin logs `"TBH Auto Synthesis 0.24.0 [next/resilient]:"` on success. |
| `[boot] FAIL: TaskBarHero.exe is not running` | TBH closed or never launched | Start TBH and wait 10s before running the orchestrator. |
| `chest_state` child exits with non-zero rc | TBH window closed or never opened | Orchestrator logs and continues; restart it manually. |
| Cube plugin armed but `cycles` not advancing | Cube panel was closed | Open the Cube panel and press F8 again. The plugin does not auto-open the panel. |
| Wants to revert to preschian's defaults (`AutoStart=true`, `AutoOpenCube=true`) | You don't want this — you reported "spam clicks" as unwanted. | Don't edit the config. If you really want it, edit `BepInEx\config\com.pres.tbh.autosynth.cfg` and restart TBH. |

## Going unattended (later — only after 5+ verified hand-runs)

The orchestrator does not implement unattended mode. If you decide later that
you want it, the path is:

1. Show me 5+ runs of `py orchestrator.py --click` with successful cycles
   logged. Confirm no false-positive cubes (e.g. nothing above RARE got
   mistakenly synthesized).
2. We discuss unattended options: a Windows Task Scheduler entry, a tray app,
   etc. I won't ship unattended mode without that data.

I will not ship unattended mode before seeing that data.
