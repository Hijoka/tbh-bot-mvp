"""tbh_launcher.py — single tray-icon entry point for tbh-bot-mvp.

Wraps chest_state.py, cube_state.py, and orchestrator.py behind a system-tray
icon and menu, matching the launch UX of preschian/tbh-presence (one icon,
menu to arm modes, no console window).

LAUNCH SHAPE
------------
- Auto-discover the game: $env:TBH_GAME_DIR first, then Steam default
  ("C:\\Program Files (x86)\\Steam\\steamapps\\common\\TaskbarHero").
  If neither exists, prompt once via tkinter and persist to config.
- Tray icon (pystray) with menu:
    [ ] Chest    — toggles `python chest_state.py --click`
    [ ] Cube     — toggles `python cube_state.py  --click`
    [ ] Orch     — toggles `python orchestrator.py --click`
    Settings…   — re-runs the game-path prompt
    Open log     — opens the log file in the default viewer
    Quit
- Each mode runs in its own subprocess thread. Disarming kills the child.
- All child stdout/stderr are tee'd to logs/tbh_launcher.log.
- Tray tooltip shows the latest line from any child, refreshed every 2 s.

OPPORTUNISTIC, NEVER INTRUSIVE (Bob's rule #2)
----------------------------------------------
Every mode starts DISABLED. Nothing runs until you click its menu item.
Disarming kills the child immediately. No wall-clock firing, no auto-start.

USAGE (PowerShell, double-clickable via launch.bat):
    python tbh_launcher.py
"""

from __future__ import annotations

# CRITICAL: launch.bat uses pythonw.exe (windowless). Any unhandled exception
# at import time disappears silently. We must catch everything and write to
# logs/tbh_launcher.log BEFORE doing heavy imports.

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Bootstrap: write a log file first, then import heavy deps.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
LOG_DIR = REPO_ROOT / "logs"
LOG_PATH = LOG_DIR / "tbh_launcher.log"
CONFIG_PATH = REPO_ROOT / "tbh_launcher.json"

LOG_DIR.mkdir(parents=True, exist_ok=True)

def _bootstrap_log(msg: str) -> None:
    """Write a line to the log even before Logger class exists."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8", buffering=1) as fh:
            ts = time.strftime("%H:%M:%S")
            fh.write(f"[{ts}] [bootstrap] {msg}\n")
    except Exception:
        pass  # if even this fails, there's nothing we can do

_bootstrap_log(f"launcher starting, pid={os.getpid()}, python={sys.executable}")
_bootstrap_log(f"sys.version={sys.version.split()[0]}, platform={sys.platform}")

def _fatal(msg: str, exc: Optional[BaseException] = None) -> None:
    _bootstrap_log(f"FATAL: {msg}")
    if exc is not None:
        _bootstrap_log("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    # Also try to write to stderr in case a console exists (running directly).
    try:
        sys.stderr.write(f"tbh_launcher: {msg}\n")
        if exc is not None:
            traceback.print_exc()
    except Exception:
        pass

try:
    import subprocess
    from tkinter import simpledialog  # lazy-imported inside prompt_for_game_dir
    import tkinter as tk
    _bootstrap_log("stdlib imports OK")
except Exception as e:
    _fatal("stdlib import failed", e)
    sys.exit(1)

# Lazy GUI imports — only required when actually showing the tray / dialog.
def _import_gui():
    global pystray, Item, Image, ImageDraw
    try:
        import pystray
        from pystray import MenuItem as Item
        from PIL import Image, ImageDraw
        _bootstrap_log("pystray + pillow imported OK")
        return True
    except Exception as e:
        _fatal("pystray/pillow import failed. Run: pip install pystray pillow", e)
        return False


# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

STEAM_DEFAULT = r"C:\Program Files (x86)\Steam\steamapps\common\TaskbarHero"
GAME_EXE = "TaskBarHero.exe"

PYTHON_EXE = sys.executable  # use the same interpreter that launched us

# Mode definitions: (label, script, default_args, env key for the path)
MODES = [
    ("Chest", "chest_state.py", ["--click"]),
    ("Cube", "cube_state.py", ["--click"]),
    ("Orch", "orchestrator.py", ["--click"]),
]


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def game_dir_valid(path: str) -> bool:
    if not path:
        return False
    p = Path(path)
    return p.exists() and (p / GAME_EXE).exists()


def discover_game_dir() -> Optional[str]:
    """env var → Steam default → None (caller will prompt)."""
    env = os.environ.get("TBH_GAME_DIR")
    if env and game_dir_valid(env):
        return env
    if game_dir_valid(STEAM_DEFAULT):
        return STEAM_DEFAULT
    return None


def prompt_for_game_dir() -> Optional[str]:
    """One-time modal prompt. Returns the path string or None on cancel."""
    root = tk.Tk()
    root.withdraw()
    try:
        answer = simpledialog.askstring(
            "TBH Launcher — game folder",
            "Couldn't find TaskBarHero.exe.\n\n"
            "Enter the full path to your TaskbarHero install folder\n"
            "(the one containing TaskBarHero.exe).\n\n"
            f"Default Steam location:\n{STEAM_DEFAULT}",
            initialvalue=STEAM_DEFAULT,
        )
        if answer and game_dir_valid(answer):
            return answer
        return None
    finally:
        root.destroy()


def ensure_game_dir(cfg: dict, write) -> str:
    """Resolve and persist the game dir; prompt if missing.

    `write` is a callable that takes a single str (the Logger.write method).
    """
    gdir = cfg.get("game_dir") or discover_game_dir()
    if not gdir:
        write("game dir not found — prompting")
        gdir = prompt_for_game_dir()
    if not gdir:
        write("FATAL: no valid game dir; quitting")
        raise SystemExit("No valid TaskbarHero install path. Set TBH_GAME_DIR or rerun and enter it.")
    if not game_dir_valid(gdir):
        write(f"WARN: saved game_dir is stale: {gdir}")
        gdir = prompt_for_game_dir()
        if not gdir:
            raise SystemExit("No valid TaskbarHero install path.")
    cfg["game_dir"] = gdir
    save_config(cfg)
    write(f"game_dir = {gdir}")
    return gdir


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class Logger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self.last_line = "starting…"

    def write(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._lock:
            self._fh.write(line + "\n")
            self.last_line = msg

    def close(self) -> None:
        with self._lock:
            self._fh.close()


# ---------------------------------------------------------------------------
# Subprocess supervision
# ---------------------------------------------------------------------------

class ModeRunner:
    """Owns one child subprocess. Arms/disarms via start()/stop()."""

    def __init__(self, label: str, script: str, args: list[str], env_extras: dict, log: Logger):
        self.label = label
        self.script = script
        self.args = args
        self.env_extras = env_extras
        self.log = log
        self.proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self.enabled = False

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return  # already running
        cmd = [PYTHON_EXE, "-u", str(REPO_ROOT / self.script), *self.args]
        env = os.environ.copy()
        env.update(self.env_extras)
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            self.log.write(f"[{self.label}] failed to start: {e}")
            return
        self.enabled = True
        self.log.write(f"[{self.label}] started pid={self.proc.pid} cmd={' '.join(cmd)}")
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def stop(self) -> None:
        if not self.proc:
            return
        self.log.write(f"[{self.label}] stopping")
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.log.write(f"[{self.label}] terminate timed out, killing")
            self.proc.kill()
        except Exception as e:
            self.log.write(f"[{self.label}] stop error: {e}")
        finally:
            self.proc = None
            self.enabled = False

    def _drain(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            try:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.log.write(f"[{self.label}] {text}")
            except Exception:
                pass
        rc = self.proc.wait() if self.proc else -1
        self.log.write(f"[{self.label}] exited rc={rc}")
        self.enabled = False
        self.proc = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------

def make_icon_image():
    """Generate a tiny helmet-ish icon at runtime — no asset file needed."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(40, 40, 50, 255), outline=(200, 200, 220, 255), width=2)
    d.rectangle((14, 28, 50, 38), fill=(120, 180, 255, 255))
    d.ellipse((10, 10, 16, 16), fill=(220, 220, 230, 255))
    d.ellipse((48, 10, 54, 16), fill=(220, 220, 230, 255))
    return img


class App:
    def __init__(self):
        self.log = Logger(LOG_PATH)
        self.cfg = load_config()
        self.gdir = ensure_game_dir(self.cfg, self.log.write)
        env_extras = {"TBH_GAME_DIR": self.gdir}
        self.runners = {
            label: ModeRunner(label, script, args, env_extras, self.log)
            for (label, script, args) in MODES
        }
        self.icon = None  # type: ignore[assignment]

    # ---- menu actions ----------------------------------------------------
    # pystray invokes callbacks with ONE arg (the MenuItem). Methods that take
    # `item` match that contract. _actions is used as a stable handle for the
    # pystray.Item constructors below.
    def _toggle_chest(self, item): self._toggle("Chest")
    def _toggle_cube(self, item): self._toggle("Cube")
    def _toggle_orch(self, item): self._toggle("Orch")
    def _is_checked(self, label: str) -> bool:
        return self.runners[label].enabled

    def _toggle(self, label: str) -> None:
        r = self.runners[label]
        if r.enabled:
            r.stop()
        else:
            r.start()
        self._refresh_tooltip()

    def open_settings(self, item) -> None:
        new = prompt_for_game_dir()
        if new:
            self.cfg["game_dir"] = new
            save_config(self.cfg)
            self.gdir = new
            for r in self.runners.values():
                r.env_extras["TBH_GAME_DIR"] = new
            self.log.write(f"game_dir updated to {new}")
            self._refresh_tooltip()

    def open_log(self, item) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(LOG_PATH))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(LOG_PATH)])
        except Exception as e:
            self.log.write(f"could not open log: {e}")

    def quit(self, item) -> None:
        self.log.write("quit requested")
        for r in self.runners.values():
            r.stop()
        if self.icon:
            self.icon.stop()
        self.log.close()

    # ---- tooltip / supervision -------------------------------------------
    # Windows NOTIFYICONDATAW.szTip is capped at 128 chars. Keep the tooltip
    # short — show modes, drop the long game path, show a short status line.
    def _tooltip_text(self) -> str:
        marks = " ".join(
            f"{'●' if r.alive() else '○'}{label[0]}"
            for label, r in self.runners.items()
        )
        status = self.log.last_line[:40]
        # Build the line, then truncate to fit.
        line = f"TBH {marks} — {status}"
        return line[:127]

    def _refresh_tooltip(self) -> None:
        if self.icon:
            try:
                self.icon.title = self._tooltip_text()
            except Exception:
                pass

    def _supervise(self) -> None:
        # Periodically update tooltip and notice crashed children.
        while True:
            for r in self.runners.values():
                if r.enabled and not r.alive():
                    self.log.write(f"[{r.label}] child died unexpectedly")
                    r.enabled = False
            self._refresh_tooltip()
            time.sleep(2)

    def run(self) -> None:
        menu = pystray.Menu(
            Item("Chest", self._toggle_chest,
                 checked=lambda item: self._is_checked("Chest"),
                 radio=False),
            Item("Cube", self._toggle_cube,
                 checked=lambda item: self._is_checked("Cube"),
                 radio=False),
            Item("Orch", self._toggle_orch,
                 checked=lambda item: self._is_checked("Orch"),
                 radio=False),
            pystray.Menu.SEPARATOR,
            Item("Settings…", self.open_settings),
            Item("Open log", self.open_log),
            Item("Quit", self.quit),
        )
        self.icon = pystray.Icon(
            "tbh_launcher",
            make_icon_image(),
            title=self._tooltip_text(),
            menu=menu,
        )
        threading.Thread(target=self._supervise, daemon=True).start()
        self.log.write("tray icon shown; all modes DISABLED — pick from menu to arm")
        self.icon.run()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> int:
    if not _import_gui():
        return 2  # log already has the diagnosis
    app = None
    try:
        app = App()
        app.run()
    except KeyboardInterrupt:
        if app is not None:
            try:
                app.quit(None)
            except Exception:
                pass
    except Exception as e:
        _fatal("unhandled exception in App.run", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
