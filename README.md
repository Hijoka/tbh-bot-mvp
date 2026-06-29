# tbh-bot-mvp

Memory-driven chest auto-opener for **Task Bar Hero**. Reads
`BoxObtain - BoxOpen` from the game's own process memory and clicks the
chest icon the moment a new chest appears.

Replaces a fixed-timer autoclicker (the `alandsamuel/TBH_Task-Bar-Hero_Bot`
original) with a **rising-edge detector on the live chest count** read
straight from the IL2CPP runtime via a vendored reader.

## Why

The original `alandsamuel/TBH_Task-Bar-Hero_Bot` fires a fixed coordinate
every N seconds. That's "many clicks for no reason" — every 15 s whether
or not a chest is on screen. This bot waits for the game's own chest
counters to advance and clicks once per new chest. Result:

| | Timer-based (`alandsamuel/.../chest_mvp.py`) | Memory-driven (this bot) |
|---|---|---|
| Clicks per real drop | 3-6 (1 every 15 s) | 1 |
| Detection signal | wall-clock timer | `BoxObtain - BoxOpen` from game memory |
| Cross-monitor support | clips to primary | uses `MOUSEEVENTF_VIRTUALDESKTOP` |
| Reaction latency | up to 15 s | ≤ poll interval (default 0.5 s) |
| Robust to game restart | timer keeps firing, blind | re-attaches on pid change |
| Robust to wrong build | no warning | reader fails fast with a clear log line |

## How it works

```
TaskBarHero.exe process memory
  ↓ (read-only attach: PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)
memory_attach.py → GameAssembly.dll + TypeInfoTable(RVA) + AggregateManager klass
  ↓ (read two EAggregateType cells per tick)
BoxObtain  = cumulative chests dropped (this profile, ever)
BoxOpen    = cumulative chests opened  (this profile, ever)
pending    = BoxObtain - BoxOpen       = chests waiting to be opened
  ↓
chest_state.py → poll pending every --poll s
  ↓
if pending rose  → click chest icon once
if pending fell  → log "chest opened" (click confirmed)
if pending same  → skip
```

The reader attaches on first call (a few seconds while the IL2CPP
TypeInfoTable is resolved via a fixed RVA + TypeDefIndex) and reuses
the same process handle for the bot's lifetime. It re-attaches
automatically if the game's pid changes (i.e. you restarted Task Bar
Hero while the bot was running).

The bot is **read-only** with respect to the game — no
`WriteProcessMemory`, no DLL injection, no anti-cheat trip. It only
synthesizes mouse input via `SendInput` (the same Win32 API used by
AutoHotkey).

User rule the bot enforces: **click when the count rises, log when it
falls, otherwise stay quiet.** The pending counter is exact: each new
chest produces exactly one click, and each click is confirmed by a
matching fall in the count. There is no cooldown or "blind window"
needed — the count is the truth, not an estimate.

## Requirements

- Windows 10 or 11 (the bot uses Win32 `SendInput`, `ctypes.windll.user32`,
  and `psapi.dll`)
- Python 3.10+ (uses `int | None` and `from __future__ import annotations`)
- `TaskBarHero.exe` running and visible (not minimized)
- Build **v1.00.21** (the calibrated seed in `memory_attach.py` matches
  this build — GameAssembly.dll RVA `0x6173D90`, AggregateManager
  TypeDefIndex `2827`. Other builds will fail to resolve the class and
  log `[state] memory read failed`)
- `pip install -r requirements.txt` — **no third-party packages**, this
  is pure stdlib + `ctypes`

## Setup

```powershell
git clone https://github.com/<you>/tbh-bot-mvp
cd tbh-bot-mvp
# Make sure TaskBarHero.exe is running and visible.
# That is the only thing the bot needs — no meter, no live.json.
```

## Usage

The bot has three operating modes — `--preview` and `--click` are real;
default is `--dry-run` (log only, never click).

### 1. Dry run first (safe)

```powershell
py chest_state.py --dry-run
```

Log + `chest_state.log` will show the attach line, the initial
pending count, and every drop event as it happens. **No clicks are
sent.** Run for ~5 minutes to confirm the bot detects your chest
drops.

The first memory read takes a few seconds while the reader attaches
(this happens once, on boot, and is logged as `[boot] memory reader
attached`). Subsequent reads are <10 ms.

### 2. Preview the click target (visual confirm)

```powershell
py chest_state.py --preview
```

When a chest is detected, the **real cursor moves** to the would-be
click coordinate via `SetCursorPos`. **No click is sent.** Glance at
your screen — does the cursor land on the chest icon? If yes → proceed
to step 3. If not → adjust `--wx` / `--wy` to match your window's
current layout.

### 3. Live clicks

```powershell
py chest_state.py --click
```

Bot now clicks for real. Stop with `Ctrl-C`.

## CLI

```
py chest_state.py [--dry-run | --preview | --click]
                  [--wx 533] [--wy 744]
                  [--poll 0.5]
                  [--log chest_state.log]
```

| flag | default | meaning |
|---|---|---|
| `--dry-run` / `--preview` / `--click` | `--dry-run` | mutually exclusive. `--click` actually clicks |
| `--wx`, `--wy` | `533`, `744` | window-relative chest-icon coords |
| `--poll` | `0.5` | seconds between memory reads. First call takes a few seconds while the reader attaches; later calls are <10 ms |
| `--log` | `chest_state.log` | path to the human-readable event log |

## What the memory read gives us

`pending` is a single integer that combines two truths from the game
itself (lifted from `alandsamuel/TBH_Task-Bar-Hero_Bot`):

```python
# inside memory_attach.py — lifted from alandsamuel's chest_mvp.py
ob = self.read_aggregate(EAggregateType.BoxObtain)   # cumulative chests dropped
op = self.read_aggregate(EAggregateType.BoxOpen)     # cumulative chests opened
pending = max(0, ob - op)                            # chests waiting
```

`BoxObtain` and `BoxOpen` are **session-total counters**: monotonic
within a profile, no resets on run boundaries, no per-run count to
confuse us. `pending` is the exact number of chests sitting on the
ground waiting to be opened. The bot's job reduces to: *count rising
edge → click once; count falling edge → log; otherwise nothing*.

## Verification log — what to look for

When you run `--dry-run`, you should see lines like:

```
[boot] TBH hwnd=... rect=(...) monitors=2 mode=dry-run reader=memory(BoxObtain-BoxOpen); first read attaches the process (a few seconds)...
[boot] memory reader attached; initial pending=0
[state] baseline pending=0 (BoxObtain - BoxOpen from game memory)
[state] +1 chest(s) dropped (pending 0 -> 1) | click (AX, AY)
[state] -1 chest(s) opened (pending 1 -> 0) | click confirmed
```

If `[boot] memory reader attached` never appears, the game isn't
running or you ran the wrong build (the seed is for v1.00.21).

If `[state] memory read failed` repeats, the reader can't resolve
`AggregateManager` — check the build version.

If `monitors=1` but TBH is on a second monitor, the click might miss
the actual game window. The bot refuses to click if the would-be
absolute coord lands off every monitor.

## Files

| file | purpose |
|---|---|
| `chest_state.py` | the bot: window find + memory poll + click (~300 lines, stdlib + ctypes only) |
| `memory_attach.py` | thin wrapper over the vendored reader: `get_pending(hwnd) -> int \| None` |
| `vendor/` | alandsamuel's reader, lifted verbatim (see NOTICE in `memory_attach.py`) |
| `requirements.txt` | (empty — no third-party packages) |
| `LICENSE` | MIT (this project); vendored reader under Apache-2.0 |
| `.gitignore` | ignores `*.log`, `__pycache__/`, `.venv/` |

## License & attribution

This project: **MIT**.

Vendored reader (under `vendor/`) and the lifted `GameAttach` /
`pending_chests()` code in `memory_attach.py`:

- `alandsamuel/TBH_Task-Bar-Hero_Bot` — process attach, IL2CPP
  TypeInfoTable resolution, AggregateManager reads. Apache-2.0.
  See the NOTICE block at the top of `memory_attach.py` and the
  Apache-2.0 attribution in `LICENSE`.

Lifted (not vendored) snippets in `chest_state.py`:

- `find_tbh_window()` — window-find by Unity class + exe name.
- `click_abs()` + `SendInput` ctypes bindings + multi-monitor math.
  Apache-2.0.

## What's NOT here

- AFK reward claim (different signal — needs detection on the
  `OfflineReward` panel, not chest counters)
- Cube synthesis (per-stage loop, needs a different file)
- A second monitor guard beyond "refuse to click off every monitor"
- Unattended-execution safety: the bot runs while you're at the PC.
  Don't leave it unattended without first verifying multiple drops
  in `--dry-run` and one drop in `--click`.
