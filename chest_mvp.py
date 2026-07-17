"""chest_mvp.py — open chests as soon as they drop. MVP, single file.

HOW IT WORKS
------------
Attaches to TaskBarHero.exe (read-only: PROCESS_VM_READ, no write ever).

Resolves the AggregateManager singleton via IL2CPP TypeInfoTable
(fixed RVA + TypeDefIndex — same fast path the TBH Meter uses for gold):

    GameAssembly.dll base + ANCHOR_RVA  →  s_TypeInfoTable
    table_base + idx_ut * 8            →  AggregateManager.Il2CppClass*
    klass → PARENT → STATIC_FIELDS → bbwf → live instance

Once attached, reads two EAggregateType cells every poll:

    BoxObtain  = cumulative chests dropped (this profile, ever)
    BoxOpen    = cumulative chests opened  (this profile, ever)
    pending    = BoxObtain - BoxOpen       = chests waiting to be opened

If pending rises (a chest dropped while the bot runs), the bot waits 1.5 s
for the drop animation to settle, then left-clicks the chest icon at
(CHEST_X, CHEST_Y) — same place every time, that's where the HUD anchors it.

If pending > 0 at startup (chests already there when the bot launches),
the bot clicks pending times to drain them, then watches for new drops.

WHY THIS IS BETTER THAN live.json
---------------------------------
- live.json's `drops` is per-current-RUN: resets to [0,0,0] on every clear,
  doesn't tell us about chests you already have on the ground.
- BoxObtain / BoxOpen are session-total counters: monotonic, always live,
  source of truth = the game itself.

REQUIREMENTS
------------
- Windows
- Python 3.10+
- Task Bar Hero running (visible)
- Game build v1.00.21 (the calibrated seed in CALIB below matches this build)

USAGE
-----
    python chest_mvp.py
    # Set CHEST_X, CHEST_Y first (see README). The bot will auto-detect
    # build and warn if the seed doesn't match.
"""

from __future__ import annotations

import ctypes
import os
import random
import struct
import sys
import time
from ctypes import wintypes

# ---- VENDORED meter reader (subset) --------------------------------------- #
# We import the meter primitives we need: process attach, RVA + TypeInfoTable
# resolution, Dict walks. Vendored from mad-labs-org/tbh-meter (Apache-2.0).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "vendor"))

from shared.memory import Reader, find_pid, open_process, close, module_base  # noqa: E402
from il2cpp.typeinfo import ga_module, table_base, class_by_index, class_name  # noqa: E402
from il2cpp.finder import bbwf_from_klass                                       # noqa: E402
from config.offsets import (Class, Dict, Dict8B,                                # noqa: E402
                             EAggregateType, AggregateManager)


# ---- CONFIG (you set CHEST_X, CHEST_Y once) -------------------------------- #
CHEST_X: int | None = 862          # absolute screen pixel X of the chest icon
CHEST_Y: int | None = 709          # absolute screen pixel Y of the chest icon
POLL_SECONDS = 0.5                # how often to re-read BoxObtain / BoxOpen
DROP_TO_CLICK_DELAY = 1.5         # wait this long after a chest drops, then click

# Calibrated seed for build v1.00.21 (matches the meter's calib_seed.json).
# If your build differs, the meter will tell you — its reader logs the FP.
CALIB = {
    "anchor_rva": 102128400,      # → s_TypeInfoTable
    "idx_ut":     2827,           # TypeDefIndex of AggregateManager (was named "ut"/"uu")
}


# ---- TYPED Win32 bits ----------------------------------------------------- #
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]


def click(x: int, y: int) -> None:
    """Synthetic left-click via SendInput. ±3 px jitter + variable hold."""
    u = ctypes.windll.user32
    sx, sy = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    jx = x + random.randint(-3, 3)
    jy = y + random.randint(-3, 3)
    ax, ay = int(jx * 65535 / sx), int(jy * 65535 / sy)

    def send(flags: int) -> None:
        inp = INPUT(0, MOUSEINPUT(ax, ay, 0, flags, 0, None))
        if u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) != 1:
            raise RuntimeError(f"SendInput failed (err={ctypes.get_last_error()})")

    send(0x8001)                                       # MOVE | ABSOLUTE
    time.sleep(0.02 + random.random() * 0.05)
    send(0x8002)                                       # LEFTDOWN | ABSOLUTE
    time.sleep(random.uniform(0.04, 0.09))            # variable hold
    send(0x8004)                                       # LEFTUP | ABSOLUTE


# ---- ATTACH + RESOLVE ----------------------------------------------------- #
class GameAttach:
    """Owns the read-only handle + the resolved AggregateManager klass.

    Resolution is one-shot at startup (the klass is stable for the session).
    Live reads go through `read_aggregate(klass, EAggregateType.X)` which
    follows: bbwf -> inst -> AGGREGATES -> outer[Type=X] -> inner[SubKey=0].
    """

    def __init__(self):
        self.handle = None
        self.pid: int | None = None
        self.ga_base: int | None = None
        self.ga_size: int | None = None
        self.tbase: int | None = None
        self.ut_klass: int | None = None

    def attach(self, retries: int = 30, retry_delay: float = 2.0) -> bool:
        for _ in range(retries):
            pid = find_pid()
            if pid:
                h = open_process(pid)
                if h:
                    base = module_base(pid)
                    if base:
                        self.handle, self.pid, self.ga_base = h, pid, base
                        break
                    close(h)
            time.sleep(retry_delay)
        if not self.handle:
            return False
        ga_base, ga_size = ga_module(self.pid)
        if not ga_base:
            self.detach()
            return False
        self.ga_base, self.ga_size = ga_base, ga_size
        r = Reader(self.handle)
        tbase = table_base(r, self.ga_base, CALIB["anchor_rva"])
        if not tbase:
            self.detach()
            return False
        self.tbase = tbase
        K = class_by_index(r, self.tbase, CALIB["idx_ut"])
        # Validate the class round-trips (cheap, defensive — wrong RVA/idx would
        # give a junk klass; this catches it before we start clicking)
        if not K or class_name(r, K) is None:
            self.detach()
            return False
        self.ut_klass = K
        return True

    def detach(self):
        if self.handle:
            close(self.handle)
        self.handle = self.pid = self.ga_base = self.ga_size = None
        self.tbase = self.ut_klass = None

    def _r(self) -> Reader:
        if not self.handle:
            raise RuntimeError("GameAttach not attached")
        return Reader(self.handle)

    def read_aggregate(self, agg: EAggregateType) -> int | None:
            """Live value of AggregateManager.AGGREGATES[agg][SubKey=0].

            The outer dict is keyed by EAggregateType; the inner dict is a single-key
            long with SubKey=0 (the cumulative count). Returns None on any failure.
            """
            if not self.ut_klass:
                return None
            r = self._r()
            inst = bbwf_from_klass(r, self.ut_klass)
            if not inst or inst <= 0x10000:
                return None
            outer = r.rptr(inst + AggregateManager.AGGREGATES)
            if not outer or outer <= 0x10000:
                return None
            # Walk outer Dict<EAggregateType, Dict<SubKey,long>> for key == agg
            for k, v in r.dict8b_items(outer):
                if k == agg:
                    # v is a POINTER to the inner Dict<SubKey,long>
                    if v is None or v <= 0x10000:
                        return None
                    for sk, sv in r.dict8b_items(v):
                        if sk == 0:                                # cumulative count
                            return sv if (sv is not None and 0 <= sv < 1_000_000_000) else None
                    return None
            return None

    def pending_chests(self) -> int | None:
        """BoxObtain - BoxOpen = chests waiting to be opened. None on read failure."""
        ob = self.read_aggregate(EAggregateType.BoxObtain)
        op = self.read_aggregate(EAggregateType.BoxOpen)
        if ob is None or op is None:
            return None
        return max(0, ob - op)


# ---- CORE LOOP ------------------------------------------------------------ #
def main() -> int:
    if CHEST_X is None or CHEST_Y is None:
        print("Set CHEST_X and CHEST_Y in this file first (see README).")
        return 1

    attach = GameAttach()
    print("Attaching to TaskBarHero.exe (read-only)...")
    if not attach.attach():
        print("Failed to attach. Is Task Bar Hero running? (retried 30x @ 2s)")
        return 1
    print(f"Attached. pid={attach.pid} ga_base={hex(attach.ga_base)} "
          f"ut_klass={hex(attach.ut_klass)}")

    pending = attach.pending_chests()
    if pending is None:
        print("Could not read BoxObtain/BoxOpen — wrong build? (seed is for v1.00.21)")
        attach.detach()
        return 1
    print(f"Pending chests at startup: {pending}")
    print(f"Will click ({CHEST_X},{CHEST_Y}) {DROP_TO_CLICK_DELAY}s after each new chest.")
    print("Ctrl-C to stop.\n")

    last = pending
    # If there are already chests waiting at startup, drain them: each click
    # opens one, which makes BoxOpen catch up to BoxObtain. We don't wait the
    # DROP_TO_CLICK_DELAY between these (they were already dropped, the
    # animation completed before the bot started).
    if pending > 0:
        print(f"[chest] draining {pending} chest(s) from startup...")
        for _ in range(pending):
            click(CHEST_X, CHEST_Y)
            time.sleep(random.uniform(0.5, 1.2))

    while True:
        time.sleep(POLL_SECONDS)
        pending = attach.pending_chests()
        if pending is None:
            continue                                  # transient read failure
        if pending > last:
            n_new = pending - last
            print(f"[chest] +{n_new} pending (total {last} -> {pending})")
            time.sleep(DROP_TO_CLICK_DELAY)
            for _ in range(n_new):
                click(CHEST_X, CHEST_Y)
                time.sleep(random.uniform(0.4, 0.9))
        last = pending


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[chest] stopped.")
    finally:
        # best-effort cleanup; close handled in attach.detach()
        pass