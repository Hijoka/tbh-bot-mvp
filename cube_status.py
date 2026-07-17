r"""cube_status.py — read the BepInEx auto-synth plugin's status JSON.

The TBH Auto Synthesis BepInEx plugin (preschian/tbh-presence, prebuilt
TbhAutoSynth-next.dll) writes %LOCALAPPDATA%\tbh-companion\autosynth-status.json
every ~3 seconds with the current phase, cycle count, last item count, max
grade, and cycle interval. We mirror chest_state.py's mtime fast-poll discipline:
stat() first, re-parse only when mtime advances. Zero deps beyond stdlib.

This module is read-only and game-process read-only. We never write to the
file, never touch TBH memory, never click anything.

STATUS (v1, opportunistic-only mode):
  auto:           false (user-armed via F8, never auto-started)
  phase:          "Fill" | "Synth" | "Clear"
  cycles:         int (count of completed cycles in this run/session)
  lastCount:      int (-1 if no synth has run yet this session)
  lastGrade:      int (-1 if no synth has run yet this session)
  maxGrade:       int (from plugin config; 2 = RARE)
  cycleIntervalSeconds: int (from plugin config; 300 default)
  updatedUtc:     ISO-8601 UTC timestamp
  version:        plugin string version

USAGE (Python):
    from cube_status import read_status, _default_status_path
    state, mtime, changed = read_status(_default_status_path(), prev=None)
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _default_status_path() -> str:
    """Default plugin status location: %LOCALAPPDATA%\\tbh-companion\\autosynth-status.json.

    On Windows, %LOCALAPPDATA% resolves to e.g. C:\\Users\\Admin\\AppData\\Local.
    We read it from the env so the path is correct regardless of the caller's cwd.
    Raises RuntimeError on non-Windows or if the env var is unset, because this
    file is only written by a Windows BepInEx plugin into a Windows-only path.
    """
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not local:
        raise RuntimeError(
            "LOCALAPPDATA not set. cube_status only runs on Windows "
            "(the BepInEx plugin writes to %LOCALAPPDATA%\\tbh-companion\\)."
        )
    return str(Path(local) / "tbh-companion" / "autosynth-status.json")


def read_status(status_path: str, prev: tuple | None) -> tuple:
    """mtime fast-poll: stat() first, re-parse JSON only when mtime advances.

    Mirrors chest_state.read_state() line-for-line in spirit:
      - prev None, or mtime unchanged -> return (prev or (None, 0.0), False)
      - parse failure or wrong shape   -> return (prev or (None, 0.0), False)
      - success                        -> return (snapshot_dict, mtime, True)

    The snapshot is the parsed JSON dict. We do not extract fields here;
    callers decide what to compare. That keeps this module a thin read.

    Args:
        status_path: absolute path to autosynth-status.json.
        prev: previous return value, or None on first call.

    Returns:
        (snapshot_dict_or_None, mtime_float, changed_bool).
    """
    prev_snapshot, prev_mtime = (None, 0.0) if prev is None else (prev[0], prev[1])
    try:
        mtime = os.stat(status_path).st_mtime
    except OSError:
        # File doesn't exist yet (plugin not loaded or hasn't written yet)
        # or path is wrong. Return prev-or-None, False, so the caller can keep
        # polling without raising on the very first ticks.
        return prev_snapshot, prev_mtime, False
    if mtime == prev_mtime and prev_mtime != 0.0:
        # No new write since last poll. The 0.0 guard handles the first
        # ever read where prev_mtime is the sentinel.
        return prev_snapshot, prev_mtime, False
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        # File was just-written but partially. Skip this tick; next tick
        # will re-attempt. chest_state does the same.
        return prev_snapshot, prev_mtime, False
    if not isinstance(d, dict):
        # Plugin occasionally writes a sentinel at startup. Skip.
        return prev_snapshot, prev_mtime, False
    return d, mtime, True


def format_summary(snapshot: dict) -> str:
    """One-line human-readable summary for the orchestrator's log.

    Example:
        cube: armed=False phase=Fill cycles=0 lastCount=-1 lastGrade=-1
    """
    if not isinstance(snapshot, dict):
        return "cube: <no snapshot>"
    return (
        f"cube: armed={snapshot.get('auto', '?')} "
        f"phase={snapshot.get('phase', '?')} "
        f"cycles={snapshot.get('cycles', '?')} "
        f"lastCount={snapshot.get('lastCount', '?')} "
        f"lastGrade={snapshot.get('lastGrade', '?')}"
    )
