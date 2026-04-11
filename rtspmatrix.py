# rtspmatrix.py
# Changes vs your current:
# - VLC quieter: --quiet / --verbose=0
# - RTSP hardening: :rtsp-tcp + :rtsp-keepalive + :rtsp-timeout
# - macOS deadlock mitigation: disable HW decode (VideoToolbox) via --avcodec-hw=none and :avcodec-hw=none
# - Auto-resume: when stream drops or stalls -> exponential backoff retry, using NEW MediaPlayer on each retry
# - AboutDialog fixed (QT_VERSION_STR / PYQT_VERSION_STR)

import sys
import os
import json
import math
import signal
import platform
import threading
import time
import configparser
import importlib.metadata

import vlc
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QT_VERSION_STR, PYQT_VERSION_STR
from PyQt5.QtGui import QGuiApplication, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame,
    QVBoxLayout, QHBoxLayout, QGridLayout, QSizePolicy,
    QPushButton, QButtonGroup, QLabel, QComboBox,
    QDialog, QTextEdit, QInputDialog, QMessageBox
)


# ---------- utils ----------

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
    particular.  Same fix lives in the virtual variant.
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
        pass

    def _w():
        try:
            p.stop()
        except Exception:
            pass
        try:
            p.release()
        except Exception:
            pass

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


def safe_read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def safe_write_json(path: str, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------- config ----------

class RtspConfig:
    def __init__(self, ini_path: str = "rtsp.ini"):
        cp = configparser.RawConfigParser(inline_comment_prefixes=())
        ok = cp.read(ini_path, encoding="utf-8")
        if not ok:
            raise FileNotFoundError(f"Missing config file: {ini_path}")

        s = cp["rtsp"]
        self.host = s.get("host", "127.0.0.1")
        self.port = s.getint("port", 554)
        self.user = s.get("user", "")
        self.password = s.get("password", "")
        self.path = s.get("path", "/cam/realmonitor")
        self.subtype = s.getint("subtype", 0)
        self.tcp = s.getint("tcp", 1) != 0
        self.network_caching_ms = s.getint("network_caching_ms", 250)
        self.open_timeout_ms = s.getint("open_timeout_ms", 2500)

        # resiliency / retry tuning
        self.poll_interval_ms = s.getint("poll_interval_ms", 500)
        self.stall_timeout_ms = s.getint("stall_timeout_ms", 6000)
        self.retry_base_ms = s.getint("retry_base_ms", 1000)
        self.retry_max_ms = s.getint("retry_max_ms", 20000)

        # live555 idle timeout (seconds, best-effort)
        self.rtsp_timeout_s = s.getint("rtsp_timeout_s", 4)

        # macOS HW decode can deadlock on glitchy RTSP
        self.disable_hw_decode = s.getint("disable_hw_decode", 1) != 0

        a = cp["app"] if cp.has_section("app") else {}
        self.title = a.get("title", "RTSPMatrix")
        self.default_panes = int(a.get("default_panes", 4))
        self.views_file = a.get("views_file", "views.json")
        self.state_file = a.get("state_file", "state.json")

        raw_labels = cp["view"].get("labels", "") if cp.has_section("view") else ""
        self.labels = self._parse_labels(raw_labels)

    def url(self, channel: int) -> str:
        ch = max(1, min(16, int(channel)))
        return f"rtsp://{self.host}:{self.port}{self.path}?channel={ch}&subtype={self.subtype}"

    @staticmethod
    def _parse_labels(raw: str) -> list:
        """Parse `labels = { A, B, C, ... }` into a 16-entry list (None for missing)."""
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

    def label_for(self, ch):
        try:
            ch = int(ch)
        except Exception:
            return None
        if 1 <= ch <= 16:
            return self.labels[ch - 1]
        return None

    def is_channel_active(self, ch) -> bool:
        lbl = self.label_for(ch)
        return lbl is not None and lbl != "BRAK"

    def channel_text(self, ch) -> str:
        lbl = self.label_for(ch)
        if lbl and lbl != "BRAK":
            return f"CH{int(ch)} {lbl}"
        return f"CH{int(ch)}"


# ---------- views ----------

class ViewsStore:
    def __init__(self, path: str):
        self.path = path
        self.data = safe_read_json(self.path, {"views": {}})
        if "views" not in self.data or not isinstance(self.data["views"], dict):
            self.data = {"views": {}}

    def list_names(self):
        return sorted(self.data["views"].keys(), key=str.lower)

    def get(self, name: str):
        return self.data["views"].get(name)

    def save(self, name: str, panes: int, assign: list):
        panes = int(max(1, min(16, panes)))
        assign = (assign or [])[:16]
        self.data["views"][name] = {"panes": panes, "assign": assign}
        safe_write_json(self.path, self.data)

    def delete(self, name: str):
        if name in self.data["views"]:
            del self.data["views"][name]
            safe_write_json(self.path, self.data)


# ---------- UI ----------

class ClickableFrame(QFrame):
    clicked = pyqtSignal()
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class PlayerPane(QWidget):
    """
    Auto-resume:
      - state polling detects drop/stall
      - schedules exponential retry
      - retry uses a NEW MediaPlayer (old disposed async) to escape broken live555 state
    """
    def __init__(self, pane_id: int, vlc_instance: vlc.Instance, cfg: RtspConfig, on_focus):
        super().__init__()
        self.pane_id = pane_id
        self.vlc = vlc_instance
        self.cfg = cfg
        self.on_focus = on_focus

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.frame = ClickableFrame(self)
        self.frame.setFrameShape(QFrame.Box)
        self.frame.setStyleSheet("background: black; border: 3px solid #333;")
        self.frame.setMinimumSize(240, 160)
        self.frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Force a real OS-level window handle independent of the parent
        # widget, so VLC binds to the video frame, not the toplevel.  Without
        # this, every player can end up rendering on top of every other.
        self.frame.setAttribute(Qt.WA_NativeWindow, True)
        self.frame.clicked.connect(self._clicked)

        self.label = QLabel(f"Pane {pane_id}: Idle", self)
        self.label.setStyleSheet("color: #ddd;")
        self.label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        l = QVBoxLayout(self)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(6)
        l.addWidget(self.frame, 1)
        l.addWidget(self.label, 0)

        self.player = None
        self.assigned_channel = None

        # retry/stall tracking
        self._retry_attempt = 0
        self._retry_pending = False
        self._opening_since_ts = 0.0
        self._last_playing_ts = 0.0

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(self.cfg.poll_interval_ms)
        self.poll_timer.timeout.connect(self._poll_state)

        self.open_timer = QTimer(self)
        self.open_timer.setSingleShot(True)
        self.open_timer.timeout.connect(self._open_timeout)

        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self._retry_now)

        QTimer.singleShot(0, self._ensure_player)

    def _clicked(self):
        self.on_focus(self.pane_id)

    def set_focused(self, focused: bool):
        if focused:
            self.frame.setStyleSheet("background: black; border: 3px solid #66aaff;")
        else:
            self.frame.setStyleSheet("background: black; border: 3px solid #333;")

    def _bind_player_window(self, player: vlc.MediaPlayer):
        wid = int(self.frame.winId())
        if sys.platform.startswith("linux"):
            player.set_xwindow(wid)
        elif sys.platform.startswith("win"):
            player.set_hwnd(wid)
        elif sys.platform.startswith("darwin"):
            player.set_nsobject(wid)
        else:
            player.set_xwindow(wid)

    def _detach_player_window(self):
        """Tell libVLC to stop drawing into our QFrame, but keep the player
        alive.  Used during grid rebuilds, where the QFrame's native window
        handle is about to be invalidated by reparenting."""
        if self.player is None:
            return
        try:
            if sys.platform.startswith("linux"):
                self.player.set_xwindow(0)
            elif sys.platform.startswith("win"):
                self.player.set_hwnd(0)
            elif sys.platform.startswith("darwin"):
                self.player.set_nsobject(0)
        except Exception:
            pass

    def rebind_to_current_frame(self):
        """Re-attach the live player to the (possibly fresh) QFrame winId
        after a grid rebuild.  Cheap: no reconnect, no media reload."""
        if self.player is None:
            return
        try:
            self._bind_player_window(self.player)
        except Exception:
            pass

    def _new_player(self) -> vlc.MediaPlayer:
        p = self.vlc.media_player_new()
        self._bind_player_window(p)
        return p

    def _ensure_player(self):
        if self.player is None:
            self.player = self._new_player()

    def _swap_player(self) -> vlc.MediaPlayer:
        old = self.player
        self.player = self._new_player()
        if old is not None:
            dispose_player_async(old)
        return self.player

    def _cancel_retry(self):
        self._retry_pending = False
        if self.retry_timer.isActive():
            self.retry_timer.stop()

    def stop_to_idle(self):
        self._cancel_retry()
        if self.open_timer.isActive():
            self.open_timer.stop()
        if self.poll_timer.isActive():
            self.poll_timer.stop()
        if self.player is not None:
            dispose_player_async(self.player)
        self.player = self._new_player()
        self.assigned_channel = None
        self._retry_attempt = 0
        self._opening_since_ts = 0.0
        self._last_playing_ts = 0.0
        self.label.setText(f"Pane {self.pane_id}: Idle")

    def play_channel(self, ch: int):
        ch = int(max(1, min(16, ch)))

        if not self.cfg.is_channel_active(ch):
            self.stop_to_idle()
            return

        # Idempotent: already playing this exact channel and the player is in
        # a healthy state -> no-op.  Avoids the wasteful "kill the player and
        # start over" cycle on every double-press of a channel button.
        if self.assigned_channel == ch and self.player is not None:
            try:
                st = self.player.get_state()
            except Exception:
                st = vlc.State.Error
            if st in (vlc.State.Playing, vlc.State.Opening, vlc.State.Buffering):
                return

        self._ensure_player()
        self._cancel_retry()
        self._retry_attempt = 0

        self.assigned_channel = ch
        now = time.monotonic()
        self._opening_since_ts = now
        self._last_playing_ts = 0.0

        self._start_play(ch, reason="play")
        if not self.poll_timer.isActive():
            self.poll_timer.start()

    def _start_play(self, ch: int, reason: str):
        p = self._swap_player()

        url = self.cfg.url(ch)
        media = self.vlc.media_new(url)

        if self.cfg.user:
            media.add_option(f":rtsp-user={self.cfg.user}")
        if self.cfg.password:
            media.add_option(f":rtsp-pwd={self.cfg.password}")

        # harden RTSP
        if self.cfg.tcp:
            media.add_option(":rtsp-tcp")
        media.add_option(":rtsp-keepalive")
        media.add_option(f":rtsp-timeout={self.cfg.rtsp_timeout_s}")

        # jitter buffer
        media.add_option(f":network-caching={self.cfg.network_caching_ms}")

        # macOS HW decode deadlock mitigation
        if self.cfg.disable_hw_decode:
            media.add_option(":avcodec-hw=none")

        p.set_media(media)
        try:
            p.play()
        except Exception:
            pass

        self.label.setText(f"Pane {self.pane_id}: Opening {self.cfg.channel_text(ch)} ({reason})")
        self.open_timer.start(self.cfg.open_timeout_ms)

    def _schedule_retry(self, why: str):
        if self._retry_pending or self.assigned_channel is None:
            return
        self._retry_pending = True
        self._retry_attempt += 1

        delay = min(self.cfg.retry_max_ms, self.cfg.retry_base_ms * (2 ** max(0, self._retry_attempt - 1)))
        ch = self.assigned_channel
        self.label.setText(f"Pane {self.pane_id}: {self.cfg.channel_text(ch)} lost ({why}), retry in {delay}ms")
        self.retry_timer.start(delay)

    def _retry_now(self):
        self._retry_pending = False
        if self.assigned_channel is None:
            return
        ch = self.assigned_channel
        self._opening_since_ts = time.monotonic()
        self._start_play(ch, reason=f"retry#{self._retry_attempt}")

    def _poll_state(self):
        if self.player is None or self.assigned_channel is None:
            return
        ch = self.assigned_channel
        now = time.monotonic()

        try:
            st = self.player.get_state()
        except Exception:
            st = vlc.State.Error

        if st == vlc.State.Playing:
            self._last_playing_ts = now
            self._opening_since_ts = 0.0
            self._retry_attempt = 0
            if self.open_timer.isActive():
                self.open_timer.stop()
            self.label.setText(f"Pane {self.pane_id}: {self.cfg.channel_text(ch)} playing")
            return

        if st in (vlc.State.Opening, vlc.State.Buffering):
            if self._opening_since_ts > 0.0:
                if (now - self._opening_since_ts) * 1000.0 > self.cfg.stall_timeout_ms:
                    self._schedule_retry("stall(opening/buffering)")
            return

        if st in (vlc.State.Error, vlc.State.Ended, vlc.State.Stopped):
            self._schedule_retry(f"state={st}")
            return

        # other non-playing; if we were playing before and now stalled too long
        if self._last_playing_ts > 0.0:
            if (now - self._last_playing_ts) * 1000.0 > self.cfg.stall_timeout_ms:
                self._schedule_retry(f"stall(state={st})")

    def _open_timeout(self):
        if self.player is None or self.assigned_channel is None:
            return
        try:
            st = self.player.get_state()
        except Exception:
            st = vlc.State.Error
        if st != vlc.State.Playing:
            self._schedule_retry("open-timeout")

    def shutdown(self):
        self._cancel_retry()
        if self.open_timer.isActive():
            self.open_timer.stop()
        if self.poll_timer.isActive():
            self.poll_timer.stop()
        if self.player is not None:
            dispose_player_async(self.player)
            self.player = None


ABOUT_LOGO_CANDIDATES = (
    "RTSPMatrix_logo.png",
    os.path.join("assets", "RTSPMatrix_logo.png"),
    "puffy-clouds-logo.png",
    os.path.join("assets", "puffy-clouds-logo.png"),
)


def _find_about_logo():
    for p in ABOUT_LOGO_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


class AboutDialog(QDialog):
    def __init__(self, parent, cfg: RtspConfig, panes: int):
        super().__init__(parent)
        self.setWindowTitle("About RTSPMatrix")
        self.resize(720, 620)

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 12)
        v.setSpacing(12)

        # ---- branding header ----
        logo_path = _find_about_logo()
        logo_is_rtspmatrix = bool(logo_path and "RTSPMatrix_logo" in logo_path)
        if logo_path:
            pm = QPixmap(logo_path)
            if not pm.isNull():
                scaled = pm.scaledToWidth(560, Qt.SmoothTransformation)
                logo = QLabel(self)
                logo.setPixmap(scaled)
                logo.setAlignment(Qt.AlignCenter)
                # The RTSPMatrix logo ships with its own dark background and
                # baked-in title; don't put it on a white card.  The Puffy
                # Clouds fallback is dark-on-white, so it gets the card.
                if not logo_is_rtspmatrix:
                    logo.setStyleSheet("background: white; padding: 12px; border-radius: 6px;")
                v.addWidget(logo, 0, Qt.AlignCenter)

        # The RTSPMatrix logo already renders the app name + tagline.  For
        # the fallback (Puffy Clouds) we still need an explicit title row.
        if not logo_is_rtspmatrix:
            title = QLabel(f"<h2 style='margin:0'>{cfg.title}</h2>", self)
            title.setAlignment(Qt.AlignCenter)
            v.addWidget(title)

            subtitle = QLabel("RTSP grid viewer for Dahua / compatible DVR-NVR devices",
                              self)
            subtitle.setAlignment(Qt.AlignCenter)
            subtitle.setStyleSheet("color: #aaa;")
            v.addWidget(subtitle)

        # ---- runtime / config info ----
        text = QTextEdit(self)
        text.setReadOnly(True)
        text.setStyleSheet("font-family: monospace; font-size: 11px;")

        py_ver = sys.version.replace("\n", " ")
        qt_ver = QT_VERSION_STR
        pyqt_ver = PYQT_VERSION_STR

        try:
            pv_vlc = importlib.metadata.version("python-vlc")
        except Exception:
            pv_vlc = "unknown"

        try:
            libvlc_ver = vlc.libvlc_get_version()
            if isinstance(libvlc_ver, bytes):
                libvlc_ver = libvlc_ver.decode("utf-8", errors="ignore")
            else:
                libvlc_ver = str(libvlc_ver)
        except Exception:
            libvlc_ver = "unknown"

        info = []
        info.append(f"App: {cfg.title}")
        info.append(f"OS: {platform.platform()}")
        info.append(f"Python: {py_ver}")
        info.append(f"Qt: {qt_ver}")
        info.append(f"PyQt5: {pyqt_ver}")
        info.append(f"python-vlc: {pv_vlc}")
        info.append(f"libVLC: {libvlc_ver}")
        info.append("")
        info.append(f"RTSP host: {cfg.host}:{cfg.port}")
        info.append(f"RTSP path: {cfg.path}")
        info.append(f"TCP: {cfg.tcp}")
        info.append(f"network_caching_ms: {cfg.network_caching_ms}")
        info.append(f"open_timeout_ms: {cfg.open_timeout_ms}")
        info.append(f"poll_interval_ms: {cfg.poll_interval_ms}")
        info.append(f"stall_timeout_ms: {cfg.stall_timeout_ms}")
        info.append(f"retry_base_ms: {cfg.retry_base_ms}")
        info.append(f"retry_max_ms: {cfg.retry_max_ms}")
        info.append(f"rtsp_timeout_s: {cfg.rtsp_timeout_s}")
        info.append(f"disable_hw_decode: {cfg.disable_hw_decode}")
        info.append("")
        info.append(f"Active panes: {panes}")
        info.append(f"Config: rtsp.ini")
        info.append(f"Views file: {cfg.views_file}")
        info.append(f"State file: {cfg.state_file}")

        content = "\n".join(info)
        text.setPlainText(content)
        v.addWidget(text, 1)

        # ---- buttons ----
        btn_copy = QPushButton("Copy", self)
        btn_close = QPushButton("Close", self)
        btn_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(content))
        btn_close.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn_copy)
        row.addWidget(btn_close)
        v.addLayout(row)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._cleaned = False

        self.cfg = RtspConfig("rtsp.ini")
        self.views = ViewsStore(self.cfg.views_file)

        vlc_args = [
            "--quiet",
            "--verbose=0",
            "--no-video-title-show",
        ]
        if self.cfg.tcp:
            vlc_args.append("--rtsp-tcp")
        vlc_args.append(f"--network-caching={self.cfg.network_caching_ms}")
        if self.cfg.disable_hw_decode:
            vlc_args.append("--avcodec-hw=none")
        self.vlc = vlc.Instance(vlc_args)

        self.setWindowTitle(self.cfg.title)
        self.resize(1600, 1000)

        root = QWidget(self)
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(8)

        self.btn_channels = QButtonGroup(self)
        self.btn_channels.setExclusive(False)
        grid_btn = QGridLayout()
        grid_btn.setSpacing(6)
        for i, ch in enumerate(range(1, 17)):
            lbl = self.cfg.label_for(ch)
            text = f"{ch}\n{lbl}" if lbl else str(ch)
            b = QPushButton(text, self)
            b.setMinimumHeight(46)
            b.setMinimumWidth(110)
            if not self.cfg.is_channel_active(ch):
                b.setEnabled(False)
                b.setToolTip(f"CH{ch}: no camera")
            self.btn_channels.addButton(b, ch)
            grid_btn.addWidget(b, i // 8, i % 8)
        for c in range(8):
            grid_btn.setColumnStretch(c, 1)
        main.addLayout(grid_btn)

        controls = QHBoxLayout()
        controls.setSpacing(10)

        controls.addWidget(QLabel("Streams:", self))
        self.cmb_panes = QComboBox(self)
        self.cmb_panes.addItems([str(i) for i in range(1, 17)])
        controls.addWidget(self.cmb_panes)

        controls.addWidget(QLabel("View:", self))
        self.cmb_views = QComboBox(self)
        controls.addWidget(self.cmb_views)

        self.btn_apply_view = QPushButton("Apply", self)
        self.btn_save_view = QPushButton("Save", self)
        self.btn_delete_view = QPushButton("Delete", self)
        self.btn_clear_pane = QPushButton("Clear pane", self)
        self.btn_about = QPushButton("About", self)

        controls.addWidget(self.btn_apply_view)
        controls.addWidget(self.btn_save_view)
        controls.addWidget(self.btn_delete_view)
        controls.addWidget(self.btn_clear_pane)
        controls.addStretch(1)
        controls.addWidget(self.btn_about)

        main.addLayout(controls)

        self.info = QLabel("Active pane: 1", self)
        self.info.setStyleSheet("color: #ddd;")
        main.addWidget(self.info)

        self.panes_grid = QGridLayout()
        self.panes_grid.setSpacing(8)
        main.addLayout(self.panes_grid, 1)

        self.panes = [PlayerPane(pid, self.vlc, self.cfg, self.set_active_pane)
                      for pid in range(1, 17)]
        self.grid_rows = 0
        self.grid_cols = 0

        self.active_pane = 1
        # Start at 0 so the first _apply_panes_visibility call treats every
        # visible pane as "newly revealed" and (re)plays its saved channel.
        self.visible_panes = 0

        self._reload_views_combo()

        self.btn_channels.buttonClicked[int].connect(self.on_channel_pressed)
        self.cmb_panes.currentTextChanged.connect(self._on_panes_changed)
        self.btn_apply_view.clicked.connect(self.apply_selected_view)
        self.btn_save_view.clicked.connect(self.save_view_dialog)
        self.btn_delete_view.clicked.connect(self.delete_selected_view)
        self.btn_clear_pane.clicked.connect(self.clear_active_pane)
        self.btn_about.clicked.connect(self.show_about)

        self._restore_state()
        # _restore_state may have set self.visible_panes; capture and reset
        # so the dropdown change below routes everything through one path.
        target_panes = self.visible_panes if self.visible_panes > 0 \
            else max(1, min(16, int(self.cfg.default_panes)))
        self.visible_panes = 0

        self.cmb_panes.blockSignals(True)
        self.cmb_panes.setCurrentText(str(target_panes))
        self.cmb_panes.blockSignals(False)
        self._apply_panes_visibility(target_panes)
        self._update_focus()

    def _reload_views_combo(self):
        names = self.views.list_names()
        self.cmb_views.clear()
        self.cmb_views.addItem("")
        for n in names:
            self.cmb_views.addItem(n)

    def _state_snapshot(self):
        assign = [p.assigned_channel for p in self.panes]
        return {"panes": self.visible_panes, "active_pane": self.active_pane, "assign": assign[:16]}

    def _restore_state(self):
        st = safe_read_json(self.cfg.state_file, None)
        if not isinstance(st, dict):
            return
        panes = st.get("panes")
        active = st.get("active_pane")
        assign = st.get("assign")
        if isinstance(panes, int):
            self.visible_panes = max(1, min(16, panes))
        if isinstance(active, int):
            self.active_pane = max(1, min(16, active))
        if isinstance(assign, list):
            for i in range(min(16, len(assign))):
                ch = assign[i]
                if isinstance(ch, int) and 1 <= ch <= 16 and self.cfg.is_channel_active(ch):
                    self.panes[i].assigned_channel = ch
                    self.panes[i].label.setText(f"Pane {i+1}: {self.cfg.channel_text(ch)} (saved)")
                else:
                    self.panes[i].assigned_channel = None
                    self.panes[i].label.setText(f"Pane {i+1}: Idle")

    def _save_state(self):
        safe_write_json(self.cfg.state_file, self._state_snapshot())

    def _update_focus(self):
        for p in self.panes:
            p.set_focused(p.pane_id == self.active_pane and p.isVisible())
        self.info.setText(f"Active pane: {self.active_pane}")

    def set_active_pane(self, pane_id: int):
        if pane_id > self.visible_panes:
            pane_id = 1
        self.active_pane = pane_id
        self._update_focus()

    @staticmethod
    def _grid_dims(n: int):
        """Pick reasonable (rows, cols) for n tiles.  Same heuristic as the
        virtual variant: 2-row layouts for small even counts (wider tiles),
        sqrt-based for everything else."""
        n = max(1, min(16, int(n)))
        if n % 2 == 0 and n <= 8:
            return 2, n // 2
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))
        return rows, cols

    def _clear_pane_layout(self):
        while self.panes_grid.count():
            item = self.panes_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

    def _rebuild_grid(self):
        """Re-populate self.panes_grid for self.visible_panes tiles, computing
        rows/cols dynamically.  Reparenting a QFrame invalidates its native
        window handle, so for every pane that *currently* has a player we:
          1. detach the live player from the (about-to-die) winId,
          2. re-add the pane at its new (r, c),
          3. force Qt to allocate a fresh native window handle,
          4. rebind the player to that handle.
        Players keep playing throughout — no reconnect, no media reload."""
        n = self.visible_panes

        # Detach EVERY pane that has a player, not just the soon-to-be-visible
        # ones: stop_to_idle leaves a fresh idle player behind, and even those
        # are bound to the about-to-die frame winId.
        for p in self.panes:
            p._detach_player_window()

        self._clear_pane_layout()

        rows, cols = self._grid_dims(n) if n > 0 else (0, 0)
        self.grid_rows, self.grid_cols = rows, cols

        for i in range(16):
            self.panes[i].setVisible(i < n)

        for i in range(n):
            r = i // cols
            c = i % cols
            self.panes_grid.addWidget(self.panes[i], r, c)

        # Reset stretches and apply only to the active rows/cols.
        for k in range(16):
            self.panes_grid.setRowStretch(k, 0)
            self.panes_grid.setColumnStretch(k, 0)
        for r in range(rows):
            self.panes_grid.setRowStretch(r, 1)
        for c in range(cols):
            self.panes_grid.setColumnStretch(c, 1)

        # Force Qt to allocate fresh native window handles before we rebind.
        for i in range(n):
            _ = int(self.panes[i].frame.winId())
        QApplication.processEvents()

        for i in range(n):
            self.panes[i].rebind_to_current_frame()

    def _apply_panes_visibility(self, n: int):
        n = max(1, min(16, int(n)))
        prev = self.visible_panes
        self.visible_panes = n

        # Stop streams that just disappeared (saves bandwidth + decode CPU).
        for i in range(n, prev):
            self.panes[i].stop_to_idle()

        self._rebuild_grid()

        # (Re)start saved channels for panes that just became visible — the
        # previous behaviour silently left them stuck in "(saved)".
        for i in range(prev, n):
            ch = self.panes[i].assigned_channel
            if isinstance(ch, int) and self.cfg.is_channel_active(ch):
                self.panes[i].play_channel(ch)

        if self.active_pane > n:
            self.active_pane = 1
        self._update_focus()

    def _on_panes_changed(self, txt: str):
        try:
            n = int(txt)
        except Exception:
            return
        self._apply_panes_visibility(n)

    def on_channel_pressed(self, ch: int):
        self.panes[self.active_pane - 1].play_channel(ch)

    def clear_active_pane(self):
        self.panes[self.active_pane - 1].stop_to_idle()

    def apply_selected_view(self):
        name = self.cmb_views.currentText().strip()
        if not name:
            return
        v = self.views.get(name)
        if not isinstance(v, dict):
            return
        panes = int(v.get("panes", 4))
        assign = v.get("assign", [])
        self.cmb_panes.setCurrentText(str(panes))
        self._apply_panes_visibility(panes)
        for i in range(1, 17):
            p = self.panes[i - 1]
            if i > self.visible_panes:
                continue
            ch = assign[i - 1] if i - 1 < len(assign) else None
            if isinstance(ch, int) and 1 <= ch <= 16:
                p.play_channel(ch)
            else:
                p.stop_to_idle()

    def save_view_dialog(self):
        name, ok = QInputDialog.getText(self, "Save View", "View name:")
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            return
        panes = self.visible_panes
        assign = []
        for i in range(1, 17):
            p = self.panes[i - 1]
            if i <= panes:
                assign.append(p.assigned_channel)
            else:
                assign.append(None)
        self.views.save(name, panes, assign)
        self._reload_views_combo()
        self.cmb_views.setCurrentText(name)

    def delete_selected_view(self):
        name = self.cmb_views.currentText().strip()
        if not name:
            return
        r = QMessageBox.question(self, "Delete View", f"Delete view '{name}'?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        self.views.delete(name)
        self._reload_views_combo()

    def show_about(self):
        dlg = AboutDialog(self, self.cfg, self.visible_panes)
        dlg.exec_()

    def cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        self._save_state()
        for p in self.panes:
            p.shutdown()
        # Wait for in-flight stop()/release() workers before tearing down the
        # VLC instance.  Otherwise libVLC frees state out from under them.
        join_disposal_threads(timeout_total=3.0)
        try:
            self.vlc.release()
        except Exception:
            pass

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)


def install_sigint_handler(window: MainWindow):
    def _sigint(_signum, _frame):
        QTimer.singleShot(0, window.close)
    signal.signal(signal.SIGINT, _sigint)
    tick = QTimer()
    tick.setInterval(200)
    tick.timeout.connect(lambda: None)
    tick.start()
    return tick


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    app.aboutToQuit.connect(w.cleanup)
    _tick = install_sigint_handler(w)
    w.show()
    sys.exit(app.exec_())