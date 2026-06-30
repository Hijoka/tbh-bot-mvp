r"""
cube_state.py — TBH cube synthesis bot (raw-file-driven).

Mirrors chest_state.py shape: stdlib + ctypes only, three-mode flag,
rising-edge detection on the meter's raw/<run>.json output.

Data layer:
  - Tails C:\Users\thomas\tbh-meter\raw\<run_id>.json (and Admin path on botting PC)
  - Per file: inventory.value + stash.value + heroes[*].items (equipped)
  - Compute "available for cube" = (inventory ∪ stash) − equipped uniqueIds
  - Bucket by (category, gradeId):
      slotId == 0                  -> MATERIALS
      slotId in 1..6               -> EQUIPMENT
      slotId in 7..10              -> ACCESSORIES
    Materials filter: gradeId < 4 (COMMON/UNCOMMON/RARE/LEGENDARY only)
    Equipment + Accessories: any gradeId, need 9 same-grade
  - Pick first bucket with count >= 9 in priority: EQUIPMENT -> MATERIALS -> ACCESSORIES

Click layer (absolute pixels):
  - Open/close cube panel: (1234, 647)
  - Menu button (3-mode selector):  (1499, 537)
  - Equipment selector:    (1439, 566)
  - Materials selector:    (1451, 595)
  - Accessories selector:  (1450, 620)
  - Autofill:              (1430, 534)
  - Confirm:               (1566, 534)

Modes:
  --dry-run   (default) detect eligible cube, log "would synthesize X materials grade Y", don't click
  --preview   move cursor to confirm coord without clicking
  --click     actually click; intended for production
"""
from __future__ import annotations
import os, sys, json, time, struct, ctypes, argparse, logging
from ctypes import wintypes

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
RAW_DIR_DEV   = r"C:\Users\thomas\tbh-meter\raw"
RAW_DIR_ADMIN = r"C:\Users\Admin\tbh-meter\raw"

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cube_state.log")

# ---------------------------------------------------------------------------
# Cube click coords (absolute pixels)
# ---------------------------------------------------------------------------
COORD_OPEN_PANEL     = (1234, 647)
COORD_MENU           = (1499, 537)
COORD_EQUIP_SELECT   = (1439, 566)
COORD_MAT_SELECT     = (1451, 595)
COORD_ACC_SELECT     = (1450, 620)
COORD_AUTOFILL       = (1430, 534)
COORD_CONFIRM        = (1566, 534)
CONFIRM_POLL_XY      = COORD_CONFIRM   # same coord we click; sample pixel here

# ---------------------------------------------------------------------------
# Mode / grade taxonomy
# ---------------------------------------------------------------------------
MODE_EQUIP     = "EQUIPMENT"
MODE_MATERIAL  = "MATERIALS"
MODE_ACCESSORY = "ACCESSORIES"
MODE_PRIORITY  = [MODE_EQUIP, MODE_MATERIAL, MODE_ACCESSORY]
MATERIAL_MAX_GRADE = 3   # gradeId < 4 → COMMON/UNCOMMON/RARE/LEGENDARY only

GRADE_NAMES = {
    0: "COMMON", 1: "UNCOMMON", 2: "RARE", 3: "LEGENDARY",
    4: "IMMORTAL", 5: "ARCANA", 6: "BEYOND", 7: "CELESTIAL",
    8: "DIVINE", 9: "COSMIC",
}

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
POLL_INTERVAL_S = 0.5     # how often to scan for new raw file
POST_CLICK_SLEEP_S = 0.6  # pause after each click for UI to settle
AUTOFILL_TIMEOUT_S = 5.0  # wait up to this long for confirm button to flip gray→blue
CONFIRM_POLL_S     = 0.1  # pixel poll interval while waiting for autofill
SYNTH_COOLDOWN_S   = 5.0  # after confirm, wait before next autofill (synthesis animation; 5s = safe even on long synths)
PANEL_OPEN_VERIFY_S = 1.0 # after panel-open click, pause before first action

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("cube_state")
logger.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_fh); logger.addHandler(_sh)

# ---------------------------------------------------------------------------
# Win32 — window + pixel
# ---------------------------------------------------------------------------
user32   = ctypes.WinDLL("user32",   use_last_error=True)
gdi32    = ctypes.WinDLL("gdi32",    use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

EnumWindows      = user32.EnumWindows
EnumWindowsProc  = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
GetWindowTextW   = user32.GetWindowTextW
GetWindowTextW.restype = wintypes.INT
GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, wintypes.INT]
GetClassNameW    = user32.GetClassNameW
GetClassNameW.restype = wintypes.INT
GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, wintypes.INT]
GetWindowRect    = user32.GetWindowRect
GetWindowRect.restype = wintypes.BOOL
GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
IsWindowVisible  = user32.IsWindowVisible
IsWindowVisible.restype = wintypes.BOOL
IsWindowVisible.argtypes = [wintypes.HWND]
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowThreadProcessId.restype = wintypes.DWORD
GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
SetForegroundWindow = user32.SetForegroundWindow
SetForegroundWindow.restype = wintypes.BOOL
SetForegroundWindow.argtypes = [wintypes.HWND]
SetCursorPos     = user32.SetCursorPos
SetCursorPos.restype = wintypes.BOOL
SetCursorPos.argtypes = [wintypes.INT, wintypes.INT]
mouse_event     = user32.mouse_event
mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]

# Pixel sampling
GetDC           = user32.GetDC
GetDC.restype   = wintypes.HDC
GetDC.argtypes  = [wintypes.HWND]
ReleaseDC       = user32.ReleaseDC
ReleaseDC.restype = wintypes.INT
ReleaseDC.argtypes = [wintypes.HDC, wintypes.HWND]
GetPixel        = gdi32.GetPixel
GetPixel.restype = wintypes.DWORD
GetPixel.argtypes = [wintypes.HDC, wintypes.INT, wintypes.INT]

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP   = 0x0004
COLORREF = lambda r, g, b: (b << 16) | (g << 8) | r  # little-endian helper

# ---------------------------------------------------------------------------
# Window discovery (lifted from chest_state.py — UnityWndClass + TaskBarHero.exe)
# ---------------------------------------------------------------------------
def find_tbh_window():
    """Return (hwnd, (left, top, right, bottom)) for the TBH window, or (None, None)."""
    found = []

    def cb(hwnd, lparam):
        if not IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        GetClassNameW(hwnd, cls, 64)
        title = ctypes.create_unicode_buffer(256)
        GetWindowTextW(hwnd, title, 256)
        pid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # Unity game window OR TaskBarHero.exe process
        if cls.value == "UnityWndClass" and title.value:
            found.append((hwnd, pid.value, title.value))
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    if not found:
        return None, None
    # Prefer the one with TaskBarHero in title (dev naming)
    for hwnd, pid, title in found:
        if "TaskBarHero" in title or "TBH" in title:
            r = wintypes.RECT()
            GetWindowRect(hwnd, ctypes.byref(r))
            return hwnd, (r.left, r.top, r.right, r.bottom)
    # Else first Unity window
    hwnd, pid, title = found[0]
    r = wintypes.RECT()
    GetWindowRect(hwnd, ctypes.byref(r))
    return hwnd, (r.left, r.top, r.right, r.bottom)

# ---------------------------------------------------------------------------
# Pixel sampling (for confirm-button gray→blue detection)
# ---------------------------------------------------------------------------
def sample_pixel(hwnd, x, y):
    """Return (r, g, b) at absolute (x, y) on hwnd's window. Uses GetDC/GetPixel."""
    hdc = GetDC(hwnd)
    try:
        colorref = GetPixel(hdc, x, y)
    finally:
        ReleaseDC(hdc, hwnd)
    # colorref is 0x00BBGGRR; COLORREF macro confirms
    r = colorref & 0xFF
    g = (colorref >> 8) & 0xFF
    b = (colorref >> 16) & 0xFF
    return (r, g, b)

def color_distance(c1, c2):
    """Euclidean RGB distance. Used to detect button color change."""
    return sum((a - b) ** 2 for a, b in zip(c1, c2)) ** 0.5

# ---------------------------------------------------------------------------
# Click helpers
# ---------------------------------------------------------------------------
def click(hwnd, ax, ay):
    """Move cursor to absolute (ax, ay), focus hwnd, click left button."""
    SetForegroundWindow(hwnd)
    time.sleep(0.05)
    SetCursorPos(ax, ay)
    time.sleep(0.05)
    mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

def move_only(ax, ay):
    SetCursorPos(ax, ay)

# ---------------------------------------------------------------------------
# Raw file discovery + reading
# ---------------------------------------------------------------------------
def discover_raw_dir():
    """Return path to existing raw/ dir, preferring dev path, falling back to botting path."""
    for path in (RAW_DIR_DEV, RAW_DIR_ADMIN):
        if os.path.isdir(path):
            return path
    return RAW_DIR_DEV  # may not exist; caller will see no files

def list_raw_files(raw_dir):
    """Return list of (run_id_str, full_path, mtime) sorted by run_id DESC (newest first)."""
    if not os.path.isdir(raw_dir):
        return []
    out = []
    try:
        for name in os.listdir(raw_dir):
            if not name.endswith(".json"):
                continue
            run_id = name[:-5]  # strip .json
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
    """Read raw JSON. Returns dict or None on parse failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

# ---------------------------------------------------------------------------
# Data layer — bucket inventory items by (category, gradeId)
# ---------------------------------------------------------------------------
def categorize(slot_id):
    if slot_id == 0:
        return MODE_MATERIAL
    if 1 <= slot_id <= 6:
        return MODE_EQUIP
    if 7 <= slot_id <= 10:
        return MODE_ACCESSORY
    return None

def extract_equipped_uniqueids(raw):
    """Return set of uniqueIds currently equipped to any hero."""
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
    """Yield every item in inventory.value + stash.value."""
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
    """
    Returns dict: {(category, gradeId): count} of items AVAILABLE for cube
    (i.e., not currently equipped to any hero), filtered by category rules.
    Materials filter: gradeId < MATERIAL_MAX_GRADE+1 (i.e., < 4).
    """
    equipped = extract_equipped_uniqueids(raw)
    buckets = {}
    for it in iter_inventory_items(raw):
        slot_id = it.get("slotId")
        grade_id = it.get("gradeId")
        unique_id = str(it.get("uniqueId", ""))
        if unique_id in equipped:
            continue  # equipped to a hero, not available
        cat = categorize(slot_id)
        if cat is None:
            continue
        if cat == MODE_MATERIAL and (grade_id is None or grade_id > MATERIAL_MAX_GRADE):
            continue  # materials must be < IMMORTAL
        key = (cat, grade_id)
        buckets[key] = buckets.get(key, 0) + 1
    return buckets

def pick_eligible(buckets):
    """
    Walk MODE_PRIORITY, return (mode, gradeId) of first bucket with count >= 9, or None.
    Within a mode, prefer lowest gradeId (synthesize cheap stuff first).
    """
    for mode in MODE_PRIORITY:
        # collect grades for this mode
        grades = sorted([g for (c, g) in buckets if c == mode])
        for g in grades:
            if buckets[(mode, g)] >= 9:
                return mode, g
    return None

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
class CubeState:
    """Holds the running session state. One per bot process."""

    def __init__(self):
        self.last_run_id = None       # str | None
        self.last_mtime = 0.0
        self.last_data = None         # raw dict, kept for re-bucketing without re-read
        self.mode_panel = None        # which mode the cube panel UI is currently on (None=unknown/closed)
        self.panel_open = False

    def update(self, raw_dir, poll=True):
        """
        Returns list of (run_id, path, mtime) of NEW raw files since last update.
        Empty list if no new files (or panel closed / no data).
        """
        files = list_raw_files(raw_dir)
        if not files:
            return []
        newest_id, newest_path, newest_mt = files[0]
        if self.last_run_id is None:
            # First run — just baseline, don't synthesize on first tick
            self.last_run_id = newest_id
            self.last_mtime = newest_mt
            self.last_data = read_raw_file(newest_path)
            return []
        if newest_id == self.last_run_id:
            return []  # no new run
        # New run detected — load it
        new_data = read_raw_file(newest_path)
        if new_data is None:
            return []
        self.last_run_id = newest_id
        self.last_mtime = newest_mt
        self.last_data = new_data
        return [(newest_id, newest_path, newest_mt)]

# ---------------------------------------------------------------------------
# Cube click layer — orchestrates the click sequence for one synthesis
# ---------------------------------------------------------------------------
def ensure_mode(hwnd, target_mode, state, dry_run=False):
    """If cube panel is on wrong mode, click menu + correct selector."""
    if state.mode_panel == target_mode:
        return
    logger.info(f"  switching mode -> {target_mode}")
    if not dry_run:
        click(hwnd, *COORD_MENU)
        time.sleep(POST_CLICK_SLEEP_S)
        if target_mode == MODE_EQUIP:
            click(hwnd, *COORD_EQUIP_SELECT)
        elif target_mode == MODE_MATERIAL:
            click(hwnd, *COORD_MAT_SELECT)
        elif target_mode == MODE_ACCESSORY:
            click(hwnd, *COORD_ACC_SELECT)
        time.sleep(POST_CLICK_SLEEP_S)
    state.mode_panel = target_mode

def wait_for_confirm_ready(hwnd, baseline_rgb, timeout_s=AUTOFILL_TIMEOUT_S, dry_run=False):
    """
    Poll confirm-button pixel until it differs from baseline (gray → blue)
    OR timeout. Returns True if ready, False if timeout.
    In dry-run mode, returns True immediately.
    """
    if dry_run:
        return True
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        cur = sample_pixel(hwnd, *CONFIRM_POLL_XY)
        if color_distance(cur, baseline_rgb) > 30:  # threshold tuned empirically
            return True
        time.sleep(CONFIRM_POLL_S)
    return False

def synthesize_once(hwnd, mode, grade_id, count, state, dry_run=False, preview=False):
    """Run one cube synthesis cycle. Returns True on success."""
    grade_name = GRADE_NAMES.get(grade_id, f"G{grade_id}")
    logger.info(f"  would synthesize: {mode} grade={grade_name} ({grade_id}) x {count} items")

    if preview:
        # Move cursor to confirm coord without clicking — visual sanity check
        move_only(*COORD_CONFIRM)
        time.sleep(0.3)
        return True

    if dry_run:
        state.mode_panel = mode  # virtual — don't actually click
        return True

    # Real click sequence
    if not state.panel_open:
        logger.info(f"  opening cube panel at {COORD_OPEN_PANEL}")
        click(hwnd, *COORD_OPEN_PANEL)
        time.sleep(PANEL_OPEN_VERIFY_S)
        state.panel_open = True
        state.mode_panel = None  # unknown — need to set explicitly

    ensure_mode(hwnd, mode, state, dry_run=False)

    # Sample confirm-button pixel BEFORE autofill (gray baseline)
    baseline_rgb = sample_pixel(hwnd, *CONFIRM_POLL_XY)
    logger.info(f"  confirm baseline rgb={baseline_rgb}")

    # Click autofill
    logger.info(f"  clicking autofill at {COORD_AUTOFILL}")
    click(hwnd, *COORD_AUTOFILL)

    # Wait for confirm button to flip gray→blue
    if not wait_for_confirm_ready(hwnd, baseline_rgb):
        logger.warning(f"  autofill failed: confirm button did not change color in {AUTOFILL_TIMEOUT_S}s")
        return False

    # Click confirm
    logger.info(f"  clicking confirm at {COORD_CONFIRM}")
    click(hwnd, *COORD_CONFIRM)

    # Wait for synthesis animation / cooldown
    time.sleep(SYNTH_COOLDOWN_S)
    return True

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_bot(args):
    raw_dir = discover_raw_dir()
    logger.info(f"[boot] raw_dir={raw_dir}")
    if not os.path.isdir(raw_dir):
        logger.error(f"raw dir not found: {raw_dir}")
        return 2

    hwnd, rect = find_tbh_window()
    if hwnd is None:
        logger.error("TBH window not found")
        return 3
    logger.info(f"[boot] TBH hwnd={hwnd} rect={rect} mode={'dry-run' if args.dry_run else ('preview' if args.preview else 'click')} raw_dir={raw_dir}")

    state = CubeState()
    # Baseline at startup (don't synthesize on first read)
    state.update(raw_dir)

    while True:
        try:
            new_files = state.update(raw_dir)
            if not new_files:
                time.sleep(POLL_INTERVAL_S)
                continue

            # New run detected — recompute buckets
            buckets = compute_cube_buckets(state.last_data)
            if not buckets:
                logger.info(f"[state] new run {state.last_run_id[:8]}... no inventory items found")
                continue

            # Show summary
            lines = [f"[state] new run {state.last_run_id[:8]}... buckets:"]
            for (cat, g), c in sorted(buckets.items()):
                marker = " <-- eligible" if c >= 9 else ""
                lines.append(f"    {cat:<10} grade={GRADE_NAMES.get(g, g):<10} count={c}{marker}")
            logger.info("\n".join(lines))

            # Pick first eligible
            picked = pick_eligible(buckets)
            if picked is None:
                logger.info(f"[state] no bucket has >= 9 items, nothing to synthesize")
                continue
            mode, grade_id = picked
            count = buckets[(mode, grade_id)]
            logger.info(f"[action] new run {state.last_run_id[:8]}... synthesizing {mode} {GRADE_NAMES[grade_id]} x {count}")
            synthesize_once(hwnd, mode, grade_id, count, state,
                           dry_run=args.dry_run, preview=args.preview)

        except KeyboardInterrupt:
            logger.info("[shutdown] Ctrl+C, exiting")
            return 0
        except Exception as e:
            logger.exception(f"[error] tick failed: {e}")
            time.sleep(POLL_INTERVAL_S)

def main():
    p = argparse.ArgumentParser(description="TBH cube synthesis bot (raw-file-driven)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="detect + log, no clicks (DEFAULT)")
    g.add_argument("--preview", action="store_true", help="move cursor to confirm coord without clicking")
    g.add_argument("--click",   action="store_true", help="actually click — production mode")
    p.add_argument("--raw-dir", default=None, help="override raw/ dir (default: auto-detect dev/admin)")
    args = p.parse_args()

    if args.preview:
        args.dry_run = False
    elif args.click:
        args.dry_run = False

    if args.raw_dir:
        global RAW_DIR_DEV, RAW_DIR_ADMIN
        RAW_DIR_DEV = args.raw_dir
        RAW_DIR_ADMIN = args.raw_dir

    return run_bot(args)

if __name__ == "__main__":
    sys.exit(main())