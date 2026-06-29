r"""chest_state.py — open chest drops gated by TBH meter's live.json state.

State-driven MVP. ~390 lines, stdlib + ctypes only.

INPUT  : <meter-dir>/live.json (rewritten every ~1s by tbh-meter)
STATE  : drops[3] = [monster, boss, actboss], each binary 0/1
         0 = no chest of this tier sitting
         1 = chest of this tier is unopened
ACTION : while ANY drops[N] is 1, click (533, 744) window-relative every
         1s; stop when drops returns to [0,0,0]. First click per drop
         waits 1.5s for the chest animation to settle; subsequent clicks
         are immediate (chest is already on screen, just retrying).

User rule: "as long as the binary is 1 we have to click until status
changes back to [0,0,0]" — no wasted clicks between drops, just enough
clicks while a chest is on screen to handle animation latency.

Multi-monitor: includes MOUSEEVENTF_VIRTUALDESKTOP so clicks on monitor
2/3 land correctly (without it, SendInput clips to primary).
Refresh: stat() mtime check every --poll seconds (default 1.0s matching
the meter's measured 1s cadence). JSON only re-parses on actual rewrite.

"Many clicks for no reason" — the dumb autoclicker fires every N
seconds regardless of state. This bot fires only while a chest is
visible per the meter.

USAGE (PowerShell on Windows):
    cd C:\Users\thomas\tbh-bot-mvp   (or wherever you cloned it)
    py chest_state.py                                # default dry-run
    py chest_state.py --dry-run                      # log only, no clicks
    py chest_state.py --preview                      # SetCursorPos only
    py chest_state.py --click                        # real clicks

    # Defaults assume the meter is at C:\Users\thomas\tbh-meter.
    # On a different PC, override:
    py chest_state.py --dry-run --meter-dir C:\Users\Admin\tbh-meter

Stop with Ctrl-C.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import time
from ctypes import wintypes
from pathlib import Path

# ---- click cooldown state ----------------------------------------------- #
# Per-hwnd last-click timestamp, shared across ticks. Default 2.5s between
# clicks is enough for TBH chest animations to settle without spamming.
#
# CLICK_OPEN_BLIND_S is the window during which we IGNORE the meter's
# drops[] state entirely after a click. The meter is unreliable about the
# 1->0 transition (it can hold 1 for several seconds while the chest is
# already opening). Inside the blind window we trust our click and stop
# re-clicking. Default 5s; tune upward if chests aren't all opening.
_LAST_CLICK_AT: dict[int, float] = {}
_LAST_POLL_S = 1.0
CLICK_COOLDOWN_S = 2.5
CLICK_OPEN_BLIND_S = 5.0

# ---- Win32 setup (lifted from alandsamuel's auto_click.py, Apache-2.0) --- #
u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE,
                                       wintypes.LPWSTR, wintypes.DWORD]
psapi.GetModuleFileNameExW.restype = wintypes.DWORD
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_UNITY_CLASS = "UnityWndClass"
_EXPECTED_EXE = "taskbarhero.exe"


def find_tbh_window():
    """Find TBH: Unity class AND owning process is TaskBarHero.exe."""
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    found = []

    def cb(hwnd, _lparam):
        if not u32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        if u32.GetClassNameW(hwnd, buf, 256) == 0:
            return True
        if buf.value != _UNITY_CLASS:
            return True
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not h:
                return True
            path = ctypes.create_unicode_buffer(260)
            n = psapi.GetModuleFileNameExW(h, None, path, 260)
            k32.CloseHandle(h)
            exe = path.value if n > 0 else ""
            if exe and os.path.basename(exe).lower() == _EXPECTED_EXE:
                found.append(hwnd)
                return False
        except Exception:
            pass
        return True

    u32.EnumWindows(EnumProc(cb), 0)
    return found[0] if found else None


def window_rect(hwnd):
    rect = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError(f"GetWindowRect failed for hwnd={hwnd}")
    return rect.left, rect.top, rect.right, rect.bottom


def monitor_bounds():
    """Enumerate all monitors via EnumDisplayMonitors.

    Returns list of (L, T, R, B). Use to validate that a click coord is on
    a real monitor before sending SendInput.
    """
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

    bounds = []

    def cb(_h, _dc, rect_ptr, _data):
        bounds.append((rect_ptr.contents.left, rect_ptr.contents.top,
                        rect_ptr.contents.right, rect_ptr.contents.bottom))
        return True

    u32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return bounds


def point_on_any_monitor(x, y):
    """True iff (x, y) is inside at least one monitor's bounds."""
    for L, T, R, B in monitor_bounds():
        if L <= x < R and T <= y < B:
            return True
    return False


def validate_click_coord(hwnd, wx, wy):
    """Return (abs_x, abs_y) if a click at (wx, wy) in window lands on a real monitor.

    Raises RuntimeError if the window rect is degenerate or the click coord
    is off every monitor. Catches the "TBH is on a disconnected / hidden
    monitor" case before SendInput fires into nowhere.
    """
    L, T, _, _ = window_rect(hwnd)
    ax, ay = L + wx, T + wy
    if not point_on_any_monitor(ax, ay):
        raise RuntimeError(
            f"click ({ax},{ay}) off all monitors (window rect={L},{T},"
            f" target=({wx},{wy})) — refusing to click. "
            f"Move TBH onto a visible monitor and re-run."
        )
    return ax, ay


# ---- SendInput (lifted from alandsamuel's click.py, Apache-2.0) ----------- #
# 0x8000 = MOUSEEVENTF_ABSOLUTE
# 0x4000 = MOUSEEVENTF_VIRTUALDESKTOP. Required for multi-monitor (without
#          it, SendInput clips to the primary monitor and ignores monitor 2+).
#          On single-monitor systems (monitors==1) it's harmless but unnecessary;
#          we drop it via ClickConfig to keep math simpler there.
INPUT_MOUSE = 0
_MOVE_ABS = 0x0001 | 0x8000
_DOWN_ABS = 0x0002 | 0x8000
_UP_ABS = 0x0004 | 0x8000


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", _MOUSEINPUT)]


def _send(flags, x, y):
    inp = _INPUT(INPUT_MOUSE, _MOUSEINPUT(x, y, 0, flags, 0, None))
    if u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) != 1:
        raise RuntimeError("SendInput failed")


def click_abs(ax, ay, jitter=3, hold_ms=70, virtual_desktop=True):
    """Left-click at absolute pixel (ax, ay). Light humanization.

    Multi-monitor: pass virtual_desktop=True with SM_CXVIRTUALSCREEN divisor.
    Single-monitor: pass virtual_desktop=False with SM_CXSCREEN divisor --
                    simpler mapping, and avoids the rare case where
                    SM_CXVIRTUALSCREEN returns 0 on some single-monitor
                    configs (which the previous fallback masked but didn't
                    fully resolve).
    """
    jx = ax + random.randint(-jitter, jitter)
    jy = ay + random.randint(-jitter, jitter)
    if virtual_desktop:
        vsx = u32.GetSystemMetrics(76)   # SM_CXVIRTUALSCREEN
        vsy = u32.GetSystemMetrics(77)   # SM_CYVIRTUALSCREEN
        if vsx <= 0 or vsy <= 0:
            # Fallback path for VM/RDP/some-GPU-drivers: virtual-screen
            # returns 0 even on single-monitor systems.
            vsx = u32.GetSystemMetrics(0) or 1920
            vsy = u32.GetSystemMetrics(1) or 1080
        flags_move, flags_down, flags_up = _MOVE_ABS | 0x4000, _DOWN_ABS | 0x4000, _UP_ABS | 0x4000
    else:
        vsx = u32.GetSystemMetrics(0) or 1920   # SM_CXSCREEN
        vsy = u32.GetSystemMetrics(1) or 1080   # SM_CYSCREEN
        flags_move, flags_down, flags_up = _MOVE_ABS, _DOWN_ABS, _UP_ABS
    abs_x = int(jx * 65535 / vsx)
    abs_y = int(jy * 65535 / vsy)
    _send(flags_move, abs_x, abs_y)
    time.sleep(0.04 + random.random() * 0.06)
    _send(flags_down, abs_x, abs_y)
    time.sleep(hold_ms / 1000.0)
    _send(flags_up, abs_x, abs_y)


# ---- live.json tail + state-driven edge detection ------------------------ #
def read_live_drops(meter_dir: Path, log_fh=None):
    """Read meter/live.json, return (drops_tuple, mtime, run) or (None, mtime, None).

    drops_tuple is a 3-tuple of int counts -- the meter's "chests dropped this run,
    per tier" field. mtime is the file's last-modified timestamp; we use it to skip
    re-parsing JSON when nothing changed. run is the current run id, used to detect
    run boundaries.
    """
    path = meter_dir / "live.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None, 0.0, None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        drops = d.get("drops")
        run = d.get("run")
        if isinstance(drops, list) and len(drops) == 3:
            return (tuple(int(x) for x in drops), mtime, run)
    except (json.JSONDecodeError, OSError):
        pass
    return None, mtime, None


def tick(meter_dir, hwnd, wx, wy, click_mode, prev_state, log_fh, virtual_desktop=True):
    """One poll. Returns (new_drops, new_mtime).

    State machine: drops[] is a *count* of chests dropped this run, per tier.
      drops = [a, b, c] = [monster_count, boss_count, actboss_count]
    Counter rises monotonically within a run, flushes back to [0,0,0] on
    run end (visible as `run` field change in live.json or a [N,N,N]->[0,0,0]
    transition in drops[]).

    Action: any per-tier rising edge within the current run -> click once.

    Out-of-run reset rule:
      If drops[] just hit [0,0,0] (run ended), or the run field advanced,
      adopt [0,0,0] as new baseline, no click.

    No cooldown, no blind window. The count semantics make those unnecessary:
    each chest dropped this run produces exactly one click. If we missed a
    click on a dropped chest, re-clicking on the next rising edge is fine.
    """
    cur, mtime, cur_run = read_live_drops(meter_dir, log_fh)

    if mtime == 0.0:
        return prev_state

    prev_drops, prev_mtime, prev_run = prev_state

    # No new JSON write since last poll: skip work.
    if prev_drops is not None and mtime == prev_mtime:
        return prev_state

    if cur is None:
        return (prev_drops, mtime, prev_run)

    # Run boundary detection: drops flushed to [0,0,0], or run field changed.
    # In either case the previous run's tally is gone; treat cur as fresh
    # baseline and do NOT click.
    run_changed = (cur_run is not None and prev_run is not None
                   and cur_run != prev_run)
    if run_changed:
        msg = (f"[state] new run detected (run={prev_run} -> {cur_run}), "
               f"resetting baseline from {list(prev_drops) if prev_drops else '?'} "
               f"to {list(cur)}")
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()
        return (cur, mtime, cur_run)

    if prev_drops is None:
        msg = (f"[state] baseline drops={list(cur)} (run={cur_run}, count of chests this run)"
               if cur_run is not None
               else f"[state] baseline drops={list(cur)} (count of chests this run)")
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()
        return (cur, mtime, cur_run)

    if list(cur) == [0, 0, 0] and any(p > 0 for p in prev_drops):
        msg = (f"[state] drops flushed to [0,0,0] (was {list(prev_drops)}) -- "
               f"run-end reset, no click")
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()
        return (cur, mtime, cur_run)

    # Detect rising edges: any tier with cur[i] > prev[i] = a new chest dropped.
    any_rise = any(cur[i] > prev_drops[i] for i in range(3))
    if not any_rise:
        # Pure noiseless update (mtime tick but no event) -- log minimal info.
        return (cur, mtime, cur_run)

    # Compute click coord from current window position.
    try:
        L, T, _, _ = window_rect(hwnd)
        ax, ay = L + wx, T + wy
    except RuntimeError as e:
        err = f"[click] cannot get window rect: {e}"
        print(err); log_fh.write(err + "\n"); log_fh.flush()
        return (cur, mtime, cur_run)

    tier_names = ["monster", "boss", "actboss"]
    per_tier_delta = [cur[i] - prev_drops[i] for i in range(3)]
    log_deltas = ", ".join(f"{tier_names[i]}+{per_tier_delta[i]}"
                            for i in range(3) if per_tier_delta[i] > 0)
    msg = f"[state] chest(s) dropped: {log_deltas} (drops {list(prev_drops)} -> {list(cur)}) | click ({ax},{ay})"
    print(msg); log_fh.write(msg + "\n"); log_fh.flush()

    if click_mode == "dry-run":
        pass
    elif click_mode == "preview":
        u32.SetCursorPos(ax, ay)
        msg2 = f"[preview] cursor moved to ({ax},{ay}) -- verify, then add --click"
        print(msg2); log_fh.write(msg2 + "\n"); log_fh.flush()
    elif click_mode == "click":
        try:
            click_abs(ax, ay, virtual_desktop=virtual_desktop)
        except RuntimeError as e:
            err = f"[click] failed: {e}"
            print(err); log_fh.write(err + "\n"); log_fh.flush()

    return (cur, mtime, cur_run)


def main():
    ap = argparse.ArgumentParser(
        description="State-driven TBH chest opener. Click as soon as drops[N] flips 0->1.")
    ap.add_argument("--meter-dir", default=r"C:\Users\thomas\tbh-meter",
                    help="Path to tbh-meter dir containing live.json")
    ap.add_argument("--wx", type=int, default=533,
                    help="Window-relative X of the chest icon (default 533)")
    ap.add_argument("--wy", type=int, default=744,
                    help="Window-relative Y of the chest icon (default 744)")
    ap.add_argument("--poll", type=float, default=1.0,
                    help="Seconds between mtime stat() polls (default 1.0 — "
                         "matches tbh-meter's measured ~1s live.json rewrite cadence; "
                         "JSON only re-parses on actual file rewrite)")
    ap.add_argument("--cooldown", type=float, default=2.5,
                    help="Seconds between clicks when drops stays non-zero "
                         "(default 2.5; chest animation settle time)")
    ap.add_argument("--blind", type=float, default=5.0,
                    help="Seconds after a click during which the meter's "
                         "drops[] state is ignored entirely (default 5.0; "
                         "covers the window where the meter holds drops=1 "
                         "even though the chest is already opening)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Log decisions but do not click")
    ap.add_argument("--preview", action="store_true",
                    help="Move the real cursor (SetCursorPos) to the would-be click point "
                         "but do not click. Use to visually confirm the click coord lands "
                         "on the chest icon before going live.")
    ap.add_argument("--click", action="store_true",
                    help="Actually click (default is --dry-run).")
    ap.add_argument("--log", default="chest_state.log",
                    help="Log file path (default chest_state.log next to this script)")
    args = ap.parse_args()

    meter_dir = Path(args.meter_dir)
    log_path = Path(args.log)
    log_fh = open(log_path, "a", encoding="utf-8")

    hwnd = find_tbh_window()
    if not hwnd:
        msg = (f"[boot] TBH window not found ({_UNITY_CLASS} + {_EXPECTED_EXE}). "
               f"Is the game running and visible?")
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()
        return 1

    # Pre-validate click coord lands on a real monitor.
    try:
        L0, T0, _, _ = window_rect(hwnd)
        ax0, ay0 = validate_click_coord(hwnd, args.wx, args.wy)
    except RuntimeError as e:
        msg = f"[boot] {e}"
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()
        return 1

    # Click mode precedence: --click > --preview > default (dry-run).
    # Only one may be active.
    flags = sum(bool(getattr(args, f)) for f in ("dry_run", "preview", "click"))
    if flags > 1:
        ap.error("--dry-run, --preview, and --click are mutually exclusive")
    click_mode = "click" if args.click else ("preview" if args.preview else "dry-run")

    # Wire user --cooldown / --poll into the module-level cooldown dict
    # so the tick() branch can read them.
    global CLICK_COOLDOWN_S, _LAST_POLL_S, CLICK_OPEN_BLIND_S
    CLICK_COOLDOWN_S = args.cooldown
    CLICK_OPEN_BLIND_S = args.blind
    _LAST_POLL_S = args.poll

    # Detect monitor count for the click-math switch. monitors==1 means
    # primary-monitor-only -- skip MOUSEEVENTF_VIRTUALDESKTOP and use
    # SM_CXSCREEN. >1 requires virtual-desktop handling.
    monitor_count = len(monitor_bounds())
    _VIRTUAL_DESKTOP = (monitor_count > 1)

    boot = (f"[boot] TBH hwnd={hwnd} rect=({L0},{T0},...) "
            f"click_rel=({args.wx},{args.wy}) abs=({ax0},{ay0}) "
            f"meter_dir={meter_dir} poll={args.poll}s cooldown={args.cooldown}s "
            f"blind={args.blind}s mode={click_mode} monitors={monitor_count} "
            f"virtual_desktop={_VIRTUAL_DESKTOP}")
    print(boot); log_fh.write(boot + "\n"); log_fh.flush()

    prev_state = (None, 0.0, None)
    try:
        while True:
            prev_state = tick(meter_dir, hwnd,
                              args.wx, args.wy, click_mode, prev_state, log_fh)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\n[chest-state] stopped")
        log_fh.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
