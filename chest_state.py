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
# 0x4000 = MOUSEEVENTF_VIRTUALDESKTOP (REQUIRED for multi-monitor -- without it,
#                                     SendInput clips to the primary monitor
#                                     and ignores monitor-2/3 coords)
INPUT_MOUSE = 0
_ABS_VIRT = 0x8000 | 0x4000
_MOVE = 0x0001 | _ABS_VIRT
_DOWN = 0x0002 | _ABS_VIRT
_UP = 0x0004 | _ABS_VIRT


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


def click_abs(ax, ay, jitter=3, hold_ms=70):
    """Left-click at absolute VIRTUAL-SCREEN pixel (ax, ay). Light humanization.

    Uses SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN so coords spanning multiple
    monitors map correctly. Pair with MOUSEEVENTF_VIRTUALDESKTOP (set above)
    or SendInput will silently clip to the primary monitor.

    Falls back to SM_CXSCREEN / SM_CYSCREEN (primary monitor metrics) when
    the virtual screen metrics are 0 -- happens in some single-monitor
    configurations, inside RDP/VM sessions, and on a few GPU drivers.
    """
    jx = ax + random.randint(-jitter, jitter)
    jy = ay + random.randint(-jitter, jitter)
    vsx = u32.GetSystemMetrics(76)   # SM_CXVIRTUALSCREEN
    vsy = u32.GetSystemMetrics(77)   # SM_CYVIRTUALSCREEN
    if vsx <= 0 or vsy <= 0:
        # Fall back to primary-monitor metrics. Virtual-screen returns 0
        # on some single-monitor configs -- without this, divide-by-zero.
        vsx = u32.GetSystemMetrics(0) or 1920   # SM_CXSCREEN
        vsy = u32.GetSystemMetrics(1) or 1080   # SM_CYSCREEN
    abs_x = int(jx * 65535 / vsx)
    abs_y = int(jy * 65535 / vsy)
    _send(_MOVE, abs_x, abs_y)
    time.sleep(0.04 + random.random() * 0.06)
    _send(_DOWN, abs_x, abs_y)
    time.sleep(hold_ms / 1000.0)
    _send(_UP, abs_x, abs_y)


# ---- live.json tail + state-driven edge detection ------------------------ #
def read_live_drops(meter_dir: Path, log_fh=None):
    """Read meter/live.json, return (drops_tuple, mtime) or (None, mtime).

    Returning mtime even on read-failure lets the caller cheap-poll every
    --poll seconds but only re-parse JSON when the meter actually wrote.
    Drops 6s-of-blink lag to whatever the cadence of file stat() is.
    """
    path = meter_dir / "live.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None, 0.0
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        drops = d.get("drops")
        if isinstance(drops, list) and len(drops) == 3:
            return tuple(int(x) for x in drops), mtime
    except (json.JSONDecodeError, OSError):
        pass
    return None, mtime


def tick(meter_dir, hwnd, wx, wy, click_mode, prev_state, log_fh):
    """One poll. Returns (new_drops_or_None, new_mtime).

    click_mode is one of:
      'dry-run'  -- log only, no cursor move, no click
      'preview'  -- SetCursorPos to target (real cursor moves, but no click)
      'click'    -- SendInput left-click at target

    State machine (binary per tier):
      0  = no chest of this tier sitting on the ground
      1  = chest of this tier is sitting, not yet opened

    Action: any 0 -> 1 transition -> click (533, 744) after a 1.5s settle.
            Any 1 -> 0 transition (chest opened / run reset): no click, log only.

    Why a tuple state instead of "rising-edge counter": the meter holds
    drops[N] = 1 the whole time the chest is visible, flips to 0 when
    opened. A counter rising-edge would miss chests that reset to baseline
    on stage clear before the bot polls.
    """
    cur, mtime = read_live_drops(meter_dir, log_fh)

    # No mtime yet -> file unreadable, do nothing.
    if mtime == 0.0:
        return prev_state

    prev_drops, prev_mtime = prev_state

    # First-ever read or mtime advanced: re-parse JSON.
    if prev_drops is None or mtime != prev_mtime:
        # File rewritten by meter; emit fresh snapshot.
        pass
    else:
        # Identical mtime -- nothing changed since last poll.
        return prev_state

    if cur is None:
        return (prev_drops, mtime)

    # Run boundary: every tier flipped N -> 0 simultaneously.
    if prev_drops is not None and any(prev_drops[i] > cur[i] for i in range(3)):
        msg = (f"[state] drops {list(prev_drops)} -> {list(cur)} "
               f"(run boundary or chest auto-collected, no click)")
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()
        return (cur, mtime)

    # Click on any per-tier 0 -> 1 transition AND keep clicking every poll
    # until the drops tuple returns to [0,0,0]. The user's rule:
    #   "as long as the binary is 1 we have to click until status changes
    #    back to [0,0,0]"
    # Each click hits the chest that the meter is still reporting as 1,
    # which means there is always a chest on screen to click -- no wasted
    # clicks, just idempotent retries until the chest auto-collects.
    tier_names = ["monster", "boss", "actboss"]
    any_tier_one = any(c == 1 for c in cur)
    was_any_one = (prev_drops is not None
                   and any(prev_drops[j] == 1 for j in range(3)))

    if any_tier_one:
        # First we try to compute coords. If window moved off monitors we
        # log and don't click this iteration; will retry on next mtime change.
        try:
            L, T, _, _ = window_rect(hwnd)
            ax, ay = L + wx, T + wy
        except RuntimeError as e:
            err = f"[click] cannot get window rect: {e}"
            print(err); log_fh.write(err + "\n"); log_fh.flush()
            return (cur, mtime)

        if not was_any_one:
            # Just transitioned from [0,*,*] to having any 1. Sleep once
            # to let the drop animation settle, then click.
            msg = (f"[state] drops became non-zero (drops={list(cur)}) | "
                   f"wait 1.5s, click ({ax},{ay}); will keep clicking "
                   f"until drops==[0,0,0]")
            print(msg); log_fh.write(msg + "\n"); log_fh.flush()
            time.sleep(1.5)
        else:
            # Still 1 from last poll -- we keep clicking without delay,
            # since the chest is already on screen and we're just retrying.
            msg = (f"[state] drops still non-zero (drops={list(cur)}) | "
                   f"click again ({ax},{ay})")
            print(msg); log_fh.write(msg + "\n"); log_fh.flush()

        if click_mode == "dry-run":
            pass
        elif click_mode == "preview":
            u32.SetCursorPos(ax, ay)
            msg2 = f"[preview] cursor moved to ({ax},{ay}) -- visually verify, then add --click"
            print(msg2); log_fh.write(msg2 + "\n"); log_fh.flush()
        elif click_mode == "click":
            try:
                click_abs(ax, ay)
            except RuntimeError as e:
                err = f"[click] failed: {e}"
                print(err); log_fh.write(err + "\n"); log_fh.flush()

    elif was_any_one and not any_tier_one:
        # The transition 1 -> 0 we were waiting for.
        msg = f"[state] drops cleared ({list(prev_drops)} -> {list(cur)}) -- chest opened/auto-collected"
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()

    if prev_drops is None:
        msg = f"[state] baseline drops={list(cur)} (binary per tier: 0=empty, 1=chest waiting; click until [0,0,0])"
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()

    return (cur, mtime)


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

    boot = (f"[boot] TBH hwnd={hwnd} rect=({L0},{T0},...) "
            f"click_rel=({args.wx},{args.wy}) abs=({ax0},{ay0}) "
            f"meter_dir={meter_dir} poll={args.poll}s mode={click_mode} "
            f"monitors={len(monitor_bounds())}")
    print(boot); log_fh.write(boot + "\n"); log_fh.flush()

    prev_state = (None, 0.0)
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
