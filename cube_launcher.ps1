# cube_launcher.ps1 — manual first-time install + sanity check.
#
# This is NOT an auto-runner. Run this once by hand at the keyboard to set up
# the BepInEx + cube plugin install. It fails loud on any problem; do NOT
# wrap it in unattended scheduling.
#
# Usage (PowerShell on Admin, TBH closed):
#   cd C:\Users\Admin\tbh-bot-mvp
#   .\cube_launcher.ps1
#
# What it does:
#   1. Verifies TaskBarHero.exe is NOT running (or stops with a clear error).
#   2. Verifies BepInEx is installed alongside the game (BepInEx\ exists next to
#      TaskBarHero.exe with winhttp.dll present). If not, prints the URL and
#      stops — does NOT auto-download (we don't auto-install BepInEx).
#   3. Copies prebuilt\TbhAutoSynth-next.dll into BepInEx\plugins\, renaming it
#      to TbhAutoSynth.dll so the BepInEx GUID loader picks it up.
#   4. Writes the v1 config (AutoStart=false, AutoOpenCube=false, MaxGrade=2) to
#      the BepInEx config file if it doesn't exist yet. Does NOT clobber an
#      existing config — backs it up to .bak if found.
#   5. Prints "READY" and the next manual steps:
#        - Launch TBH by hand
#        - Wait 10s, check BepInEx\Log\*.log for "TBH Auto Synthesis 0.24.0 [next/resilient]:"
#        - Run: py orchestrator.py --dry-run
#        - Inspect %LOCALAPPDATA%\tbh-companion\autosynth-status.json (should appear within 3s)
#        - Then run: py orchestrator.py --click
#
# Anti-features (intentionally NOT in this script):
#   - Does NOT modify BepInEx's home folder install. That's a one-time manual step.
#   - Does NOT launch TBH. You launch TBH.
#   - Does NOT launch orchestrator. You launch orchestrator.
#   - Does NOT auto-download anything from the internet.

$ErrorActionPreference = 'Stop'

# ---- config: edit these if your install paths differ ------------------ #
# Defaults assume the repo lives next to the user's Documents. Override via
# env vars or edit the path values here.
$RepoDir    = if ($env:TBH_BOT_REPO_DIR) { $env:TBH_BOT_REPO_DIR } else { Join-Path $env:USERPROFILE 'tbh-bot-mvp' }
$GameDir    = if ($env:TBH_GAME_DIR)     { $env:TBH_GAME_DIR }     else { 'C:\Program Files\TesseractStudio\TaskBarHero' }
$PluginDll  = Join-Path $RepoDir 'prebuilt\TbhAutoSynth-next.dll'
$PluginsDir = Join-Path $GameDir 'BepInEx\plugins'
$ConfigPath = Join-Path $GameDir 'BepInEx\config\com.pres.tbh.autosynth.cfg'

# ---- step 1: TBH must not be running --------------------------------- #
$tbh = Get-Process -Name 'taskbarhero' -ErrorAction SilentlyContinue
if ($tbh) {
    Write-Host "FATAL: TaskBarHero.exe is running (PID $($tbh.Id)). Close the game and re-run." -ForegroundColor Red
    exit 2
}
Write-Host "[1/5] TaskBarHero.exe not running. OK." -ForegroundColor Green

# ---- step 2: BepInEx installed? -------------------------------------- #
if (-not (Test-Path (Join-Path $GameDir 'winhttp.dll'))) {
    Write-Host "FATAL: BepInEx not installed at $GameDir\winhttp.dll" -ForegroundColor Red
    Write-Host "Manual install (once):" -ForegroundColor Yellow
    Write-Host "  1. Download from https://builds.bepinex.dev/projects/bepinex_be (BepInEx-Unity.IL2CPP-win-x64)"
    Write-Host "  2. Extract the zip into $GameDir so winhttp.dll and BepInEx\ sit next to TaskBarHero.exe"
    Write-Host "  3. Launch TBH once, wait for BepInEx to generate interop assemblies, close TBH."
    Write-Host "  4. Re-run this script."
    exit 2
}
if (-not (Test-Path (Join-Path $GameDir 'BepInEx'))) {
    Write-Host "FATAL: BepInEx folder missing at $GameDir\BepInEx" -ForegroundColor Red
    exit 2
}
Write-Host "[2/5] BepInEx installed. OK." -ForegroundColor Green

# ---- step 3: drop the prebuilt DLL ----------------------------------- #
if (-not (Test-Path $PluginDll)) {
    Write-Host "FATAL: prebuilt DLL not found at $PluginDll" -ForegroundColor Red
    Write-Host "Download from: https://github.com/preschian/tbh-presence/releases (TbhCompanion-next.zip or autosynth/prebuilt/TbhAutoSynth-next.dll)"
    Write-Host "Save it to: $RepoDir\prebuilt\TbhAutoSynth-next.dll"
    exit 2
}
New-Item -ItemType Directory -Path $PluginsDir -Force | Out-Null
$dest = Join-Path $PluginsDir 'TbhAutoSynth.dll'
Copy-Item -Path $PluginDll -Destination $dest -Force
Write-Host "[3/5] Plugin DLL dropped at: $dest" -ForegroundColor Green

# ---- step 4: write the v1 config (opportunistic mode) ---------------- #
if (Test-Path $ConfigPath) {
    Move-Item $ConfigPath "$ConfigPath.bak" -Force
    Write-Host "[4/5] Existing config backed up to $ConfigPath.bak (not overwritten, you can diff)." -ForegroundColor Yellow
} else {
    $cfgDir = Split-Path -Parent $ConfigPath
    New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null
    @"
[Timing]
AfterFillSeconds = 1.0
AfterSynthesisSeconds = 4.0
CycleIntervalSeconds = 300

[General]
AutoStart = false
AutoOpenCube = false
SynthesisTypes = Equipment,Materials,Accessories

[Safety]
MaxGrade = 2
"@ | Set-Content -Path $ConfigPath -Encoding utf8
    Write-Host "[4/5] v1 config written (AutoStart=false, AutoOpenCube=false, MaxGrade=2)." -ForegroundColor Green
}

# ---- step 5: READY --------------------------------------------------- #
Write-Host ""
Write-Host "===== READY =====" -ForegroundColor Cyan
Write-Host "Setup complete. Manual next steps:"
Write-Host "  1. Launch TaskBarHero.exe by hand."
Write-Host "  2. Wait 10s. Check $GameDir\BepInEx\Log\*-Il2CppInterop.log"
Write-Host "     for the line: 'TBH Auto Synthesis 0.24.0 [next/resilient]:'"
Write-Host "  3. In a NEW terminal: cd $RepoDir ; py orchestrator.py --dry-run"
Write-Host "     Expect '[boot] cube plugin status file: ...' then READY. Ctrl-C to exit."
Write-Host "  4. Open the Cube panel in TBH. Press F8. Watch the orchestrator log."
Write-Host "  5. Stop the orchestrator, then: py orchestrator.py --click"
Write-Host ""
Write-Host "Killing TBH, BepInEx, or the orchestrator at any step is safe — nothing persists state."
