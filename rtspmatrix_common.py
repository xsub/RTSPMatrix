# rtspmatrix_common.py
#
# Code shared between the classic (rtspmatrix.py) and virtual
# (rtspmatrix-vitual.py) viewers.  Keep this module dependency-light:
# stdlib + python-vlc + (optionally) PyQt5 are fine, but no project-local
# imports — both viewers must be able to import it cheaply.

import json
import logging
import os
import sys
import threading
import time

import vlc


log = logging.getLogger("rtspmatrix")


# ---------- JSON helpers ----------

def safe_read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        log.exception("Failed to read JSON %s", path)
        return default


def safe_write_json(path: str, obj):
    """Atomic write: serialize to <path>.tmp, then os.replace into place."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------- channel labels ----------

def parse_labels(raw: str) -> list:
    """Parse `labels = { A, B, C, ... }` (with optional surrounding braces and
    comma separators) into a 16-entry list.  Missing entries are None.

    The braces and the trailing entries are both optional.  Empty strings are
    skipped.  Long lists are truncated to 16.
    """
    if not raw:
        return [None] * 16
    s = raw.strip()
    if s.startswith("{"):
        s = s[1:]
    if s.endswith("}"):
        s = s[:-1]
    parts = [p.strip() for p in s.split(",")]
    parts = [p for p in parts if p != ""]
    return [parts[i] if i < len(parts) else None for i in range(16)]


# ---------- VLC player disposal ----------
#
# All in-flight disposal threads, so MainWindow.cleanup() can join them
# before releasing the libVLC instance.  Without this the daemon threads
# may still be running stop()/release() on a MediaPlayer when the parent
# vlc.Instance is freed -> use-after-free crash on exit.
_disposal_threads = []
_disposal_lock = threading.Lock()


def dispose_player_async(p: vlc.MediaPlayer):
    """Stop+release a MediaPlayer on a background thread.

    Detach the OS window handle SYNCHRONOUSLY on the GUI thread first:
    libVLC's render thread may otherwise still try to draw into a freed
    HWND/XWindow/NSView, which is a use-after-free crash on macOS in
    particular.
    """
    try:
        if sys.platform.startswith("linux"):
            p.set_xwindow(0)
        elif sys.platform.startswith("win"):
            p.set_hwnd(0)
        elif sys.platform.startswith("darwin"):
            p.set_nsobject(0)
        else:
            p.set_xwindow(0)
    except Exception:
        log.exception("Detach OS handle failed during disposal")

    def _w():
        try:
            p.stop()
        except Exception:
            log.exception("MediaPlayer.stop() failed during disposal")
        try:
            p.release()
        except Exception:
            log.exception("MediaPlayer.release() failed during disposal")

    t = threading.Thread(target=_w, daemon=True)
    with _disposal_lock:
        _disposal_threads.append(t)
    t.start()


def join_disposal_threads(timeout_total: float = 3.0):
    """Wait (with a global timeout) for all in-flight disposal threads."""
    deadline = time.monotonic() + timeout_total
    with _disposal_lock:
        threads = list(_disposal_threads)
        _disposal_threads.clear()
    for t in threads:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            break
        try:
            t.join(timeout=remaining)
        except Exception:
            pass


def bind_player_to_window(player: vlc.MediaPlayer, winid: int):
    """Bind a libVLC player to an OS-level window handle for the current
    platform.  Pass 0 to detach."""
    wid = int(winid)
    if sys.platform.startswith("linux"):
        player.set_xwindow(wid)
    elif sys.platform.startswith("win"):
        player.set_hwnd(wid)
    elif sys.platform.startswith("darwin"):
        player.set_nsobject(wid)
    else:
        player.set_xwindow(wid)


# ---------- logging setup ----------

def setup_logging(level_name: str = "INFO", log_file: str = ""):
    """Configure the rtspmatrix logger.

    - Always logs to stderr.
    - If `log_file` is non-empty, also logs to that file (append mode).
    - Idempotent: calling it twice does not duplicate handlers.
    """
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    log.setLevel(level)
    log.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    has_stream = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in log.handlers
    )
    if not has_stream:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)

    if log_file:
        already = any(
            isinstance(h, logging.FileHandler)
            and getattr(h, "baseFilename", "") == os.path.abspath(log_file)
            for h in log.handlers
        )
        if not already:
            try:
                fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
                fh.setFormatter(fmt)
                log.addHandler(fh)
            except Exception as exc:
                log.warning("Could not open log file %s: %s", log_file, exc)
