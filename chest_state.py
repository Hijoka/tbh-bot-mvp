r"""chest_state.py — open chest drops gated by reading the meter's live.json.

State-driven MVP. ~300 lines, stdlib + ctypes only.

INPUT  : <meter-dir>/live.json (written ~1x/s by mad-labs-org/tbh-meter).
         We stat() the file each poll and re-parse only when mtime advances
         (zero-dep mtime fast-poll; no fsnotify / watchdog).
STATE  : drops = [Monster, Boss, ActBoss] — per-run counter from the meter.
         rises monotonically within a run as chests drop;
         resets to [0, 0, 0] on run end (meter "flushes the pending close").
ACTION : on any per-tier rising edge (cur[i] > prev[i]), click the chest
         icon at (wx, wy) window-relative. One click per edge — even if
         5 chests drop in a single run, we click 5 times.
         Edge case — the TRAILING-BOSS-BOX FLASH: on stage clear, the
         boss chest drops AND the run ends within ~1 s. The meter briefly
         raises drops[i] to N then flushes to [0,0,0]. If our poll
         misses the intermediate write, the rising-edge detector never
         fires for that chest. We track clicks emitted per tier per run;
         on run boundary, any tier where prev_drops[i] > clicks_emitted[i]
         gets clicked for the difference. Counter resets at run end.

History
-------
v1 (memory read of BoxObtain - BoxOpen):
  Failed on this build. Verified via _diag2/_diag3 that EAggregateType=3
  (BoxObtain) is NOT in the AggregateManager outer dict on TBH v1.00.21
  as installed on Admin's PC. BoxOpen (key=16) IS present and reads as
  617, but with no BoxObtain there's no "pending = obtained - opened"
  signal. The meter's own taskhero-engine MemoryReader docstring confirms
  this is the same gap: "BoxObtain (EAggregateType=3) is NOT in
  AggregateManager outer dict." The meter's own bot uses
  PlayerSaveData.BoxData.BoxUniqueId.count() as the alternate path; we
  do not have that calibration.

v2 (THIS FILE — meter live.json drops[]):
  Field semantics verified against the meter's source
  (mad-labs-org/tbh-meter/reader/meter_windows.py):
    - build_live_record() at line 406 emits drops=<list-of-3-ints>
    - _drop_counts() at line 616: dc[EMonsterLogType] += 1 per drop in
      the CURRENT run + any "absorbed" trailing-boss chests from the
      pending-close phase. Index = EMonsterLogType (Monster=0, Boss=1,
      ActBoss=2).
    - drops[i] NEVER decrements on click — it only resets to 0 at run
      end (the docstring: "a drop only lowers their baseline, no event;
      post-flush the count drops back, harmless").
    - live.json cadence: ~1 Hz (measured median 1.013 s on Admin's PC).

USAGE (PowerShell on Windows):
    cd C:\Users\Admin\tbh-bot-mvp   (or wherever you cloned it)
    py chest_state.py                                        # default dry-run
    py chest_state.py --dry-run                              # log only, no clicks
    py chest_state.py --preview                              # SetCursorPos only
    py chest_state.py --click                                # real clicks

    # Default meter dir is ~/tbh-meter (C:\\Users\\Admin\\tbh-meter).
    py chest_state.py --click --live-json C:\Users\Admin\tbh-meter\live.json
    py chest_state.py --click --poll 0.25                    # faster reaction

Stop with Ctrl-C.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import sys
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


# ---- live.json read + per-tier rising-edge detection --------------------- #
def _default_live_json_path() -> str:
    """Default meter location: ~/tbh-meter/live.json.

    On Windows, ~ is the user's home. We expand to an absolute path so
    subprocess logs / error messages don't depend on the caller's cwd.
    """
    return str(Path.home() / "tbh-meter" / "live.json")


def read_state(live_json_path: str, prev_state):
    """Read the live.json drops + run fields. mtime fast-poll: stat() first,
    re-parse JSON only when mtime advances. No deps beyond stdlib.

    Returns ((drops_tuple, run_id, mtime), True) on success,
            (prev_state, False) on no-change / unreadable / parse-error.

    drops_tuple is (Monster, Boss, ActBoss) — index = EMonsterLogType.
    run_id is the meter's `run` field (advances at each new stage attempt);
    mtime is the file's st_mtime at the read (used by tick() for log
    timestamps and to skip logging on no-change ticks).
    """
    prev_drops, prev_run, prev_mtime = (None, None, 0.0) if prev_state is None else (prev_state[0], prev_state[1], prev_state[2])
    # clicks_emitted (4th element) is preserved across reads — it lives
    # in the state tuple but read_state() doesn't need to look at it.
    # Defensive: if a 3-tuple state is somehow passed in (old code path,
    # saved state from an earlier version), default to (0, 0, 0).
    prev_clicks = (0, 0, 0)
    if prev_state is not None and len(prev_state) >= 4:
        prev_clicks = prev_state[3]
    try:
        mtime = os.stat(live_json_path).st_mtime
    except OSError:
        return prev_state, False
    if prev_mtime != 0.0 and mtime == prev_mtime:
        return prev_state, False                                # file unchanged
    try:
        with open(live_json_path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return prev_state, False
    drops = d.get("drops")
    if not isinstance(drops, list) or len(drops) != 3:
        return prev_state, False                                # wrong shape
    try:
        drops_t = (int(drops[0] or 0), int(drops[1] or 0), int(drops[2] or 0))
    except (TypeError, ValueError):
        return prev_state, False
    run_id = d.get("run")
    try:
        run_id = int(run_id) if run_id is not None else None
    except (TypeError, ValueError):
        run_id = None
    return (drops_t, run_id, mtime, prev_clicks), True


def _log(msg, log_fh):
    print(msg)
    log_fh.write(msg + "\n")
    log_fh.flush()


def tick(hwnd, wx, wy, click_mode, prev_state, log_fh, virtual_desktop, live_json_path):
    """One poll. Returns the new `prev_state` for the next tick.

    State machine (per `mad-labs-org/tbh-meter/reader/meter_windows.py`):
      drops = [Monster, Boss, ActBoss] per-run counter for the current run
              plus any "absorbed" trailing-boss chests from the
              pending-close phase.
      drops[i] NEVER decrements on click — only resets to 0 at run end
              (the meter "flushes the pending close"). So:
      - cur[i] > prev[i]  → 1+ chests of tier i just dropped → click once
      - cur[i] < prev[i]  → run-end reset (drops flushed to lower value)
      - run id advanced    → run-end reset
      - cur == prev        → no change, skip work

      Auto-collected chests still get clicked: the meter counts *drops*,
      not opens, so even if the chest auto-collects before our click, the
      rising edge fired and we click once.

      Multi-tier rise within the same tick: click ONCE per rising tier,
      not once total. If drops goes [1,0,0]→[2,1,1] we click 3 times.
      (The meter's per-tick "absorbed trailing-boss" mechanism can raise
       2 tiers simultaneously.)

    Edge case — the TRAILING-BOSS-BOX FLASH (verified on Admin's PC):
      On stage clear, the boss's chest drops AND the run ends within
      ~1 second. The meter briefly raises drops[i] to N, then flushes
      to [0,0,0]. If our poll cadence (--poll 0.25s) misses the
      intermediate write — i.e. we read `[0,0,0] → [0,1,0] → [0,0,0]`
      across two consecutive live.json writes — we never see the rising
      edge and the Boss chest never gets clicked.

      Fix: track clicks emitted per tier per run. On run boundary, if
      `prev_drops[i] > _clicks[i]` for any tier, click the difference.
      This catches chests that flashed too briefly for the normal rising-
      edge detector. The counter resets at run boundary.
    """
    state, changed = read_state(live_json_path, prev_state)
    cur_drops, cur_run, cur_mtime = state[0], state[1], state[2]    # state is a 4-tuple: (drops, run, mtime, clicks_emitted)

    if not changed:
        return prev_state                                       # no work

    # Compute click coord from current window position
    try:
        L, T, _, _ = window_rect(hwnd)
        ax, ay = L + wx, T + wy
    except RuntimeError as e:
        _log(f"[click] cannot get window rect: {e}", log_fh)
        return state

    if prev_state is None:
        prev_drops, prev_run, prev_mtime = (None, None, 0.0)
        clicks_emitted = (0, 0, 0)        # one per tier for the (about-to-start) run
    else:
        prev_drops, prev_run, prev_mtime, clicks_emitted = prev_state

    # First-ever read or new file -- establish baseline. No click even if
    # drops > 0: by the time we read, the chests may already be openable;
    # clicking blindly is wasted motion. Subsequent reads will catch the
    # change.
    if prev_drops is None:
        _log(f"[state] baseline drops={list(cur_drops)} run={cur_run} (from {live_json_path})", log_fh)
        return (cur_drops, cur_run, cur_mtime, (0, 0, 0))

    # Run-end reset: drops flushed to all zeros, or run id advanced.
    # Either case = adopt fresh baseline, BUT first check if we missed
    # any rising edges during the run that just ended (the trailing-boss-
    # box flash). For any tier where prev_drops[i] > clicks_emitted[i],
    # click the difference. Counter resets for the next run.
    run_changed = cur_run is not None and prev_run is not None and cur_run != prev_run
    zero_after_nonzero = all(c == 0 for c in cur_drops) and any(p > 0 for p in prev_drops)
    if run_changed or zero_after_nonzero:
        reason = "run id changed" if run_changed else "drops flushed to [0,0,0]"
        # Catch any missed rising edges from the trailing-boss flash.
        missed = [prev_drops[i] - clicks_emitted[i] for i in range(3)]
        missed_total = sum(m for m in missed if m > 0)
        tier_names = ("Monster", "Boss", "ActBoss")
        if missed_total > 0:
            tier_summary = ", ".join("%s+%d" % (tier_names[i], m)
                                     for i, m in enumerate(missed) if m > 0)
            _log(f"[state] run boundary ({reason}); prev={list(prev_drops)} run={prev_run}"
                 f" -> cur={list(cur_drops)} run={cur_run}; "
                 f"missed {missed_total} rising edge(s) (tiers=[{tier_summary}]) "
                 f"from trailing-boss flash | click ({ax},{ay}) x{missed_total}", log_fh)
            _do_clicks(missed_total, ax, ay, click_mode, virtual_desktop, log_fh)
        else:
            _log(f"[state] run boundary ({reason}); prev={list(prev_drops)} run={prev_run}"
                 f" -> cur={list(cur_drops)} run={cur_run}; "
                 f"clicks emitted={list(clicks_emitted)}; fresh baseline, no click", log_fh)
        return (cur_drops, cur_run, cur_mtime, (0, 0, 0))

    # No change — skip (cur == prev means all tiers unchanged)
    if cur_drops == prev_drops:
        return prev_state

    # Per-tier rising edges: any cur[i] > prev[i] is a fresh drop → click.
    # Also count up clicks emitted per tier so run-end can detect missed
    # edges from the trailing-boss flash.
    n_clicks = 0
    fired = []
    new_clicks = list(clicks_emitted)
    for i in range(3):
        if cur_drops[i] > prev_drops[i]:
            delta = cur_drops[i] - prev_drops[i]
            n_clicks += delta
            fired.append("%s+%d" % (tier_names[i], delta))
            new_clicks[i] += delta

    if n_clicks == 0:
        # Some weird decrease we didn't classify as run-end (shouldn't happen
        # in practice). Just adopt the new baseline.
        _log(f"[state] non-monotonic drop: prev={list(prev_drops)} -> cur={list(cur_drops)}; "
             f"adopting as new baseline (no click)", log_fh)
        return (cur_drops, cur_run, cur_mtime, clicks_emitted)

    _log(f"[state] +{n_clicks} chest(s) dropped "
         f"(drops {list(prev_drops)} -> {list(cur_drops)}, tiers=[{', '.join(fired)}]) "
         f"| click ({ax},{ay}) x{n_clicks}", log_fh)
    _do_clicks(n_clicks, ax, ay, click_mode, virtual_desktop, log_fh)

    return (cur_drops, cur_run, cur_mtime, tuple(new_clicks))


def _do_clicks(n_clicks, ax, ay, click_mode, virtual_desktop, log_fh):
    """Fire `n_clicks` left-clicks at (ax, ay), respecting click_mode."""
    if click_mode == "dry-run":
        return
    if click_mode == "preview":
        u32.SetCursorPos(ax, ay)
        _log(f"[preview] cursor moved to ({ax},{ay}) -- verify, then add --click", log_fh)
        return
    # click_mode == "click"
    for k in range(n_clicks):
        try:
            click_abs(ax, ay, virtual_desktop=virtual_desktop)
        except RuntimeError as e:
            _log(f"[click] failed (click {k+1}/{n_clicks}): {e}", log_fh)
            return                                       # stop on first failure
        if k + 1 < n_clicks:
            time.sleep(0.08 + random.random() * 0.05)


def main():
    ap = argparse.ArgumentParser(
        description="Open TBH chest drops gated by meter live.json rising edges.")
    ap.add_argument("--wx", type=int, default=533,
                    help="Window-relative X of the chest icon (default 533)")
    ap.add_argument("--wy", type=int, default=744,
                    help="Window-relative Y of the chest icon (default 744)")
    ap.add_argument("--poll", type=float, default=0.25,
                    help="Seconds between file stat() polls (default 0.25). "
                         "Meter writes live.json ~1x/s; 0.25 gives sub-second "
                         "reaction. Going faster is wasteful; going slower "
                         "misses intermediate drops.")
    ap.add_argument("--live-json", default=_default_live_json_path(),
                    help="Path to the meter's live.json (default: %(default)s)")
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
        _log(msg, log_fh)
        return 1

    # Pre-validate click coord lands on a real monitor.
    try:
        L0, T0, _, _ = window_rect(hwnd)
        ax0, ay0 = validate_click_coord(hwnd, args.wx, args.wy)
    except RuntimeError as e:
        _log(f"[boot] {e}", log_fh)
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

    # Verify the live.json path exists BEFORE we start the loop. A wrong
    # path is a 1-second fix; debugging it from per-tick log lines is
    # 5 minutes.
    if not os.path.exists(args.live_json):
        msg = (f"[boot] live.json not found at {args.live_json}. "
               f"Is tbh-meter running? Use --live-json to override the path.")
        _log(msg, log_fh)
        return 1

    boot = (f"[boot] TBH hwnd={hwnd} rect=({L0},{T0},...) "
            f"click_rel=({args.wx},{args.wy}) abs=({ax0},{ay0}) "
            f"poll={args.poll}s mode={click_mode} monitors={monitor_count} "
            f"virtual_desktop={_VIRTUAL_DESKTOP} "
            f"reader=live_json(drops[]); source={args.live_json}")
    _log(boot, log_fh)

    prev_state = None
    try:
        while True:
            prev_state = tick(hwnd, args.wx, args.wy, click_mode,
                              prev_state, log_fh,
                              virtual_desktop=_VIRTUAL_DESKTOP,
                              live_json_path=args.live_json)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\n[chest-state] stopped")
        log_fh.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())