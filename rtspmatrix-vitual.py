# rtspmatrix_virtual.py
#
# Core guarantees:
# - scroll by 1: [1,2,3,4] -> [2,3,4,5] : ONLY channel 5 is a new RTSP session
# - buffer=N keeps N channels on the left and N on the right connected (playing) on hidden sinks
# - fullscreen is seamless: same already-playing MediaPlayer, only drawable rebind (no reconnect)
# - fullscreen Left/Right switches to prev/next channel fast (uses the same buffered pool)
#
# Logo:
# - put icon at: assets/rtspmatrix_icon.png
# - if missing and the Artlist composite exists in cwd, the app auto-extracts it.

import sys
import os
import json
import math
import signal
import threading
import datetime
import platform
import configparser
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import vlc
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QGuiApplication, QIcon, QPixmap, QImage
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QButtonGroup, QLabel, QComboBox,
    QDialog, QTextEdit, QInputDialog, QMessageBox,
    QSizePolicy, QAction, QMenu, QCheckBox, QScrollArea
)

APP_NAME = "RTSPMatrix-Virtual"

ICON_PATH = os.path.join("assets", "rtspmatrix_icon.png")
ARTLIST_COMPOSITE_HINT = "Artlist_prompt_logo_design_RTS_Nano_Banana_Pro_48527.jpeg"
# Crop of the small lockup icon (verified for the provided 1536x1536 composite):
ARTLIST_CROP_BOX = (805, 245, 1030, 470)  # left, top, right, bottom


# ---------------- utils ----------------

def clamp_int(v, lo, hi, default):
    try:
        v = int(v)
    except Exception:
        return default
    return max(lo, min(hi, v))


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


def dispose_player_async(p: vlc.MediaPlayer):
    # Detach from any OS window handle synchronously (on the caller/GUI thread)
    # BEFORE the associated sink widget may be deleted.  Without this, libVLC
    # can still try to render into a dead HWND/XWindow from its internal thread,
    # causing a use-after-free crash.
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
    threading.Thread(target=_w, daemon=True).start()


def bind_player(player: vlc.MediaPlayer, winid: int):
    wid = int(winid)
    if sys.platform.startswith("linux"):
        player.set_xwindow(wid)
    elif sys.platform.startswith("win"):
        player.set_hwnd(wid)
    elif sys.platform.startswith("darwin"):
        player.set_nsobject(wid)
    else:
        player.set_xwindow(wid)


def ensure_icon_assets():
    if os.path.exists(ICON_PATH):
        return

    os.makedirs(os.path.dirname(ICON_PATH), exist_ok=True)

    src = None
    if os.path.exists(ARTLIST_COMPOSITE_HINT):
        src = ARTLIST_COMPOSITE_HINT
    else:
        # fallback: try any file matching prefix in cwd
        for fn in os.listdir("."):
            if fn.startswith("Artlist_prompt_logo_design") and fn.lower().endswith((".jpg", ".jpeg", ".png")):
                src = fn
                break
    if not src:
        return

    img = QImage(src)
    if img.isNull():
        return

    l, t, r, b = ARTLIST_CROP_BOX
    if r <= l or b <= t:
        return

    cropped = img.copy(l, t, r - l, b - t)
    if cropped.isNull():
        return

    # upscale to 1024 with smooth scaling
    pm = QPixmap.fromImage(cropped).scaled(1024, 1024, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.save(ICON_PATH, "PNG")


# ---------------- config ----------------

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
        self.rtsp_timeout_s = s.getint("rtsp_timeout_s", 4)

        a = cp["app"] if cp.has_section("app") else {}
        self.title = a.get("title", APP_NAME)
        self.default_panes = int(a.get("default_panes", 4))
        self.views_file = a.get("views_file", "views.json")
        self.state_file = a.get("state_file", "state.json")

    def url(self, channel: int) -> str:
        ch = clamp_int(channel, 1, 16, 1)
        return f"rtsp://{self.host}:{self.port}{self.path}?channel={ch}&subtype={self.subtype}"


# ---------------- views ----------------

class ViewsStore:
    def __init__(self, path: str):
        self.path = path
        self.data = safe_read_json(self.path, {"views": {}})
        if not isinstance(self.data, dict) or "views" not in self.data or not isinstance(self.data["views"], dict):
            self.data = {"views": {}}

    def list_names(self):
        return sorted(self.data["views"].keys(), key=str.lower)

    def get(self, name: str):
        return self.data["views"].get(name)

    def save_view(self, name: str, view_obj: dict):
        self.data["views"][name] = view_obj
        safe_write_json(self.path, self.data)

    def delete(self, name: str):
        if name in self.data["views"]:
            del self.data["views"][name]
            safe_write_json(self.path, self.data)


# ---------------- UI primitives ----------------

class ClickableFrame(QFrame):
    clicked = pyqtSignal()
    doubleClicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class HiddenSink(QFrame):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        # WA_NativeWindow forces creation of a real OS-level window handle
        # without requiring the widget to be shown.  WA_DontShowOnScreen is
        # unreliable on macOS and some Wayland compositors and can return 0.
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFixedSize(1, 1)
        self.hide()
        _ = int(self.winId())  # confirm the handle is allocated now


class Tile(QWidget):
    def __init__(self, idx: int, on_click, on_dblclick):
        super().__init__()
        self.idx = idx
        self.on_click = on_click
        self.on_dblclick = on_dblclick

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.frame = ClickableFrame(self)
        self.frame.setFrameShape(QFrame.Box)
        self.frame.setStyleSheet("background: black; border: 3px solid #333;")
        self.frame.setMinimumSize(160, 120)
        self.frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Each tile needs its own native OS window handle so VLC can render
        # into it independently.  Without this, winId() returns the toplevel
        # window's handle and all players render on top of each other.
        self.frame.setAttribute(Qt.WA_NativeWindow, True)
        self.frame.clicked.connect(lambda: self.on_click(self))
        self.frame.doubleClicked.connect(lambda: self.on_dblclick(self))

        self.label = QLabel("Idle", self)
        self.label.setStyleSheet("color: #ddd;")
        self.label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addWidget(self.frame, 1)
        v.addWidget(self.label, 0)

        self.channel: Optional[int] = None

    def set_focused(self, focused: bool):
        if focused:
            self.frame.setStyleSheet("background: black; border: 3px solid #66aaff;")
        else:
            self.frame.setStyleSheet("background: black; border: 3px solid #333;")


class ChannelListDialog(QDialog):
    """Dialog with a checklist of all 16 channels.
    Checked = active (included), unchecked = excluded.
    """
    def __init__(self, parent, excluded: set):
        super().__init__(parent)
        self.setWindowTitle("Channel list")
        self.setMinimumWidth(220)

        self._checks: Dict[int, QCheckBox] = {}

        scroll_widget = QWidget()
        grid = QGridLayout(scroll_widget)
        grid.setSpacing(6)
        for i, ch in enumerate(range(1, 17)):
            cb = QCheckBox(f"CH {ch}", scroll_widget)
            cb.setChecked(ch not in excluded)
            self._checks[ch] = cb
            grid.addWidget(cb, i // 2, i % 2)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(scroll_widget)
        scroll.setFrameShape(QFrame.NoFrame)

        btn_ok = QPushButton("Apply", self)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel", self)
        btn_cancel.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)

        v = QVBoxLayout(self)
        v.addWidget(scroll, 1)
        v.addLayout(btn_row)

    def excluded_set(self) -> set:
        """Return the set of channels the user wants excluded."""
        return {ch for ch, cb in self._checks.items() if not cb.isChecked()}


class AboutDialog(QDialog):
    def __init__(self, parent, cfg: RtspConfig, panes: int, rows: int, cols: int,
                 active_count: int, start: int, visible: List[int], active_list: List[int]):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.resize(860, 620)

        text = QTextEdit(self)
        text.setReadOnly(True)

        py_ver = sys.version.replace("\n", " ")
        try:
            pv_vlc = __import__("importlib").metadata.version("python-vlc")
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
        info.append(f"App: {APP_NAME}")
        info.append(f"OS: {platform.platform()}")
        info.append(f"Python: {py_ver}")
        info.append(f"python-vlc: {pv_vlc}")
        info.append(f"libVLC: {libvlc_ver}")
        info.append("")
        info.append(f"RTSP host: {cfg.host}:{cfg.port}")
        info.append(f"RTSP path: {cfg.path}")
        info.append(f"TCP: {cfg.tcp}")
        info.append(f"network_caching_ms: {cfg.network_caching_ms}")
        info.append(f"rtsp_timeout_s: {cfg.rtsp_timeout_s}")
        info.append("")
        info.append(f"Displayed tiles: {panes} (grid {rows}x{cols})")
        info.append(f"Active channels count: {active_count}")
        info.append(f"Window start index: {start}")
        info.append(f"Visible channels: {visible}")
        info.append(f"Active list: {active_list}")
        info.append("")
        info.append("Files:")
        info.append("  rtsp.ini")
        info.append(f"  {cfg.views_file}")
        info.append(f"  {cfg.state_file}")

        content = "\n".join(info)
        text.setPlainText(content)

        btn_copy = QPushButton("Copy", self)
        btn_close = QPushButton("Close", self)
        btn_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(content))
        btn_close.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn_copy)
        row.addWidget(btn_close)

        v = QVBoxLayout(self)
        v.addWidget(text, 1)
        v.addLayout(row)


# ---------------- buffering core ----------------

@dataclass
class ChanPlayer:
    ch: int
    player: vlc.MediaPlayer
    sink: HiddenSink
    bound_to: int


class PlayerPool:
    """
    Stable mapping:
      channel -> MediaPlayer
    Only channels within (window + left/right buffer) exist.
    Moving the window by 1 means:
      - 1 channel leaves desired set -> player released
      - 1 channel enters desired set -> player created and connected
      - kept channels never reconnect
    """
    def __init__(self, vlc_instance: vlc.Instance, cfg: RtspConfig, parent_widget: QWidget):
        self.vlc = vlc_instance
        self.cfg = cfg
        self.parent = parent_widget
        self.by_channel: Dict[int, ChanPlayer] = {}

    def _make_media(self, ch: int) -> vlc.Media:
        m = self.vlc.media_new(self.cfg.url(ch))
        if self.cfg.user:
            m.add_option(f":rtsp-user={self.cfg.user}")
        if self.cfg.password:
            m.add_option(f":rtsp-pwd={self.cfg.password}")
        if self.cfg.tcp:
            m.add_option(":rtsp-tcp")
        m.add_option(f":network-caching={self.cfg.network_caching_ms}")
        # hard timeout helps with dead channels; does not block UI
        m.add_option(f":rtsp-timeout={self.cfg.rtsp_timeout_s}")
        return m

    def ensure(self, desired_channels: List[int]):
        desired = [int(x) for x in desired_channels if isinstance(x, int)]
        desired_set = set(desired)

        # remove undesired
        for ch in list(self.by_channel.keys()):
            if ch not in desired_set:
                cp = self.by_channel.pop(ch)
                dispose_player_async(cp.player)
                try:
                    cp.sink.setParent(None)
                    cp.sink.deleteLater()
                except Exception:
                    pass

        # add missing (start playing immediately on hidden sink)
        for ch in desired:
            if ch in self.by_channel:
                continue
            p = self.vlc.media_player_new()
            sink = HiddenSink(self.parent)
            bind_player(p, int(sink.winId()))
            m = self._make_media(ch)
            p.set_media(m)
            try:
                p.play()
            except Exception:
                pass
            self.by_channel[ch] = ChanPlayer(ch=ch, player=p, sink=sink, bound_to=int(sink.winId()))

    def get(self, ch: int) -> Optional[vlc.MediaPlayer]:
        cp = self.by_channel.get(int(ch))
        return cp.player if cp else None

    def bind_to(self, ch: int, winid: int):
        cp = self.by_channel.get(int(ch))
        if not cp:
            return
        wid = int(winid)
        if cp.bound_to == wid:
            return
        try:
            bind_player(cp.player, wid)
            cp.bound_to = wid
        except Exception:
            pass

    def bind_hidden(self, ch: int):
        cp = self.by_channel.get(int(ch))
        if not cp:
            return
        wid = int(cp.sink.winId())
        if cp.bound_to == wid:
            return
        try:
            bind_player(cp.player, wid)
            cp.bound_to = wid
        except Exception:
            pass

    def shutdown(self):
        for ch in list(self.by_channel.keys()):
            cp = self.by_channel.pop(ch)
            dispose_player_async(cp.player)
            try:
                cp.sink.setParent(None)
                cp.sink.deleteLater()
            except Exception:
                pass


# ---------------- fullscreen ----------------

class FullScreenWindow(QMainWindow):
    closed = pyqtSignal()

    def __init__(self, owner: "MainWindow"):
        super().__init__()
        self.owner = owner
        self.setWindowTitle(APP_NAME)

        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))

        root = QWidget(self)
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.video = ClickableFrame(self)
        self.video.setStyleSheet("background: black;")
        self.video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # WA_NativeWindow gives this child widget its own OS-level window handle
        # so VLC binds to the video area specifically, not to the whole QMainWindow.
        self.video.setAttribute(Qt.WA_NativeWindow, True)
        self.video.clicked.connect(lambda: QTimer.singleShot(0, self.close))
        v.addWidget(self.video, 1)

        self.current_channel: Optional[int] = None

    def show_channel(self, ch: int):
        self.current_channel = int(ch)
        self.setWindowTitle(f"{APP_NAME} - CH{self.current_channel}")
        # Defer the VLC bind by one tick so the window is fully mapped before
        # we hand the drawable to libVLC.  The guard in _bind_channel_to_fullscreen
        # (self.fullscreen is None → return) prevents this timer from stealing
        # the player back if the window is closed before the tick fires.
        ch_snap = self.current_channel
        wid_snap = int(self.video.winId())
        QTimer.singleShot(0, lambda: self.owner._bind_channel_to_fullscreen(ch_snap, wid_snap))

    def keyPressEvent(self, event):
        k = event.key()
        if k == Qt.Key_Escape:
            self.close()
            return
        if k == Qt.Key_Left:
            self.owner.fullscreen_step(-1)
            return
        if k == Qt.Key_Right:
            self.owner.fullscreen_step(+1)
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self.owner._fullscreen_closed()
        try:
            self.owner.showNormal()
            self.owner.raise_()
            self.owner.activateWindow()
        except Exception:
            pass
        self.closed.emit()
        super().closeEvent(event)


# ---------------- main window ----------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._cleaned = False

        ensure_icon_assets()
        self.cfg = RtspConfig("rtsp.ini")
        self.views = ViewsStore(self.cfg.views_file)

        vlc_args = ["--no-video-title-show"]
        if self.cfg.tcp:
            vlc_args.append("--rtsp-tcp")
        vlc_args.append(f"--network-caching={self.cfg.network_caching_ms}")
        self.vlc = vlc.Instance(vlc_args)

        self.pool = PlayerPool(self.vlc, self.cfg, self)

        # state
        self.visible_panes = clamp_int(self.cfg.default_panes, 1, 16, 4)
        self.grid_rows, self.grid_cols = self._grid_dims(self.visible_panes)

        self.active_channels = 16
        self.active_list: List[int] = list(range(1, 17))

        self.window_start = 0                 # shift-by-1 window start index
        self.focus_idx = 0

        # fullscreen lock: while fullscreen open, this channel is not allowed to be rebound to tiles/sinks
        self.fullscreen: Optional[FullScreenWindow] = None
        self.fullscreen_channel: Optional[int] = None
        self.fullscreen_return_tile_winid: Optional[int] = None

        # channels excluded from the active rotation; right-click a button to toggle
        self.excluded: set = set()

        # ui
        self.setWindowTitle(APP_NAME)
        self.resize(1600, 1000)

        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))

        self._init_menu()

        root = QWidget(self)
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(8)

        # channel buttons
        self.btn_channels = QButtonGroup(self)
        self.btn_channels.setExclusive(False)
        btn_grid = QGridLayout()
        btn_grid.setSpacing(6)
        for i, ch in enumerate(range(1, 17)):
            b = QPushButton(str(ch), self)
            b.setMinimumHeight(34)
            b.setMinimumWidth(48)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            # Right-click → exclude / include context menu
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(
                lambda _pos, c=ch: self._on_channel_btn_context(c))
            self.btn_channels.addButton(b, ch)
            btn_grid.addWidget(b, i // 8, i % 8)
        main.addLayout(btn_grid)

        # controls
        controls = QHBoxLayout()
        controls.setSpacing(10)

        controls.addWidget(QLabel("Streams:", self))
        self.cmb_panes = QComboBox(self)
        self.cmb_panes.addItems([str(i) for i in range(1, 17)])
        controls.addWidget(self.cmb_panes)

        controls.addWidget(QLabel("Active:", self))
        self.cmb_active = QComboBox(self)
        self.cmb_active.addItems([str(i) for i in range(1, 17)])
        controls.addWidget(self.cmb_active)

        self.btn_left = QPushButton("◀", self)
        self.btn_right = QPushButton("▶", self)
        self.btn_left.setFixedWidth(44)
        self.btn_right.setFixedWidth(44)
        controls.addWidget(self.btn_left)
        controls.addWidget(self.btn_right)

        self.btn_channels_list = QPushButton("Channels…", self)
        self.btn_channels_list.setToolTip("Show/hide channels (exclude from rotation)")
        controls.addWidget(self.btn_channels_list)

        controls.addWidget(QLabel("View:", self))
        self.cmb_views = QComboBox(self)
        controls.addWidget(self.cmb_views)

        self.btn_apply_view = QPushButton("Apply", self)
        self.btn_save_view = QPushButton("Save", self)
        self.btn_delete_view = QPushButton("Delete", self)
        controls.addWidget(self.btn_apply_view)
        controls.addWidget(self.btn_save_view)
        controls.addWidget(self.btn_delete_view)

        controls.addStretch(1)
        main.addLayout(controls)

        self.info = QLabel("", self)
        self.info.setStyleSheet("color: #ddd;")
        main.addWidget(self.info)

        self.panes_grid = QGridLayout()
        self.panes_grid.setSpacing(8)
        main.addLayout(self.panes_grid, 1)

        # tiles
        self.tiles: List[Tile] = [Tile(i + 1, self._on_tile_click, self._on_tile_dblclick) for i in range(16)]

        # restore
        self._reload_views_combo()
        self._restore_state()

        # wire
        # buttonClicked[int] is deprecated in Qt5 and removed in Qt6; use idClicked.
        self.btn_channels.idClicked.connect(self.on_channel_pressed)
        self.cmb_panes.currentTextChanged.connect(self._on_panes_changed)
        self.cmb_active.currentTextChanged.connect(self._on_active_changed)
        self.btn_left.clicked.connect(lambda: self.scroll_by(-1))
        self.btn_right.clicked.connect(lambda: self.scroll_by(+1))
        self.btn_apply_view.clicked.connect(self.apply_selected_view)
        self.btn_save_view.clicked.connect(self.save_view_dialog)
        self.btn_delete_view.clicked.connect(self.delete_selected_view)
        self.btn_channels_list.clicked.connect(self.open_channel_list_dialog)

        # Set initial UI values without firing signals (which would trigger
        # _rebuild_grid / _apply_window prematurely and start RTSP connections
        # before the window is shown).  The explicit calls below handle init.
        for widget, value in [
            (self.cmb_panes,   str(self.visible_panes)),
            (self.cmb_active,  str(self.active_channels)),
        ]:
            widget.blockSignals(True)
            widget.setCurrentText(value)
            widget.blockSignals(False)

        self._rebuild_grid()
        self._clamp_active_list_to_count()
        self._clamp_window_start()
        self._apply_window()
        self._update_focus()
        self._update_scroll_buttons()
        self._refresh_channel_buttons()

        # label refresh (cheap), no per-channel timers
        self.state_timer = QTimer(self)
        self.state_timer.setInterval(400)
        self.state_timer.timeout.connect(self._refresh_labels)
        self.state_timer.start()

    # ---------- menu ----------

    def _init_menu(self):
        help_menu = self.menuBar().addMenu("&Help")
        act_about = QAction(f"About {APP_NAME}", self)
        act_about.triggered.connect(self.show_about)
        help_menu.addAction(act_about)

    def show_about(self):
        dlg = AboutDialog(
            self, self.cfg,
            self.visible_panes, self.grid_rows, self.grid_cols,
            self.active_channels, self.window_start,
            self._visible_channels(), self.active_list[:]
        )
        dlg.exec_()

    # ---------- grid ----------

    def _grid_dims(self, n: int) -> Tuple[int, int]:
        n = clamp_int(n, 1, 16, 4)
        # 2-row layout is reasonable for small counts (gives wide tiles),
        # but for n >= 10 it becomes impractically wide (e.g. 2x8 for 16).
        # Use sqrt-based layout for larger counts instead.
        if n % 2 == 0 and n <= 8:
            return 2, n // 2
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))
        return rows, cols

    def _clear_layout(self, layout: QGridLayout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

    def _reset_grid_stretch(self):
        for i in range(0, 16):
            self.panes_grid.setRowStretch(i, 0)
            self.panes_grid.setColumnStretch(i, 0)

    def _apply_grid_stretch(self, rows: int, cols: int):
        self._reset_grid_stretch()
        for r in range(rows):
            self.panes_grid.setRowStretch(r, 1)
        for c in range(cols):
            self.panes_grid.setColumnStretch(c, 1)

    def _rebuild_grid(self):
        self.grid_rows, self.grid_cols = self._grid_dims(self.visible_panes)
        self._clear_layout(self.panes_grid)

        for i in range(16):
            t = self.tiles[i]
            if i < self.visible_panes:
                t.setVisible(True)
                r = i // self.grid_cols
                c = i % self.grid_cols
                self.panes_grid.addWidget(t, r, c)
            else:
                t.setVisible(False)

        self._apply_grid_stretch(self.grid_rows, self.grid_cols)

        if self.focus_idx >= self.visible_panes:
            self.focus_idx = 0

    # ---------- window model (shift-by-1) ----------

    def _clamp_active_list_to_count(self):
        self.active_channels = clamp_int(self.active_channels, 1, 16, 16)

        tmp = []
        seen = set()
        for x in self.active_list:
            if isinstance(x, int) and 1 <= x <= 16 and x not in seen:
                tmp.append(x)
                seen.add(x)
        self.active_list = tmp

        if len(self.active_list) > self.active_channels:
            self.active_list = self.active_list[:self.active_channels]
        elif len(self.active_list) < self.active_channels:
            # Never pad with excluded channels
            existing = set(self.active_list)
            missing = [c for c in range(1, 17)
                       if c not in existing and c not in self.excluded]
            self.active_list.extend(missing[: (self.active_channels - len(self.active_list))])

    def _max_start(self) -> int:
        return max(0, len(self.active_list) - self.visible_panes)

    def _clamp_window_start(self):
        self.window_start = clamp_int(self.window_start, 0, self._max_start(), 0)

    def _visible_channels(self) -> List[int]:
        out = []
        for i in range(self.visible_panes):
            idx = self.window_start + i
            if 0 <= idx < len(self.active_list):
                out.append(self.active_list[idx])
        return out

    def _desired_channels(self) -> List[int]:
        """Return the complete set of channels that must stay connected.

        All active channels are kept connected at all times — switching the
        visible window is then just a drawable rebind (instant), not a new
        RTSP session.  The old sliding-window + buffer approach required the
        buffer to be large enough that new channels had already connected,
        which was unreliable on slower networks.
        """
        return list(self.active_list)

    def _apply_window(self):
        self._clamp_active_list_to_count()
        self._clamp_window_start()

        # _desired_channels() already includes the fullscreen neighbourhood.
        desired = self._desired_channels()
        visible = self._visible_channels()

        self.pool.ensure(desired)

        # bind visible channels to tiles (rebind only; no reconnect)
        for i in range(self.visible_panes):
            t = self.tiles[i]
            idx = self.window_start + i
            if 0 <= idx < len(self.active_list):
                ch = self.active_list[idx]
                t.channel = ch

                # fullscreen lock: do not steal drawable for the fullscreen channel
                if self.fullscreen_channel is not None and ch == self.fullscreen_channel:
                    t.label.setText(f"CH{ch} (FS)")
                else:
                    self.pool.bind_to(ch, int(t.frame.winId()))
                    t.label.setText(f"CH{ch}")
            else:
                t.channel = None
                t.label.setText("Idle")

        # bind buffered but not visible to hidden sinks (keep sessions warm)
        for ch in desired:
            if ch in visible:
                continue
            if self.fullscreen_channel is not None and ch == self.fullscreen_channel:
                continue
            self.pool.bind_hidden(ch)

    # ---------- labels (state sampling) ----------

    def _refresh_labels(self):
        # visible tiles only; no scanning of whole pool
        for i in range(self.visible_panes):
            t = self.tiles[i]
            ch = t.channel
            if not isinstance(ch, int):
                continue
            if self.fullscreen_channel is not None and ch == self.fullscreen_channel:
                continue
            p = self.pool.get(ch)
            if not p:
                continue
            try:
                st = p.get_state()
            except Exception:
                st = vlc.State.Error

            if st == vlc.State.Playing:
                t.label.setText(f"CH{ch} playing")
            elif st in (vlc.State.Opening, vlc.State.Buffering):
                t.label.setText(f"CH{ch} opening")
            elif st == vlc.State.Error:
                t.label.setText(f"CH{ch} ERR")
            else:
                # keep short and stable
                t.label.setText(f"CH{ch}")

    # ---------- focus / scrolling ----------

    def _on_tile_click(self, tile: Tile):
        idx = self._tile_index(tile)
        if idx is None:
            return
        self.focus_idx = idx
        self._update_focus()
        # Defer by one event-loop tick so the mousePressEvent stack unwinds
        # before showFullScreen() is called.  Calling showFullScreen() directly
        # from inside mousePressEvent confuses Qt's event delivery on most
        # platforms and can prevent the window from appearing or cause the
        # fullscreen video to receive the same click and immediately close.
        QTimer.singleShot(0, lambda t=tile: self.open_fullscreen_from_tile(t))

    def _on_tile_dblclick(self, tile: Tile):
        # Double-click is intentionally the same as single-click so that fast
        # double-clicks don't close the fullscreen immediately after opening it.
        pass

    def _tile_index(self, tile: Tile) -> Optional[int]:
        for i in range(self.visible_panes):
            if self.tiles[i] is tile:
                return i
        return None

    def scroll_by(self, delta: int):
        self.window_start += int(delta)
        self._clamp_window_start()
        self._apply_window()
        self._update_focus()
        self._update_scroll_buttons()

    def keyPressEvent(self, event):
        if self.fullscreen is None:
            if event.key() == Qt.Key_Left:
                self.scroll_by(-1)
                return
            if event.key() == Qt.Key_Right:
                self.scroll_by(+1)
                return
        super().keyPressEvent(event)

    def _update_focus(self):
        for i in range(self.visible_panes):
            self.tiles[i].set_focused(i == self.focus_idx)

        vis = self._visible_channels()
        self.info.setText(f"Focus: {self.focus_idx + 1} | Start: {self.window_start} | Visible: {vis}")

    def _update_scroll_buttons(self):
        enable = len(self.active_list) > self.visible_panes
        self.btn_left.setEnabled(enable)
        self.btn_right.setEnabled(enable)

    # ---------- channel exclusion ----------

    def open_channel_list_dialog(self):
        """Open the channel checklist dialog and apply the result."""
        dlg = ChannelListDialog(self, self.excluded)
        if dlg.exec_() != QDialog.Accepted:
            return
        new_excluded = dlg.excluded_set()
        # Apply differences so toggle_exclude handles pool + UI updates
        to_exclude = new_excluded - self.excluded
        to_include = self.excluded - new_excluded
        for ch in to_exclude:
            self.toggle_exclude(ch)
        for ch in to_include:
            self.toggle_exclude(ch)

    def _on_channel_btn_context(self, ch: int):
        """Right-click context menu on a channel number button."""
        menu = QMenu(self)
        if ch in self.excluded:
            act = menu.addAction(f"Include CH{ch}")
            act.triggered.connect(lambda: self.toggle_exclude(ch))
        else:
            act = menu.addAction(f"Exclude CH{ch}")
            act.triggered.connect(lambda: self.toggle_exclude(ch))
        btn = self.btn_channels.button(ch)
        if btn:
            menu.exec_(btn.mapToGlobal(btn.rect().bottomLeft()))

    def toggle_exclude(self, ch: int):
        """Add or remove ch from the excluded set and rebuild active_list."""
        if ch in self.excluded:
            self.excluded.discard(ch)
            # Re-insert the channel at the end of the active list if absent
            if ch not in self.active_list:
                self.active_list.append(ch)
                self.active_channels = len(self.active_list)
        else:
            self.excluded.add(ch)
            # Remove from active_list immediately; pool will release the player
            if ch in self.active_list:
                self.active_list.remove(ch)
                self.active_channels = len(self.active_list)

        self._refresh_channel_buttons()
        self.cmb_active.blockSignals(True)
        self.cmb_active.setCurrentText(str(self.active_channels))
        self.cmb_active.blockSignals(False)
        self._clamp_window_start()
        self._apply_window()
        self._update_focus()
        self._update_scroll_buttons()

    def _refresh_channel_buttons(self):
        """Style channel buttons to show which channels are excluded."""
        for ch in range(1, 17):
            btn = self.btn_channels.button(ch)
            if btn is None:
                continue
            if ch in self.excluded:
                btn.setStyleSheet(
                    "color: #666; text-decoration: line-through; background: #1a1a1a;")
                btn.setToolTip(f"CH{ch} excluded (right-click to include)")
            else:
                btn.setStyleSheet("")
                btn.setToolTip("")

    # ---------- channel button jump ----------

    def on_channel_pressed(self, ch: int):
        ch = clamp_int(ch, 1, 16, 1)

        # ensure channel exists in active_list
        if ch not in self.active_list:
            if len(self.active_list) < 16:
                self.active_list.append(ch)
                self.active_channels = len(self.active_list)
                self.cmb_active.setCurrentText(str(self.active_channels))
            else:
                self.active_list[-1] = ch

        idx = self.active_list.index(ch)
        desired_start = idx - self.focus_idx
        self.window_start = clamp_int(desired_start, 0, self._max_start(), 0)
        self._apply_window()
        self._update_focus()
        self._update_scroll_buttons()

    # ---------- fullscreen ----------

    def open_fullscreen_from_tile(self, tile: Tile):
        if self.fullscreen is not None:
            return
        if not isinstance(tile.channel, int):
            return
        ch = tile.channel

        # ensure this channel is in pool and already playing
        self.pool.ensure(list(set(self._desired_channels() + [ch])))

        self.fullscreen = FullScreenWindow(self)
        self.fullscreen_channel = ch
        self.fullscreen_return_tile_winid = int(tile.frame.winId())

        # bind player to fullscreen drawable
        self.fullscreen.showFullScreen()
        self.fullscreen.show_channel(ch)

        # remove tile binding for the fullscreen channel (tile goes black + label changes)
        self._apply_window()

    def _bind_channel_to_fullscreen(self, ch: int, fullscreen_winid: int):
        # Guard: if fullscreen was already closed don't steal the player
        # back from the tile it was returned to by _fullscreen_closed().
        if self.fullscreen is None:
            return
        # keep fullscreen channel in pool
        desired = self._desired_channels()
        if ch not in desired:
            desired.append(ch)
        self.pool.ensure(desired)

        p = self.pool.get(ch)
        if not p:
            return
        self.pool.bind_to(ch, fullscreen_winid)

    def fullscreen_step(self, delta: int):
        if self.fullscreen is None or self.fullscreen_channel is None:
            return
        if self.fullscreen_channel not in self.active_list:
            return

        idx = self.active_list.index(self.fullscreen_channel)
        nidx = idx + int(delta)
        if nidx < 0 or nidx >= len(self.active_list):
            return

        new_ch = self.active_list[nidx]
        self.fullscreen_channel = new_ch

        # keep neighborhood hot, then bind to fullscreen
        self._apply_window()
        self.fullscreen.show_channel(new_ch)

    def _fullscreen_closed(self):
        # on close: release fullscreen lock and restore tile bindings
        self.fullscreen = None
        self.fullscreen_channel = None
        self.fullscreen_return_tile_winid = None
        self._apply_window()
        self._update_focus()

    # ---------- controls ----------

    def _on_panes_changed(self, txt: str):
        self.visible_panes = clamp_int(txt, 1, 16, self.visible_panes)
        self._rebuild_grid()
        self._clamp_window_start()
        self._apply_window()
        self._update_focus()
        self._update_scroll_buttons()

    def _on_active_changed(self, txt: str):
        self.active_channels = clamp_int(txt, 1, 16, self.active_channels)
        self._clamp_active_list_to_count()
        self._clamp_window_start()
        self._apply_window()
        self._update_focus()
        self._update_scroll_buttons()

    # ---------- views ----------

    def _reload_views_combo(self):
        names = self.views.list_names()
        self.cmb_views.clear()
        self.cmb_views.addItem("")
        for n in names:
            self.cmb_views.addItem(n)

    def _normalize_assign_list(self, assign):
        if not isinstance(assign, list):
            return [None] * 16
        out = []
        for x in assign[:16]:
            if isinstance(x, int) and 1 <= x <= 16:
                out.append(x)
            else:
                out.append(None)
        while len(out) < 16:
            out.append(None)
        return out

    def _active_list_from_assign(self, assign):
        a = self._normalize_assign_list(assign)
        seen = set()
        out = []
        for x in a:
            if isinstance(x, int) and x not in seen:
                out.append(x)
                seen.add(x)
        return out

    def apply_selected_view(self):
        name = self.cmb_views.currentText().strip()
        if not name:
            return
        v = self.views.get(name)
        if not isinstance(v, dict):
            return

        self.visible_panes = clamp_int(v.get("panes", self.visible_panes), 1, 16, self.visible_panes)
        self.active_channels = clamp_int(v.get("active_channels", self.active_channels), 1, 16, self.active_channels)
        self.window_start = clamp_int(v.get("start", self.window_start), 0, 999, self.window_start)

        self.active_list = self._active_list_from_assign(v.get("assign", [])) or list(range(1, 17))

        self.cmb_panes.setCurrentText(str(self.visible_panes))
        self.cmb_active.setCurrentText(str(self.active_channels))

        self._rebuild_grid()
        self._clamp_active_list_to_count()
        self._clamp_window_start()
        self._apply_window()
        self._update_focus()
        self._update_scroll_buttons()

    def save_view_dialog(self):
        name, ok = QInputDialog.getText(self, "Save View", "View name:")
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            return

        assign = []
        for x in self.active_list[:16]:
            assign.append(x if isinstance(x, int) else None)
        while len(assign) < 16:
            assign.append(None)

        view_obj = {
            "panes": self.visible_panes,
            "active_channels": self.active_channels,
            "start": self.window_start,
            "assign": assign
        }
        self.views.save_view(name, view_obj)
        self._reload_views_combo()
        self.cmb_views.setCurrentText(name)

    def delete_selected_view(self):
        name = self.cmb_views.currentText().strip()
        if not name:
            return
        r = QMessageBox.question(
            self, "Delete View", f"Delete view '{name}'?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if r != QMessageBox.Yes:
            return
        self.views.delete(name)
        self._reload_views_combo()

    # ---------- state ----------

    def _state_snapshot(self):
        assign = []
        for x in self.active_list[:16]:
            assign.append(x if isinstance(x, int) else None)
        while len(assign) < 16:
            assign.append(None)
        return {
            "panes": self.visible_panes,
            "active_channels": self.active_channels,
            "start": self.window_start,
            "focus_idx": self.focus_idx,
            "assign": assign,
            "excluded": sorted(self.excluded),
        }

    def _restore_state(self):
        st = safe_read_json(self.cfg.state_file, None)
        if not isinstance(st, dict):
            return

        self.visible_panes = clamp_int(st.get("panes", self.visible_panes), 1, 16, self.visible_panes)
        self.active_channels = clamp_int(st.get("active_channels", self.active_channels), 1, 16, self.active_channels)
        self.window_start = clamp_int(st.get("start", self.window_start), 0, 999, self.window_start)
        self.focus_idx = clamp_int(st.get("focus_idx", self.focus_idx), 0, 15, self.focus_idx)
        self.active_list = self._active_list_from_assign(st.get("assign", [])) or self.active_list

        raw_excl = st.get("excluded", [])
        if isinstance(raw_excl, list):
            self.excluded = {c for c in raw_excl if isinstance(c, int) and 1 <= c <= 16}
        # Strip any excluded channels that crept into active_list
        self.active_list = [c for c in self.active_list if c not in self.excluded]

    def _save_state(self):
        safe_write_json(self.cfg.state_file, self._state_snapshot())

    # ---------- shutdown ----------

    def cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True

        if self.fullscreen is not None:
            try:
                self.fullscreen.close()
            except Exception:
                pass
            self.fullscreen = None

        try:
            self._save_state()
        except Exception:
            pass

        try:
            self.state_timer.stop()
        except Exception:
            pass

        try:
            self.pool.shutdown()
        except Exception:
            pass

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
    # QApplication must exist before any QImage/QPixmap use.
    # ensure_icon_assets() is already called inside MainWindow.__init__().
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    try:
        QGuiApplication.setApplicationDisplayName(APP_NAME)
    except Exception:
        pass

    if os.path.exists(ICON_PATH):
        ico = QIcon(ICON_PATH)
        app.setWindowIcon(ico)
        try:
            QGuiApplication.setWindowIcon(ico)
        except Exception:
            pass

    w = MainWindow()
    app.aboutToQuit.connect(w.cleanup)
    _tick = install_sigint_handler(w)

    w.show()
    sys.exit(app.exec_())