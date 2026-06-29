"""memory_attach.py — thin wrapper that vendors alandsamuel's TBH chest reader.

VENDOR ATTRIBUTION (NOTICE) — please retain
============================================
This file imports and re-exports the READ-side of alandsamuel's chest_mvp.py
process-attach + IL2CPP resolution code, vendored under
`vendor/` (paths preserved verbatim from the upstream repo):

    alandsamuel/TBH_Task-Bar-Hero_Bot
    https://github.com/alandsamuel/TBH_Task-Bar-Hero_Bot
    Licensed under the Apache License, Version 2.0.

In particular, the following is LIFTED VERBATIM (without modification) from
alandsamuel's chest_mvp.py and lives unmodified in this repo's vendor/ tree:

    * vendor/shared/memory.py     — Reader / process attach / module_base
    * vendor/config/offsets.py    — Class/Dict/Dict8B/EAggregateType offsets
    * vendor/il2cpp/typeinfo.py   — ga_module / table_base / class_by_index
    * vendor/il2cpp/finder.py     — bbwf_from_klass

The `GameAttach` class and `pending_chests()` method below are an
extracted/lightly-wrapped subset of alandsamuel's chest_mvp.py (lines
~117-211). The lift preserves the read-only invariant: PROCESS_QUERY_INFORMATION
| PROCESS_VM_READ, no WriteProcessMemory, no injection.

Original copyright header from chest_mvp.py is preserved in each vendored
file's docstring. See LICENSE in the repo root for the full Apache-2.0
text and required NOTICE boilerplate.

OUR CONTRIBUTIONS in this file
=============================
  * Public function `get_pending(hwnd) -> int | None` (the single API our
    bot's main loop calls each tick).
  * Module-level singleton that holds ONE `GameAttach` for the bot's
    lifetime and re-attaches on game restart (pid changes) or transient
    read failures.
  * Bounded retry on first call (alandsamuel's attach() already retries;
    we add an outer wrapper that prints a one-line status so the bot's
    boot line can show "attached pid=..." once the slow attach finishes).

The memory reader takes a few seconds to attach on first call (it must
locate the GameAssembly.dll module, walk the IL2CPP TypeInfoTable, and
resolve the AggregateManager class via a fixed RVA + TypeDefIndex). This
is documented in chest_state.py's boot line.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

# ---- VENDORED meter reader (subset) --------------------------------------- #
# We import the meter primitives we need: process attach, RVA + TypeInfoTable
# resolution, Dict walks. Vendored from alandsamuel/TBH_Task-Bar-Hero_Bot
# (Apache-2.0). See the NOTICE block at the top of this file.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "vendor"))

from shared.memory import Reader, find_pid, open_process, close, module_base  # noqa: E402
from il2cpp.typeinfo import ga_module, table_base, class_by_index, class_name  # noqa: E402
from il2cpp.finder import bbwf_from_klass                                       # noqa: E402
from config.offsets import EAggregateType, AggregateManager                     # noqa: E402


# Calibrated seed for build v1.00.21 (matches the meter's calib_seed.json).
# If your build differs, the meter will tell you — its reader logs the FP.
# These are the same constants alandsamuel's chest_mvp.py bakes in.
_CALIB = {
    "anchor_rva": 102128400,      # → s_TypeInfoTable
    "idx_ut":     2827,           # TypeDefIndex of AggregateManager (was named "ut"/"uu")
}


# ---- ATTACH + RESOLVE ----------------------------------------------------- #
# Lifted from alandsamuel's chest_mvp.py:117-211, then adapted to support
# the singleton pattern our main loop needs (re-attach on pid change).
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
        tbase = table_base(r, self.ga_base, _CALIB["anchor_rva"])
        if not tbase:
            self.detach()
            return False
        self.tbase = tbase
        K = class_by_index(r, self.tbase, _CALIB["idx_ut"])
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


# ---- PUBLIC API + INTERNAL SINGLETON -------------------------------------- #
#
# We hold ONE GameAttach for the bot's lifetime. Re-attach on:
#   (a) first call (cold start, slow);
#   (b) pid change (game was restarted);
#   (c) transient read failure (stale handle, OS recycled address space).
#
# The hwnd parameter is accepted for API symmetry with chest_state.py's
# window-find path but is not required for the read — the attach is by
# process name (TaskBarHero.exe), not by window handle. The hwnd is
# forwarded so future versions can cross-check the window's pid against
# the attached pid.
_attach: GameAttach | None = None
_attached_pid: int | None = None
_attach_locked = False    # True while a (re)attach is in progress


def _current_pid() -> int | None:
    """Best-effort: return the live pid of TaskBarHero.exe, or None."""
    try:
        return find_pid()
    except Exception:
        return None


def _ensure_attached(force: bool = False) -> GameAttach | None:
    """Return a working GameAttach or None.

    Auto-attaches on first call. Re-attaches on pid change (game restart)
    or on a previous transient read failure (caller signals via force=True).
    """
    global _attach, _attached_pid, _attach_locked

    pid_now = _current_pid()
    if pid_now is None:
        # game not running — keep whatever we have; do not detach
        return _attach if _attach and _attach.handle else None

    # Cold start
    if _attach is None or _attach.handle is None:
        if _attach_locked and not force:
            return None
        _attach_locked = True
        try:
            a = GameAttach()
            if a.attach():
                _attach = a
                _attached_pid = pid_now
            else:
                _attach = None
                _attached_pid = None
        finally:
            _attach_locked = False
        return _attach

    # Game restarted (pid changed) — re-attach
    if _attached_pid != pid_now:
        _attach.detach()
        _attach = None
        _attached_pid = None
        return _ensure_attached(force=True)

    return _attach


def get_pending(hwnd: int) -> int | None:
    """Read pending chest count (BoxObtain - BoxOpen) from TBH memory.

    Returns None on read failure (game not running, attach still in
    progress, transient read error, wrong build). Auto-attaches on
    first call. Reuses the same process handle across calls; re-attaches
    automatically if the game's pid changes (i.e. you restarted Task Bar
    Hero while the bot was running).

    The `hwnd` parameter is accepted for API symmetry with chest_state.py's
    window-find path but is not required for the read — the attach is by
    process name (TaskBarHero.exe).

    Cold-start cost: 2-30 s on first call while the attach resolves the
    IL2CPP TypeInfoTable (RVA + TypeDefIndex). Subsequent calls are <10 ms.
    """
    a = _ensure_attached()
    if a is None:
        return None
    try:
        return a.pending_chests()
    except Exception:
        # Handle went stale (process exited, address space recycled).
        # Force a re-attach on the next call.
        try:
            a.detach()
        except Exception:
            pass
        return None


def shutdown() -> None:
    """Release the read-only handle. Call from a clean exit handler if you
    care; ctypes will reap the OS handle on interpreter shutdown either way."""
    global _attach, _attached_pid
    if _attach is not None:
        try:
            _attach.detach()
        except Exception:
            pass
    _attach = None
    _attached_pid = None
