r"""orchestrator.py — coordinate chest_state.py + observability of the cube plugin.

This script does NOT click the cube. It does NOT press F8. It does NOT touch
the game process. Its only jobs:

  1. Verify TBH and the BepInEx auto-synth plugin are both running.
  2. Spawn chest_state.py --click as a child process (your existing chest detector,
     used unchanged).
  3. Read %LOCALAPPDATA%\tbh-companion\autosynth-status.json every second and
     print/log a one-line summary whenever it changes.
  4. Print a heartbeat every 30 seconds so the orchestrator is visibly alive
     even when nothing is happening.
  5. Cleanly stop the chest_state child on Ctrl-C.

The cube plugin (TbhAutoSynth-next.dll) is opportunistic per SPEC.md §4.3:
AutoStart=false, AutoOpenCube=false. It only runs while the user has the cube
panel open AND has pressed F8 to arm. The orchestrator never arms or disarms.

USAGE (PowerShell on Windows, by hand at the keyboard — never unattended):
    cd C:\Users\Admin\tbh-bot-mvp
    py orchestrator.py --click          # the only mode you should run live
    py orchestrator.py --dry-run        # verify the wireup without clicking chests
    py orchestrator.py --preview        # same as dry-run but moves the chest click
                                        # cursor via SetCursorPos (orchestrator does
                                        # NOT exercise preview mode for the cube — it
                                        # is chest-only)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Local imports: only stdlib + our own modules. cube_status is sibling-module.
from cube_status import _default_status_path, read_status, format_summary


# ---- constants --------------------------------------------------------- #
HEARTBEAT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 1.0
CHILD_STOP_TIMEOUT = 2.0


# ---- helpers ---------------------------------------------------------- #
def _log(msg: str, log_fh) -> None:
    """Mirror chest_state's _log so both tools produce output in the same shape."""
    print(msg)
    log_fh.write(msg + "\n")
    log_fh.flush()


def _find_tbh_pid() -> int | None:
    """Return the PID of TaskBarHero.exe, or None if not running.

    We shell out to `tasklist` so we don't pull in pywin32. Slow (one query at
    boot is fine; we do not poll the PID). If the user prefers speed, we can
    switch to a ctypes EnumProcesses scan later.
    """
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq TaskBarHero.exe", "/NH", "/FO", "CSV"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for line in out.splitlines():
        if not line.strip():
            continue
        # CSV header looks like: "Image Name","PID","Session Name","Session#","Mem Usage"
        parts = line.strip().strip('"').split('","')
        if len(parts) >= 2 and parts[0].lower().endswith("taskbarhero.exe"):
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


# ---- boot preflight --------------------------------------------------- #
def preflight(log_fh) -> int:
    """Verify TBH is running and the cube plugin's status file exists.

    Returns 0 on success, non-zero exit code on any failure.
    """
    pid = _find_tbh_pid()
    if pid is None:
        _log(
            "[boot] FAIL: TaskBarHero.exe is not running. Start TBH first, "
            "wait ~10s for BepInEx to load the cube plugin, then re-run.",
            log_fh,
        )
        return 2

    status_path = _default_status_path()
    # Give the plugin up to 10 s to write the first status file (it writes
    # every 3 s once BepInEx has loaded it).
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if Path(status_path).exists():
            break
        time.sleep(0.5)
    else:
        _log(
            f"[boot] FAIL: cube plugin status file not found at {status_path} "
            f"after 10s. Is BepInEx loaded? Check:\n"
            f"    {os.environ.get('GAME_DIR', '<game dir>')}\\BepInEx\\Log\\*.log\n"
            f"    The plugin logs 'TBH Auto Synthesis 0.24.0 [next/resilient]:' "
            f"on first load. If you don't see it, BepInEx didn't load.",
            log_fh,
        )
        return 2

    _log(f"[boot] TBH pid={pid}", log_fh)
    _log(f"[boot] cube plugin status file: {status_path}", log_fh)
    return 0


# ---- steady state ----------------------------------------------------- #
def run_loop(child: subprocess.Popen, log_fh) -> None:
    """Status-poll loop. Runs until Ctrl-C or child exits unexpectedly."""
    status_path = _default_status_path()
    prev: tuple | None = None
    last_heartbeat = 0.0
    started = time.monotonic()

    while True:
        # 1. Read cube status. Print summary on change, heartbeat on schedule.
        snapshot, _mtime, changed = read_status(status_path, prev)
        if changed and isinstance(snapshot, dict):
            _log(f"[cube] {format_summary(snapshot)}", log_fh)
            prev = (snapshot, _mtime)
        elif prev is None:
            # First poll, no file yet -> capture whatever we got (likely None)
            prev = (snapshot, _mtime)

        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            uptime = int(now - started)
            _log(
                f"[heartbeat] orchestrator alive, uptime={uptime}s, "
                f"chest_state pid={child.pid}, cube plugin armed="
                f"{isinstance(snapshot, dict) and snapshot.get('auto', False)}",
                log_fh,
            )
            last_heartbeat = now

        # 2. Watch the chest_state child. If it died unexpectedly, log and
        #    continue polling the cube status (so you still see cube events).
        if child.poll() is not None:
            rc = child.returncode
            _log(
                f"[chest] chest_state child exited rc={rc}. "
                f"Orchestrator continues polling cube. "
                f"Restart with: py orchestrator.py --click",
                log_fh,
            )
            # Don't break — keep the cube status visible. Break only on Ctrl-C.
            child = None

        # 3. Sleep the poll interval. Use sleep, not signal-aware sleep, to
        #    keep things simple; Ctrl-C raises KeyboardInterrupt which we
        #    catch in main().
        time.sleep(POLL_INTERVAL_SECONDS)


# ---- shutdown --------------------------------------------------------- #
def shutdown(child: subprocess.Popen | None, log_fh) -> int:
    """Stop the chest_state child cleanly. Never touch TBH or the plugin."""
    if child is None or child.poll() is not None:
        _log("[shutdown] no chest_state child to stop.", log_fh)
        return 0
    _log(f"[shutdown] sending SIGTERM to chest_state pid={child.pid}", log_fh)
    try:
        child.terminate()
        child.wait(timeout=CHILD_STOP_TIMEOUT)
        _log(f"[shutdown] chest_state exited cleanly rc={child.returncode}", log_fh)
    except subprocess.TimeoutExpired:
        _log("[shutdown] chest_state did not exit in 2s, sending SIGKILL", log_fh)
        try:
            child.kill()
        except Exception:
            pass
    except Exception as e:
        _log(f"[shutdown] error stopping chest_state: {e}", log_fh)
    _log("[shutdown] STOPPED. (TBH and cube plugin untouched.)", log_fh)
    log_fh.close()
    return 0


# ---- main ------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Coordinate chest_state.py and observe the TBH cube plugin."
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Verify pre-flight and status wireup. Spawn chest_state in --dry-run too. "
             "Log only, no chest clicks.",
    )
    ap.add_argument(
        "--preview", action="store_true",
        help="chest_state --preview (cursor moves to click point but no clicks). "
             "Cube plugin remains observability-only.",
    )
    ap.add_argument(
        "--click", action="store_true",
        help="chest_state --click. Live mode. Stay at the keyboard.",
    )
    ap.add_argument(
        "--log", default="orchestrator.log",
        help="Orchestrator log path (default: orchestrator.log next to this script).",
    )
    args = ap.parse_args()

    # Mode discipline — exactly one mode flag, same shape as chest_state.py.
    flags = sum(bool(getattr(args, f)) for f in ("dry_run", "preview", "click"))
    if flags > 1:
        ap.error("--dry-run, --preview, and --click are mutually exclusive")
    click_mode = "click" if args.click else ("preview" if args.preview else "dry-run")

    log_path = Path(args.log)
    log_fh = open(log_path, "a", encoding="utf-8")

    _log(f"[boot] orchestrator v1 starting in {click_mode} mode", log_fh)
    _log(f"[boot] log file: {log_path.resolve()}", log_fh)

    # 1. Pre-flight: TBH running + cube plugin status file present.
    rc = preflight(log_fh)
    if rc != 0:
        log_fh.close()
        return rc

    # 2. Spawn chest_state.py as a child in the matching mode.
    script_dir = Path(__file__).resolve().parent
    chest_script = script_dir / "chest_state.py"
    if not chest_script.exists():
        _log(
            f"[boot] FAIL: chest_state.py not found at {chest_script}. "
            f"The orchestrator must live next to chest_state.py.",
            log_fh,
        )
        return 2
    chest_cmd = ["python", str(chest_script), f"--{click_mode}"]
    _log(f"[boot] spawning chest_state: {' '.join(chest_cmd)}", log_fh)
    try:
        child = subprocess.Popen(
            chest_cmd,
            cwd=str(script_dir),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            # On Windows, CREATE_NEW_PROCESS_GROUP so SIGINT doesn't propagate
            # from us to the child; we terminate it ourselves on shutdown.
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    except FileNotFoundError:
        _log(
            "[boot] FAIL: python not on PATH. On Admin, use `python` not `py`. "
            "(Memory note: py is NOT on PATH on Admin's PowerShell.)",
            log_fh,
        )
        return 2
    _log(f"[boot] chest_state child pid={child.pid}", log_fh)

    # 3. Status-poll loop. Ctrl-C exits cleanly via the except clause below.
    try:
        run_loop(child, log_fh)
    except KeyboardInterrupt:
        _log("\n[main] Ctrl-C received. Shutting down...", log_fh)
        return shutdown(child, log_fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
