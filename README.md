# tbh-bot-mvp

State-driven chest auto-opener for **Task Bar Hero**, gated by the
[mad-labs-org/tbh-meter](https://github.com/mad-labs-org/tbh-meter)
`live.json` writer.

Replaces a fixed-timer autoclicker with a **rising-edge detector on the
binary `drops[N]` state** carried by the meter's per-game `live.json`.
The bot clicks the chest icon the moment a chest appears and only then —
zero wasted clicks between drops.

## Why

The `alandsamuel/TBH_Task-Bar-Hero_Bot` works but fires a fixed coord
every N seconds. That's "many clicks for no reason" — every 15 s
whether or not a chest is on screen. This bot waits for the meter's
state to flip `0 → 1` and clicks once. Result:

| | Timer-based (`auto_click.py`) | State-driven (this bot) |
|---|---|---|
| Clicks per real drop | 3-6 (1 every 15 s) | 1 |
| Coord source | hard-coded perma (533, 744) | same, plus virtual-screen math |
| Cross-monitor support | clips to primary | uses `MOUSEEVENTF_VIRTUALDESKTOP` |
| Reaction latency | up to 15 s | `live.json` mtime → ≤ poll interval |
| Detection signal | none, just a timer | meter's `drops[3]` per-tier binary state |

## How it works

```
tbh-meter/reader  →  rewrites live.json every ~1s (drops:[a,b,c] per tier, binary)
   ↓
chest_state.py    →  stat() polls live.json every 1.0s (matches meter's write cadence)
   ↓ (mtime changed)
re-parse JSON
   ↓
if drops has any 1:  wait 1.5s (drop animation), click chest icon, repeat
if drops is [0,0,0]: stop clicking
```

The bot is **read-only** with respect to the game — no `WriteProcessMemory`,
no DLL injection, no anti-cheat trip. It only synthesizes mouse input via
`SendInput` (the same Win32 API used by AutoHotkey).

The meter's `drops[N]` are **binary per tier**: `1` = "a chest of this
tier is currently sitting unopened," `0` = "no chest of this tier sitting."
The `1 → 0` transition happens automatically when the chest is opened
(and the loot auto-routed to inventory).

User rule the bot enforces: **as long as the binary has any 1, keep
clicking; stop when it returns to [0,0,0].** This handles animation
latency (chest takes ~1s to fully open + auto-collect) without
misclassifying slow opens as missed clicks.

## Requirements

- Windows 10 or 11 (the bot uses Win32 `SendInput`, `ctypes.windll.user32`,
  and `psapi.dll`)
- Python 3.10+ (uses `tuple[int, ...]` and `from __future__ import annotations`)
- `tbh-meter` running and writing `C:\Users\thomas\tbh-meter\live.json`
  (this matches `mad-labs-org/tbh-meter`'s default install path)
- `TaskBarHero.exe` running and visible (not minimized)
- `pip install -r requirements.txt` (one dep: `pywin32` is **not** required —
  this is pure stdlib + `ctypes`)

## Setup

```powershell
git clone https://github.com/<you>/tbh-bot-mvp
cd tbh-bot-mvp
# Make sure C:\Users\thomas\tbh-meter\live.json is being written:
Get-Content C:\Users\thomas\tbh-meter\live.json | ConvertFrom-Json | Select-Object drops, gold_now, run
```

## Usage

The bot has three operating modes — `--preview` and `--click` are
real; default is `--dry-run` (log only, never click).

### 1. Dry run first (safe)

```powershell
py chest_state.py --dry-run
```

Log + `chest_state.log` will show baseline state plus every drop event
as it happens. **No clicks are sent.** Run for ~5 minutes to confirm
the bot detects your chest drops.

### 2. Preview the click target (visual confirm)

```powershell
py chest_state.py --preview
```

When a chest is detected, the **real cursor moves** to the would-be
click coordinate via `SetCursorPos`. **No click is sent.**
Glance at your screen — does the cursor land on the chest icon?
If yes → proceed to step 3. If not → adjust `--wx` / `--wy` to match
your window's current layout, or move the game window to the same spot
where you originally recorded `(533, 744)`.

### 3. Live clicks

```powershell
py chest_state.py --click
```

Bot now clicks for real. Stop with `Ctrl-C`.

## CLI

```
py chest_state.py [--dry-run | --preview | --click]
                  [--meter-dir C:\Users\thomas\tbh-meter]
                  [--wx 533] [--wy 744]
                  [--poll 0.25]
                  [--log chest_state.log]
```

| flag | default | meaning |
|---|---|---|
| `--dry-run` / `--preview` / `--click` | `--dry-run` | mutually exclusive. `--click` actually clicks |
| `--meter-dir` | `C:\Users\thomas\tbh-meter` | path to the meter install |
| `--wx`, `--wy` | `533`, `744` | window-relative chest-icon coords |
| `--poll` | `1.0` | seconds between `live.json` mtime checks (matches meter's measured 1s cadence) |
| `--log` | `chest_state.log` | path to the human-readable event log |

## How the state machine reads (binary per tier)

The meter's `live.json` exposes:

```json
{
  "run": 875,
  "stageKey": 3206,
  "drops": [0, 0, 0],
  ...
}
```

`drops[0]` (monster-tier), `drops[1]` (boss-tier), `drops[2]`
(actboss-tier) are each **binary**:

- `0` — no chest of that tier sitting on the ground right now
- `1` — a chest of that tier is sitting there, not yet opened

A run looks like:

```
t=0    drops=[0,0,0]                      ← baseline
t=180s drops=[1,0,0]                      ← monster chest dropped  → CLICK
t=181s drops=[0,0,0]                      ← chest opened (or auto-collected) → no click on reset
```

Verified across `tbh-meter/raw/1782*.json` (recent closed runs):
mean drops-per-run ≈ 1.6, mean run duration 270 s.

Meter write cadence measured live (30 s sample, 30 distinct writes):
**median 1.013 s between writes, range 0.822-1.058 s.**

## Verification log — what to look for

When you run `--dry-run`, you should see lines like:

```
[boot] TBH hwnd=... rect=(...) monitors=2 mode=dry-run
[state] baseline drops=[0, 0, 0] (binary per tier: 0=empty, 1=chest waiting)
[state] monster chest now visible (drops=[1, 0, 0]) | wait 1.5s, click (AX, AY)
[state] drops [1, 0, 0] -> [0, 0, 0] (run boundary or chest auto-collected, no click)
```

If `[state]` lines never show "chest now visible", your meter isn't
writing `live.json` (check `Get-Process tbh-reader` first, then
`Get-Content C:\Users\thomas\tbh-meter\updater.log`).

If `monitors=1` but TBH is on a second monitor, the click might miss
the actual game window. The bot refuses to click if the would-be
absolute coord lands off every monitor.

## Files

| file | purpose |
|---|---|
| `chest_state.py` | the entire bot (~360 lines, stdlib + ctypes only) |
| `requirements.txt` | (empty — no third-party packages) |
| `LICENSE` | MIT (this project); alandsamuel code lifted per their Apache-2.0 |
| `.gitignore` | ignores `*.log`, `__pycache__/`, `.venv/` |

## License & attribution

This project: **MIT**.

Code lifted from upstream projects (lifted snippets attributed inline):

- `alandsamuel/TBH_Task-Bar-Hero_Bot` — `find_tbh_window()`,
  `click_abs()`, `SendInput` ctypes bindings. Apache-2.0.
- The state-detection model (`drops[N] == 1`) is the meter's contract,
  not a code reuse — see
  [mad-labs-org/tbh-meter `docs/`](https://github.com/mad-labs-org/tbh-meter).

## What's NOT here

- AFK reward claim (different signal — needs detection on the
  `OfflineReward` panel, not `drops[]`)
- Cube synthesis (per-stage loop, different file: `tbh-meter/raw/<run>.json`)
- A second monitor guard beyond "refuse to click off every monitor"
- Unattended-execution safety: the bot runs while you're at the PC.
  Don't leave it unattended without first verifying multiple drops
  in `--dry-run` and one drop in `--click`.
