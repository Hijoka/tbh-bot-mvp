# tbh-bot-mvp

State-driven chest auto-opener for **Task Bar Hero**. Reads the meter
process's `live.json` (written ~1×/s) and clicks the chest icon the
moment a new chest appears.

Replaces a fixed-timer autoclicker (the original
`alandsamuel/TBH_Task-Bar-Hero_Bot`) with a **per-tier rising-edge
detector on the meter's chest-drop counter**. Result: one click per
real drop, no clicks when no chest has dropped.

## Why

The original timer-based bot fires a fixed coordinate every N seconds.
That's "many clicks for no reason" — every 15 s whether or not a chest
is on screen. This bot waits for the meter's `drops[]` counter to
advance and clicks once per tier's rising edge. On a run that drops 5
chests over 30 minutes, this bot clicks 5 times. The timer-based one
clicks ~120 times.

| | Timer-based (`alandsamuel/.../chest_mvp.py`) | This bot |
|---|---|---|
| Clicks per real drop | 3-6 (1 every 15 s) | 1 |
| Detection signal | wall-clock timer | `drops[]` per-tier rising edge from meter `live.json` |
| Reaction latency | up to 15 s | ≤ poll interval (default 0.25 s) |
| Wrong-coord risk | yes (clicks land wherever the UI is) | low (only clicks when chest detected) |
| Survives UI changes | yes | yes |
| Survives coord changes | no (re-record) | no (same) |
| Setup | standalone | needs the meter running alongside |

## How it works

```
mad-labs-org/tbh-meter running alongside the game
  ↓ (writes live.json ~1×/s; mtime fast-poll, no fsnotify)
chest_state.py → stat() every 250 ms
  ↓ (re-parse JSON only when mtime advances)
live.json["drops"] = [Monster, Boss, ActBoss] per-run counter
  ↓
per-tier rising edge (cur[i] > prev[i]) → click once
run-end reset (drops → [0,0,0] OR run field advanced) → fresh baseline, no click
no change → skip work
  ↓
SendInput left-click at (L+533, T+744) — window-relative, multi-monitor safe
```

The bot is **read-only** with respect to the game — no
`WriteProcessMemory`, no DLL injection, no anti-cheat trip. It only
synthesizes mouse input via `SendInput` (the same Win32 API used by
AutoHotkey). The meter's `live.json` is the **only** game-side data we
read; we do not touch the game process ourselves.

User rule the bot enforces: **click once per tier's rising edge, log
on run-end reset, otherwise stay quiet.** The meter's counter is exact:
each new chest produces exactly one click, and the meter holds the
count at the new value for the rest of the run (it only resets on run
end), so we never double-click.

## Requirements

- Windows 10 or 11 (the bot uses Win32 `SendInput`, `ctypes.windll.user32`)
- Python 3.10+ (uses `int | None` and `from __future__ import annotations`)
- `TaskBarHero.exe` running and visible (not minimized)
- `mad-labs-org/tbh-meter` running alongside the game, writing
  `live.json` to its default output dir (typically `~/tbh-meter/`)
- `pip install -r requirements.txt` — **no third-party packages**, this
  is pure stdlib + `ctypes`

## Setup

```powershell
git clone https://github.com/hijoka/tbh-bot-mvp
cd tbh-bot-mvp
# Make sure TaskBarHero.exe is running AND tbh-meter is running
# (the meter writes the live.json the bot reads)
```

## Usage

The bot has three operating modes — `--preview` and `--click` are real;
default is `--dry-run` (log only, never click).

### 1. Dry run first (safe)

```powershell
python chest_state.py --dry-run
```

Log + `chest_state.log` will show the boot line, the initial
`drops[]` baseline, and every drop event as it happens. **No clicks
are sent.** Run for ~5 minutes to confirm the bot detects your chest
drops.

### 2. Preview the click target (visual confirm)

```powershell
python chest_state.py --preview
```

When a chest is detected, the **real cursor moves** to the would-be
click coordinate via `SetCursorPos`. **No click is sent.** Glance at
your screen — does the cursor land on the chest icon? If yes → proceed
to step 3. If not → adjust `--wx` / `--wy` to match your window's
current layout.

### 3. Live clicks

```powershell
python chest_state.py --click
```

Bot now clicks for real. Stop with `Ctrl-C`.

## CLI

```
python chest_state.py [--dry-run | --preview | --click]
                      [--wx 533] [--wy 744]
                      [--poll 0.25]
                      [--live-json PATH]
                      [--log chest_state.log]
```

| flag | default | meaning |
|---|---|---|
| `--dry-run` / `--preview` / `--click` | `--dry-run` | mutually exclusive. `--click` actually clicks |
| `--wx`, `--wy` | `533`, `744` | window-relative chest-icon coords |
| `--poll` | `0.25` | seconds between file stat() polls. The meter writes `live.json` ~1×/s, so 0.25 gives sub-second reaction |
| `--live-json` | `~/tbh-meter/live.json` | path to the meter's live output. Override if your meter writes elsewhere |
| `--log` | `chest_state.log` | path to the human-readable event log |

## What `drops[]` means (and why rising-edge, not equality)

The meter's `live.json` contains a `drops` field that's a list of
three integers — `[Monster, Boss, ActBoss]` — counting how many chests
of each tier have dropped **in the current run**.

```
[0, 0, 0]   → start of run, no drops yet
[1, 0, 0]   → 1 Monster chest has dropped
[1, 1, 0]   → +1 Boss chest just dropped  ← rising edge on tier 1, click once
[1, 1, 0]   → same (held for the rest of the run)  ← no work
[0, 0, 0]   → run ended, meter flushed  ← fresh baseline, no click
```

The counter never decrements when you click — only resets on run end.
So the right state machine is:

- `cur[i] > prev[i]` → click once per tier's rise (a chest just dropped)
- `cur == [0,0,0]` and `prev != [0,0,0]` → run ended, fresh baseline
- `run` field advanced → also run boundary, fresh baseline
- `cur == prev` → no change, skip work

The `run` field is the meter's monotonic stage-attempt counter. It
advances at every new stage attempt; treating that as a run-end signal
handles the rare case where the meter resets `drops[]` mid-run.

Auto-collected chests still get clicked: the meter counts *drops*, not
opens. So even if a chest auto-collects before our click, the rising
edge fired and we click once.

## Verification log — what to look for

When you run `--dry-run`, you should see lines like:

```
[boot] TBH hwnd=... rect=(...) click_rel=(533,744) abs=(...) poll=0.25s mode=dry-run monitors=1 reader=live_json(drops[]); source=C:\Users\Admin\tbh-meter\live.json
[state] baseline drops=[0, 0, 0] run=141 (from C:\Users\Admin\tbh-meter\live.json)
[state] +1 chest(s) dropped (drops [0, 0, 0] -> [1, 0, 0], tiers=[Monster+1]) | click (1030,756) x1
[state] run boundary (drops flushed to [0,0,0]); prev=[3, 1, 0] run=141 -> cur=[0, 0, 0] run=142; fresh baseline, no click
```

If `[boot] live.json not found at ...` appears, the meter's output
directory is different on this machine. Run `--live-json <correct-path>`
to override.

If `[boot] TBH window not found` appears, the game isn't running or
visible (minimized windows aren't picked up by `EnumWindows`).

If `monitors=1` but TBH is on a second monitor, the click might miss
the actual game window. The bot refuses to click if the would-be
absolute coord lands off every monitor.

## Files

| file | purpose |
|---|---|
| `chest_state.py` | the bot: window find + live.json poll + click (~480 lines, stdlib + ctypes only) |
| `memory_attach.py` | (legacy) lifted wrapper over the vendored reader. **Not used** by `chest_state.py` — kept as reference for future memory-read paths |
| `vendor/` | alandsamuel's reader (Apache-2.0, kept for attribution and as a fallback if `live.json` ever stops working) |
| `_diag.py`, `_diag2.py`, `_diag3.py` | (legacy) step-by-step probes used to discover that `BoxObtain` is missing on this build. **Not used** at runtime. Kept as debugging history |
| `probe_pending.py` | (legacy, broken stub) — predates this repo's structure. Safe to delete |
| `requirements.txt` | (empty — no third-party packages) |
| `LICENSE` | MIT (this project); vendored reader under Apache-2.0 |

## Why we don't read `BoxObtain` directly from memory

The natural memory-read path would be `BoxObtain - BoxOpen` from the
game's `AggregateManager` — both are defined in the EAggregateType
enum. **On this build (`v1.00.21`), `EAggregateType.BoxObtain=3` is not
present in the AggregateManager outer dict.** Verified by walking the
outer dict (`_diag3.py`): only keys `[0, 2, 4, 5, 7, 10, 15, 16]` are
populated. `3` is missing entirely (sparse dict, never incremented for
this user).

The meter's own source (`taskhero-engine/memory_reader/reader.py:8`)
confirms this in its docstring:

> *"BoxObtain (EAggregateType=3) is NOT in AggregateManager outer dict.
> We use PlayerSaveData.BoxData.BoxUniqueId.count() for pending chests."*

So the meter falls back to a different path (`PlayerSaveData.BoxData.BoxUniqueId.count()`)
that we don't currently have calibrated. The meter's `live.json`
`drops[]` is the next-cleanest signal, and the meter is already
running on the botting PC.

If a future TBH patch re-introduces `BoxObtain` to the outer dict, this
bot can be extended to read it directly. Re-verify with `_diag3.py`
before assuming the path is back.

## License & attribution

This project: **MIT**.

Vendored reader (under `vendor/`) and the lifted `GameAttach` /
`pending_chests()` code in `memory_attach.py`:

- `alandsamuel/TBH_Task-Bar-Hero_Bot` — process attach, IL2CPP
  TypeInfoTable resolution, AggregateManager reads. Apache-2.0.
- `mad-labs-org/tbh-meter` — the meter whose `live.json` we consume.
  See the data contract at
  `https://github.com/mad-labs-org/tbh-meter/blob/main/docs/process/data-contract-id-based.md`.

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
- Reading `BoxObtain` directly. See "Why we don't read `BoxObtain`
  directly from memory" above for the build-specific reason.