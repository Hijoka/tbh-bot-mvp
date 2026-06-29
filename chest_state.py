r"""chest_state.py — open chest drops gated by reading BoxObtain - BoxOpen from game memory.

State-driven MVP. ~300 lines, stdlib + ctypes only.

INPUT  : TaskBarHero.exe process memory (via vendored alandsamuel reader)
STATE  : pending = BoxObtain - BoxOpen (integer >= 0)
         0 = no chest sitting
         N = N chests waiting to be opened
ACTION : when pending RISES (drop event), click the chest icon at
         (wx, wy) window-relative. When pending FALLS (chest opened,
         click confirmed), log only. When pending is unchanged, skip.

Replaces the previous meter/live.json reader. live.json's `drops[3]` was
a per-run count, not the binary "chest on screen right now" we needed
— rising edges fired but the 1->0 transition was unreliable. The memory
read gets the source of truth from the game itself: BoxObtain and
BoxOpen are session-total counters; their difference is "chests waiting
to be opened", which is exactly what we need to gate the click on.

Read-side code (process attach + IL2CPP resolution + Dict walks) is
vendored from alandsamuel/TBH_Task-Bar-Hero_Bot (Apache-2.0). See
memory_attach.py and the NOTICE block at the top of that file.

USAGE (PowerShell on Windows):
    cd C:\Users\thomas\tbh-bot-mvp   (or wherever you cloned it)
    py chest_state.py                                # default dry-run
    py chest_state.py --dry-run                      # log only, no clicks
    py chest_state.py --preview                      # SetCursorPos only
    py chest_state.py --click                        # real clicks

    # Default poll cadence is 0.5s; first call takes a few seconds while
    # the reader attaches to the process.
    py chest_state.py --click --poll 0.5

Stop with Ctrl-C.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import random
import sys
import time
from ctypes import wintypes
from pathlib import Path

# ---- VENDORED meter reader (subset) --------------------------------------- #
# We import the meter primitives we need through the thin wrapper in
# memory_attach.py. Vendored from alandsamuel/TBH_Task-Bar-Hero_Bot
# (Apache-2.0). The `sys.path.insert(0, ".../vendor")` trick must come
# BEFORE importing memory_attach (which itself inserts vendor/ onto
# sys.path during its own import). Vendored files use bare imports like
# `from config.offsets import ...`, which depend on this sys.path layout.
sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
import memory_attach  # noqa: E402


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


# ---- memory read + state-driven edge detection --------------------------- #
def read_pending(hwnd):
    """Read BoxObtain - BoxOpen from game memory. Returns (pending, mtime) or (None, 0.0).

    `pending` is an int >= 0 (chests waiting to be opened), or None on
    read failure (game not running, attach still in progress, transient
    error, wrong build). `mtime` is time.time() at the read — used by
    the main loop for log timestamps and to skip logging on no-change
    ticks.
    """
    try:
        n = memory_attach.get_pending(hwnd)
    except Exception:
        return None, time.time()
    if n is None:
        return None, time.time()
    return int(n), time.time()


def tick(hwnd, wx, wy, click_mode, prev_pending, log_fh, virtual_desktop=True):
    """One poll. Returns the new `prev_pending` for the next tick.

    State machine: `pending` is BoxObtain - BoxOpen (cumulative chests
    dropped minus cumulative chests opened = chests waiting to be opened).

      pending > prev_pending  → a chest dropped this tick → click once
      pending < prev_pending  → a chest was opened (click confirmed) → log only
      pending == prev_pending → no state change → skip work

    The counter is monotonic-up on BoxObtain events and monotonic-up on
    BoxOpen events, so the difference `pending` is non-decreasing between
    drops, decreases by exactly 1 per click-confirmed open, and never
    resets within a profile (the only way it could go down outside a
    click is if the user opens a chest manually, which is logged too).
    """
    cur, mtime = read_pending(hwnd)

    if cur is None:
        # Transient read failure — keep prev_pending, log once per failure
        # burst would be nice but a simple line per failed tick is fine
        # (the human operator can see the cadence from the log).
        msg = "[state] memory read failed (game not running? or attach still in progress?)"
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()
        return prev_pending

    # First ever read — establish baseline. No click even if pending > 0:
    # by the time we read, the chest may already be openable; clicking
    # blindly is wasted motion. Subsequent reads will catch the change.
    if prev_pending is None:
        msg = f"[state] baseline pending={cur} (BoxObtain - BoxOpen from game memory)"
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()
        return cur

    # No change — skip
    if cur == prev_pending:
        return cur

    # Compute click coord from current window position
    try:
        L, T, _, _ = window_rect(hwnd)
        ax, ay = L + wx, T + wy
    except RuntimeError as e:
        err = f"[click] cannot get window rect: {e}"
        print(err); log_fh.write(err + "\n"); log_fh.flush()
        return cur

    if cur > prev_pending:
        # Chest(s) dropped while the bot was running. Click once per new
        # chest. The pending count IS the truth from the game; if it
        # rose by N, N chests are waiting, click N times. No cooldown
        # needed: pending is exact, not an estimated "is there one on
        # screen" bit.
        n_new = cur - prev_pending
        msg = (f"[state] +{n_new} chest(s) dropped "
               f"(pending {prev_pending} -> {cur}) | click ({ax},{ay})")
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
    else:
        # pending fell — chest was opened (by our click, or manually by
        # the user). The previous click is confirmed; no action needed.
        n_opened = prev_pending - cur
        msg = (f"[state] -{n_opened} chest(s) opened "
               f"(pending {prev_pending} -> {cur}) | click confirmed")
        print(msg); log_fh.write(msg + "\n"); log_fh.flush()

    return cur


def main():
    ap = argparse.ArgumentParser(
        description="Memory-driven TBH chest opener. Clicks when BoxObtain-BoxOpen rises.")
    ap.add_argument("--wx", type=int, default=533,
                    help="Window-relative X of the chest icon (default 533)")
    ap.add_argument("--wy", type=int, default=744,
                    help="Window-relative Y of the chest icon (default 744)")
    ap.add_argument("--poll", type=float, default=0.5,
                    help="Seconds between memory reads (default 0.5). "
                         "First call takes a few seconds while the reader "
                         "attaches to the process; subsequent reads are <10 ms.")
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

    # Detect monitor count for the click-math switch. monitors==1 means
    # primary-monitor-only -- skip MOUSEEVENTF_VIRTUALDESKTOP and use
    # SM_CXSCREEN. >1 requires virtual-desktop handling.
    monitor_count = len(monitor_bounds())
    _VIRTUAL_DESKTOP = (monitor_count > 1)

    boot = (f"[boot] TBH hwnd={hwnd} rect=({L0},{T0},...) "
            f"click_rel=({args.wx},{args.wy}) abs=({ax0},{ay0}) "
            f"poll={args.poll}s mode={click_mode} monitors={monitor_count} "
            f"virtual_desktop={_VIRTUAL_DESKTOP} "
            f"reader=memory(BoxObtain-BoxOpen); first read attaches the process (a few seconds)...")
    print(boot); log_fh.write(boot + "\n"); log_fh.flush()

    # Kick the memory reader on boot so the slow attach happens NOW
    # (background) instead of blocking the first tick. get_pending()
    # returns None silently until attach completes.
    try:
        _first = memory_attach.get_pending(hwnd)
        if _first is not None:
            attach_msg = f"[boot] memory reader attached; initial pending={_first}"
        else:
            attach_msg = "[boot] memory reader still attaching (will retry on first tick)"
        print(attach_msg); log_fh.write(attach_msg + "\n"); log_fh.flush()
    except Exception as e:
        warn = f"[boot] memory reader warm-up failed: {e} (continuing; will retry on tick)"
        print(warn); log_fh.write(warn + "\n"); log_fh.flush()

    prev_pending = None
    try:
        while True:
            prev_pending = tick(hwnd, args.wx, args.wy, click_mode,
                                prev_pending, log_fh,
                                virtual_desktop=_VIRTUAL_DESKTOP)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\n[chest-state] stopped")
        memory_attach.shutdown()
        log_fh.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
