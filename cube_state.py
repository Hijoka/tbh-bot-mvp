r"""cube_state.py - TBH cube synthesis bot (raw-file-driven).

Mirrors chest_state.py shape: stdlib + ctypes only, three-mode flag,
rising-edge detection on the meter's raw/<run>.json output. Click layer
uses chest's proven SendInput + MOUSEEVENTF_VIRTUALDESKTOP path
(multi-monitor safe).

Data layer:
  - Tails C:\Users\thomas\tbh-meter\raw\<run_id>.json (and Admin path on botting PC)
  - Per file: inventory.value + stash.value + heroes[*].items (equipped)
  - Compute "available for cube" = (inventory U stash) - equipped uniqueIds
  - Bucket by (category, gradeId):
      slotId == 0                  -> MATERIALS
      slotId in 1..6               -> EQUIPMENT
      slotId in 7..10              -> ACCESSORIES
    Materials filter: gradeId < 4 (COMMON/UNCOMMON/RARE/LEGENDARY only)
    Equipment + Accessories: any gradeId, need 9 same-grade
  - Pick first bucket with count >= 9 in priority: EQUIPMENT -> MATERIALS -> ACCESSORIES

Click layer (window-relative pixels - resolved to absolute each tick via window_rect):
  Defaults were captured on dev PC (window at (691, 5)) and converted to relative.
  Override per-coord via --wx-open / --wy-open etc. if misaligned on Admin.

Modes:
  --dry-run   (default) detect eligible cube, log "would synthesize X Y grade Z", don't click
  --preview   set cursor to first would-click coord (visual sanity check, no click)
  --click     actually click; intended for production

Usage (PowerShell on Windows):
    cd C:\Users\Admin\tbh-bot-mvp
    py cube_state.py                       # default dry-run
    py cube_state.py --preview             # SetCursorPos only, validates click coords
    py cube_state.py --click               # real clicks

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


# ---- Paths --------------------------------------------------------------- #
RAW_DIR_DEV   = r"C:\Users\thomas\tbh-meter\raw"
RAW_DIR_ADMIN = r"C:\Users\Admin\tbh-meter\raw"


# ---- Cube coords (DEFAULT: window-relative) ----------------------------- #
# Captured on dev PC where TBH sat at (691, 5). Override per-coord on the
# CLI if your window position shifts the panel UI relative to the window.
# Use --preview to verify each coord lands on the right UI element.
COORD_OPEN_PANEL_DEFAULT    = ( 543, 642)   # cube icon in main UI
COORD_MENU_DEFAULT          = ( 808, 532)   # 3-mode selector button
COORD_EQUIP_SELECT_DEFAULT  = ( 748, 561)   # "Equipment" option
COORD_MAT_SELECT_DEFAULT    = ( 760, 590)   # "Materials" option
COORD_ACC_SELECT_DEFAULT    = ( 759, 615)   # "Accessories" option
COORD_AUTOFILL_DEFAULT      = ( 739, 529)   # autofill all eligible items button
COORD_CONFIRM_DEFAULT       = ( 875, 529)   # confirm synthesis button
CONFIRM_POLL_XY             = COORD_CONFIRM_DEFAULT


# ---- Mode / grade taxonomy ---------------------------------------------- #
MODE_EQUIP     = "EQUIPMENT"
MODE_MATERIAL  = "MATERIALS"
MODE_ACCESSORY = "ACCESSORIES"
MODE_PRIORITY  = [MODE_EQUIP, MODE_MATERIAL, MODE_ACCESSORY]
MATERIAL_MAX_GRADE = 3   # gradeId < 4 -> COMMON/UNCOMMON/RARE/LEGENDARY only

GRADE_NAMES = {
    0: "COMMON", 1: "UNCOMMON", 2: "RARE", 3: "LEGENDARY",
    4: "IMMORTAL", 5: "ARCANA", 6: "BEYOND", 7: "CELESTIAL",
    8: "DIVINE", 9: "COSMIC",
}


# ---- Timing ------------------------------------------------------------- #
POLL_INTERVAL_S    = 0.5     # how often to scan for new raw file
POST_CLICK_SLEEP_S = 0.6     # pause after each click for UI to settle
AUTOFILL_TIMEOUT_S = 5.0     # wait up to this long for confirm button to flip gray->blue
CONFIRM_POLL_S     = 0.1     # pixel poll interval while waiting for autofill
SYNTH_COOLDOWN_S   = 5.0     # after confirm, wait before next autofill (animation)
PANEL_OPEN_VERIFY_S = 1.0    # after panel-open click, pause before first action


# ---- Win32 setup (lifted from chest_state.py / alandsamuel's click.py) -- #
u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi
gdi32 = ctypes.windll.gdi32

k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
psapi.GetModuleFileNameExW.argtypes = [wintypes.HWND, wintypes.HMODULE,
                                       wintypes.LPWSTR, wintypes.DWORD]
psapi.GetModuleFileNameExW.restype = wintypes.DWORD
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_UNITY_CLASS  = "UnityWndClass"
_EXPECTED_EXE = "taskbarhero.exe"


# ---- Window discovery (lifted from chest_state.py) ---------------------- #
def find_tbh_window():
    """Find TBH: Unity class AND owning process is TaskBarHero.exe.

    Validates via GetModuleFileNameExW (returns the EXE basename) so we don't
    grab a Unity editor window or unrelated Unity app.
    """
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
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

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
    is off every monitor. Catches "TBH on a disconnected/hidden monitor"
    before SendInput fires into nowhere.
    """
    L, T, _, _ = window_rect(hwnd)
    ax, ay = L + wx, T + wy
    if not point_on_any_monitor(ax, ay):
        raise RuntimeError(
            f"click ({ax},{ay}) off all monitors (window rect={L},{T},"
            f" target=({wx},{wy})) - refusing to click. "
            f"Move TBH onto a visible monitor and re-run."
        )
    return ax, ay


# ---- SendInput (lifted from chest_state.py / alandsamuel's click.py) --- #
# 0x8000 = MOUSEEVENTF_ABSOLUTE
# 0x4000 = MOUSEEVENTF_VIRTUALDESKTOP. Required for multi-monitor (without
#          it, SendInput clips to the primary monitor and ignores monitor 2+).
#          On single-monitor systems (monitors==1) it's harmless but unnecessary.
INPUT_MOUSE = 0
_MOVE_ABS = 0x0001 | 0x8000
_DOWN_ABS = 0x0002 | 0x8000
_UP_ABS   = 0x0004 | 0x8000


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
                    simpler mapping, avoids the rare SM_CXVIRTUALSCREEN==0 case.
    """
    jx = ax + random.randint(-jitter, jitter)
    jy = ay + random.randint(-jitter, jitter)
    if virtual_desktop:
        vsx = u32.GetSystemMetrics(76)   # SM_CXVIRTUALSCREEN
        vsy = u32.GetSystemMetrics(77)   # SM_CYVIRTUALSCREEN
        if vsx <= 0 or vsy <= 0:
            vsx = u32.GetSystemMetrics(0) or 1920
            vsy = u32.GetSystemMetrics(1) or 1080
        flags_move, flags_down, flags_up = _MOVE_ABS | 0x4000, _DOWN_ABS | 0x4000, _UP_ABS | 0x4000
    else:
        vsx = u32.GetSystemMetrics(0) or 1920
        vsy = u32.GetSystemMetrics(1) or 1080
        flags_move, flags_down, flags_up = _MOVE_ABS, _DOWN_ABS, _UP_ABS
    abs_x = int(jx * 65535 / vsx)
    abs_y = int(jy * 65535 / vsy)
    _send(flags_move, abs_x, abs_y)
    time.sleep(0.04 + random.random() * 0.06)
    _send(flags_down, abs_x, abs_y)
    time.sleep(hold_ms / 1000.0)
    _send(flags_up, abs_x, abs_y)


# ---- Pixel sampling (for confirm-button gray->blue detection) ----------- #
def sample_pixel(hwnd, x, y):
    """Return (r, g, b) at absolute (x, y) on hwnd's window. Uses GetDC/GetPixel."""
    GetDC = u32.GetDC
    GetDC.restype = wintypes.HDC
    GetDC.argtypes = [wintypes.HWND]
    ReleaseDC = u32.ReleaseDC
    ReleaseDC.restype = wintypes.INT
    ReleaseDC.argtypes = [wintypes.HDC, wintypes.HWND]
    GetPixel = gdi32.GetPixel
    GetPixel.restype = wintypes.DWORD
    GetPixel.argtypes = [wintypes.HDC, wintypes.INT, wintypes.INT]
    hdc = GetDC(hwnd)
    try:
        colorref = GetPixel(hdc, x, y)
    finally:
        ReleaseDC(hdc, hwnd)
    r = colorref & 0xFF
    g = (colorref >> 8) & 0xFF
    b = (colorref >> 16) & 0xFF
    return (r, g, b)


def color_distance(c1, c2):
    return sum((a - b) ** 2 for a, b in zip(c1, c2)) ** 0.5


# ---- Logging ------------------------------------------------------------ #
def _log(msg, log_fh):
    print(msg)
    log_fh.write(msg + "\n")
    log_fh.flush()


# ---- Raw file discovery + reading (unchanged from v1) ------------------- #
def discover_raw_dir():
    """Return path to existing raw/ dir, preferring dev, falling back to botting."""
    for path in (RAW_DIR_DEV, RAW_DIR_ADMIN):
        if os.path.isdir(path):
            return path
    return RAW_DIR_DEV  # may not exist; caller will see no files


def list_raw_files(raw_dir):
    """Return list of (run_id_str, full_path, mtime) sorted by run_id DESC."""
    if not os.path.isdir(raw_dir):
        return []
    out = []
    try:
        for name in os.listdir(raw_dir):
            if not name.endswith(".json"):
                continue
            run_id = name[:-5]
            full = os.path.join(raw_dir, name)
            try:
                mt = os.path.getmtime(full)
            except OSError:
                continue
            out.append((run_id, full, mt))
    except OSError:
        return []
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def read_raw_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ---- Data layer --------------------------------------------------------- #
def categorize(slot_id):
    if slot_id == 0:
        return MODE_MATERIAL
    if 1 <= slot_id <= 6:
        return MODE_EQUIP
    if 7 <= slot_id <= 10:
        return MODE_ACCESSORY
    return None


def extract_equipped_uniqueids(raw):
    out = set()
    heroes = raw.get("heroes", {})
    if isinstance(heroes, dict):
        heroes = heroes.get("value", [])
    if not isinstance(heroes, list):
        return out
    for h in heroes:
        if not isinstance(h, dict):
            continue
        for it in h.get("items", []) or []:
            if isinstance(it, dict) and "uniqueId" in it:
                out.add(str(it["uniqueId"]))
    return out


def iter_inventory_items(raw):
    for key in ("inventory", "stash"):
        v = raw.get(key, {})
        if isinstance(v, dict):
            v = v.get("value", [])
        if not isinstance(v, list):
            continue
        for it in v:
            if isinstance(it, dict):
                yield it


def compute_cube_buckets(raw):
    """Returns dict: {(category, gradeId): count} of items AVAILABLE for cube
    (not equipped to any hero), filtered by category rules.
    Materials filter: gradeId < MATERIAL_MAX_GRADE+1 (i.e., < 4).
    """
    equipped = extract_equipped_uniqueids(raw)
    buckets = {}
    for it in iter_inventory_items(raw):
        slot_id = it.get("slotId")
        grade_id = it.get("gradeId")
        unique_id = str(it.get("uniqueId", ""))
        if unique_id in equipped:
            continue
        cat = categorize(slot_id)
        if cat is None:
            continue
        if cat == MODE_MATERIAL and (grade_id is None or grade_id > MATERIAL_MAX_GRADE):
            continue
        key = (cat, grade_id)
        buckets[key] = buckets.get(key, 0) + 1
    return buckets


def pick_eligible(buckets):
    """Walk MODE_PRIORITY, return (mode, gradeId) of first bucket with count >= 9.
    Within a mode, prefer lowest gradeId (synthesize cheap stuff first).
    """
    for mode in MODE_PRIORITY:
        grades = sorted([g for (c, g) in buckets if c == mode])
        for g in grades:
            if buckets[(mode, g)] >= 9:
                return mode, g
    return None


# ---- State machine ------------------------------------------------------ #
class CubeState:
    """Holds the running session state. One per bot process."""

    def __init__(self):
        self.last_run_id = None
        self.last_mtime = 0.0
        self.last_data = None
        self.mode_panel = None       # which mode the cube panel UI is on (None=unknown/closed)
        self.panel_open = False

    def update(self, raw_dir):
        """Returns list of (run_id, path, mtime) of NEW raw files since last update."""
        files = list_raw_files(raw_dir)
        if not files:
            return []
        newest_id, newest_path, newest_mt = files[0]
        if self.last_run_id is None:
            # First run - just baseline, don't synthesize on first tick
            self.last_run_id = newest_id
            self.last_mtime = newest_mt
            self.last_data = read_raw_file(newest_path)
            return []
        if newest_id == self.last_run_id:
            return []
        new_data = read_raw_file(newest_path)
        if new_data is None:
            return []
        self.last_run_id = newest_id
        self.last_mtime = newest_mt
        self.last_data = new_data
        return [(newest_id, newest_path, newest_mt)]


# ---- Click helpers (window-relative -> absolute via current window_rect) #
def _click_or_preview(hwnd, wx, wy, click_mode, label, log_fh):
    """Resolve a window-relative click coord to absolute and fire the action.

    click_mode:
      "dry-run"   -> log only
      "preview"   -> SetCursorPos to the would-click absolute position
      "click"     -> SendInput click at the absolute position
    Returns True on success, False if coord is off-monitor.
    """
    try:
        ax, ay = validate_click_coord(hwnd, wx, wy)
    except RuntimeError as e:
        _log(f"  [click] {label}: {e}", log_fh)
        return False

    if click_mode == "dry-run":
        _log(f"  [dry-run] {label} would click rel=({wx},{wy}) abs=({ax},{ay})", log_fh)
        return True
    if click_mode == "preview":
        u32.SetCursorPos(ax, ay)
        _log(f"  [preview] {label} cursor moved to ({ax},{ay}) - verify, then --click", log_fh)
        return True
    # click_mode == "click"
    try:
        click_abs(ax, ay, virtual_desktop=_VIRTUAL_DESKTOP)
    except RuntimeError as e:
        _log(f"  [click] {label}: {e}", log_fh)
        return False
    return True


def synthesize_once(hwnd, mode, grade_id, count, state, click_mode, coords, log_fh):
    """Run one cube synthesis cycle. Returns True on success."""
    grade_name = GRADE_NAMES.get(grade_id, f"G{grade_id}")

    # 1. Open cube panel (only if not already open)
    if not state.panel_open:
        if click_mode != "dry-run":
            _log(f"  [action] opening cube panel rel=({coords['open']})", log_fh)
        ok = _click_or_preview(hwnd, *coords["open"], click_mode, "open-panel", log_fh)
        if not ok:
            return False
        if click_mode == "click":
            time.sleep(PANEL_OPEN_VERIFY_S)
        state.panel_open = True
        state.mode_panel = None  # unknown - need to set explicitly

    # 2. Switch mode if needed
    if state.mode_panel != mode:
        if click_mode != "dry-run":
            _log(f"  [action] switching mode -> {mode}", log_fh)
        # Click menu, then the right selector
        ok = _click_or_preview(hwnd, *coords["menu"], click_mode, "menu-btn", log_fh)
        if not ok:
            return False
        selector_key = {"EQUIPMENT": "equip_sel",
                        "MATERIALS": "mat_sel",
                        "ACCESSORIES": "acc_sel"}[mode]
        if click_mode == "click":
            time.sleep(POST_CLICK_SLEEP_S)
        ok = _click_or_preview(hwnd, *coords[selector_key], click_mode, f"select-{mode}", log_fh)
        if not ok:
            return False
        if click_mode == "click":
            time.sleep(POST_CLICK_SLEEP_S)
        state.mode_panel = mode

    _log(f"  [decision] synthesize: {mode} grade={grade_name} ({grade_id}) x {count} items", log_fh)

    # 3. Sample confirm-button pixel BEFORE autofill (gray baseline)
    try:
        L, T, _, _ = window_rect(hwnd)
        confirm_abs = (L + coords["confirm"][0], T + coords["confirm"][1])
    except RuntimeError:
        confirm_abs = coords["confirm"]
    baseline_rgb = sample_pixel(hwnd, *confirm_abs)
    _log(f"  [pixel] confirm baseline rgb={baseline_rgb}", log_fh)

    # 4. Click autofill
    ok = _click_or_preview(hwnd, *coords["autofill"], click_mode, "autofill", log_fh)
    if not ok:
        return False

    # 5. Wait for confirm button to flip gray->blue (skip in dry-run/preview)
    if click_mode == "click":
        t0 = time.time()
        ready = False
        while time.time() - t0 < AUTOFILL_TIMEOUT_S:
            try:
                cur = sample_pixel(hwnd, *confirm_abs)
            except Exception:
                break
            if color_distance(cur, baseline_rgb) > 30:
                ready = True
                break
            time.sleep(CONFIRM_POLL_S)
        if not ready:
            _log(f"  [warning] autofill failed: confirm button did not change color in {AUTOFILL_TIMEOUT_S}s", log_fh)
            return False

    # 6. Click confirm
    ok = _click_or_preview(hwnd, *coords["confirm"], click_mode, "confirm", log_fh)
    if not ok:
        return False

    if click_mode == "click":
        time.sleep(SYNTH_COOLDOWN_S)
    return True


# ---- Main loop ---------------------------------------------------------- #
def run_bot(args):
    raw_dir = discover_raw_dir()
    if not os.path.isdir(raw_dir):
        _log(f"[boot] raw dir not found: {raw_dir}", args.log_fh)
        return 2

    hwnd = find_tbh_window()
    if not hwnd:
        _log(f"[boot] TBH window not found ({_UNITY_CLASS} + {_EXPECTED_EXE}). Is the game running and visible?", args.log_fh)
        return 3

    # Resolve coords dict from args (CLI overrides default)
    coords = {
        "open":      (args.wx_open, args.wy_open),
        "menu":      (args.wx_menu, args.wy_menu),
        "equip_sel": (args.wx_equip_sel, args.wy_equip_sel),
        "mat_sel":   (args.wx_mat_sel, args.wy_mat_sel),
        "acc_sel":   (args.wx_acc_sel, args.wy_acc_sel),
        "autofill":  (args.wx_autofill, args.wy_autofill),
        "confirm":   (args.wx_confirm, args.wy_confirm),
    }

    # Pre-validate click coords land on a real monitor.
    try:
        L0, T0, _, _ = window_rect(hwnd)
        # Validate just the confirm coord (the others have the same constraints).
        validate_click_coord(hwnd, coords["confirm"][0], coords["confirm"][1])
    except RuntimeError as e:
        _log(f"[boot] {e}", args.log_fh)
        return 1

    # Detect monitor count for the click-math switch.
    monitor_count = len(monitor_bounds())
    global _VIRTUAL_DESKTOP
    _VIRTUAL_DESKTOP = (monitor_count > 1)

    boot = (f"[boot] TBH hwnd={hwnd} rect=({L0},{T0},...) "
            f"mode={args.click_mode} monitors={monitor_count} "
            f"virtual_desktop={_VIRTUAL_DESKTOP} "
            f"reader=raw/<run>.json; raw_dir={raw_dir}")
    _log(boot, args.log_fh)

    state = CubeState()
    # Baseline at startup (don't synthesize on first read)
    state.update(raw_dir)

    while True:
        try:
            new_files = state.update(raw_dir)
            if not new_files:
                time.sleep(POLL_INTERVAL_S)
                continue

            # New run detected - recompute buckets
            buckets = compute_cube_buckets(state.last_data)
            if not buckets:
                _log(f"[state] new run {state.last_run_id[:8]}... no inventory items found", args.log_fh)
                continue

            # Show summary
            lines = [f"[state] new run {state.last_run_id[:8]}... buckets:"]
            for (cat, g), c in sorted(buckets.items()):
                marker = " <-- eligible" if c >= 9 else ""
                lines.append(f"    {cat:<10} grade={GRADE_NAMES.get(g, g):<10} count={c}{marker}")
            _log("\n".join(lines), args.log_fh)

            # Pick first eligible
            picked = pick_eligible(buckets)
            if picked is None:
                _log(f"[state] no bucket has >= 9 items, nothing to synthesize", args.log_fh)
                continue
            mode, grade_id = picked
            count = buckets[(mode, grade_id)]
            synthesize_once(hwnd, mode, grade_id, count, state,
                           args.click_mode, coords, args.log_fh)

        except KeyboardInterrupt:
            print("\n[cube-state] stopped")
            args.log_fh.close()
            return 0
        except Exception as e:
            import traceback
            _log(f"[error] tick failed: {e}\n{traceback.format_exc()}", args.log_fh)
            time.sleep(POLL_INTERVAL_S)


_VIRTUAL_DESKTOP = True   # overridden in run_bot after monitor detection


def main():
    ap = argparse.ArgumentParser(
        description="Synthesize cube items in TBH gated by raw/<run>.json bucket counts.")

    ap.add_argument("--raw-dir", default=None,
                    help="Override raw/ dir (default: auto-detect dev/admin).")

    # Window-relative click coords (defaults from dev capture at TBH window (691,5))
    ap.add_argument("--wx-open",  type=int, default=COORD_OPEN_PANEL_DEFAULT[0])
    ap.add_argument("--wy-open",  type=int, default=COORD_OPEN_PANEL_DEFAULT[1])
    ap.add_argument("--wx-menu",  type=int, default=COORD_MENU_DEFAULT[0])
    ap.add_argument("--wy-menu",  type=int, default=COORD_MENU_DEFAULT[1])
    ap.add_argument("--wx-equip-sel", type=int, default=COORD_EQUIP_SELECT_DEFAULT[0])
    ap.add_argument("--wy-equip-sel", type=int, default=COORD_EQUIP_SELECT_DEFAULT[1])
    ap.add_argument("--wx-mat-sel",   type=int, default=COORD_MAT_SELECT_DEFAULT[0])
    ap.add_argument("--wy-mat-sel",   type=int, default=COORD_MAT_SELECT_DEFAULT[1])
    ap.add_argument("--wx-acc-sel",   type=int, default=COORD_ACC_SELECT_DEFAULT[0])
    ap.add_argument("--wy-acc-sel",   type=int, default=COORD_ACC_SELECT_DEFAULT[1])
    ap.add_argument("--wx-autofill", type=int, default=COORD_AUTOFILL_DEFAULT[0])
    ap.add_argument("--wy-autofill", type=int, default=COORD_AUTOFILL_DEFAULT[1])
    ap.add_argument("--wx-confirm",  type=int, default=COORD_CONFIRM_DEFAULT[0])
    ap.add_argument("--wy-confirm",  type=int, default=COORD_CONFIRM_DEFAULT[1])

    # Three-mode flag (mutually exclusive, like chest)
    ap.add_argument("--dry-run", action="store_true",
                    help="Log decisions but do not click (DEFAULT)")
    ap.add_argument("--preview", action="store_true",
                    help="Move the real cursor to each would-click coord without clicking. "
                         "Use to verify click coords land on the right UI elements.")
    ap.add_argument("--click", action="store_true",
                    help="Actually click (production mode).")

    ap.add_argument("--log", default="cube_state.log",
                    help="Log file path (default cube_state.log next to this script)")

    args = ap.parse_args()

    # Mode precedence: --click > --preview > default (dry-run)
    flags = sum(bool(getattr(args, f)) for f in ("dry_run", "preview", "click"))
    if flags > 1:
        ap.error("--dry-run, --preview, and --click are mutually exclusive")
    click_mode = "click" if args.click else ("preview" if args.preview else "dry-run")
    args.click_mode = click_mode

    if args.raw_dir:
        global RAW_DIR_DEV, RAW_DIR_ADMIN
        RAW_DIR_DEV = args.raw_dir
        RAW_DIR_ADMIN = args.raw_dir

    log_path = Path(args.log)
    args.log_fh = open(log_path, "a", encoding="utf-8")

    return run_bot(args)


if __name__ == "__main__":
    sys.exit(main())
