# rtspmatrix_virtual.py
import sys
import os
import json
import math
import signal
import platform
import threading
import configparser
import importlib.metadata

import vlc
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QT_VERSION_STR, PYQT_VERSION_STR
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QButtonGroup, QLabel, QComboBox,
    QDialog, QTextEdit, QInputDialog, QMessageBox,
    QCheckBox, QSizePolicy, QAction
)

APP_NAME = "RTSPMatrix-Virtual"


# ---------------- utils ----------------

def dispose_player_async(p: vlc.MediaPlayer):
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


def clamp_int(v, lo, hi, default):
    try:
        v = int(v)
    except Exception:
        return default
    return max(lo, min(hi, v))


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
        self.open_timeout_ms = s.getint("open_timeout_ms", 2500)

        a = cp["app"] if cp.has_section("app") else {}
        self.title = a.get("title", APP_NAME)
        self.default_panes = int(a.get("default_panes", 4))
        self.views_file = a.get("views_file", "views.json")
        self.state_file = a.get("state_file", "state.json")

    def url(self, channel: int) -> str:
        ch = max(1, min(16, int(channel)))
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


# ---------------- UI widgets ----------------

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


class FullScreenWindow(QMainWindow):
    closed = pyqtSignal()

    def __init__(self, vlc_instance: vlc.Instance, cfg: RtspConfig, channel: int, title: str):
        super().__init__()
        self.vlc = vlc_instance
        self.cfg = cfg
        self.channel = clamp_int(channel, 1, 16, 1)
        self.setWindowTitle(title)

        root = QWidget(self)
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.video = ClickableFrame(self)
        self.video.setStyleSheet("background: black;")
        self.video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video.clicked.connect(lambda: QTimer.singleShot(0, self.close))
        v.addWidget(self.video, 1)

        self.player = self.vlc.media_player_new()
        QTimer.singleShot(0, self._start)

    def _bind_player_window(self):
        wid = int(self.video.winId())
        if sys.platform.startswith("linux"):
            self.player.set_xwindow(wid)
        elif sys.platform.startswith("win"):
            self.player.set_hwnd(wid)
        elif sys.platform.startswith("darwin"):
            self.player.set_nsobject(wid)
        else:
            self.player.set_xwindow(wid)

    def _start(self):
        self._bind_player_window()

        url = self.cfg.url(self.channel)
        media = self.vlc.media_new(url)

        if self.cfg.user:
            media.add_option(f":rtsp-user={self.cfg.user}")
        if self.cfg.password:
            media.add_option(f":rtsp-pwd={self.cfg.password}")
        if self.cfg.tcp:
            media.add_option(":rtsp-tcp")
        media.add_option(f":network-caching={self.cfg.network_caching_ms}")

        self.player.set_media(media)
        try:
            self.player.play()
        except Exception:
            pass

        self.showFullScreen()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        try:
            dispose_player_async(self.player)
        except Exception:
            pass
        self.closed.emit()
        super().closeEvent(event)


class PlayerPane(QWidget):
    def __init__(self, pane_id: int, vlc_instance: vlc.Instance, cfg: RtspConfig, on_pane_click):
        super().__init__()
        self.pane_id = pane_id
        self.vlc = vlc_instance
        self.cfg = cfg
        self.on_pane_click = on_pane_click  # callback(pane_id, is_double)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.frame = ClickableFrame(self)
        self.frame.setFrameShape(QFrame.Box)
        self.frame.setStyleSheet("background: black; border: 3px solid #333;")
        self.frame.setMinimumSize(160, 120)
        self.frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.frame.clicked.connect(lambda: self.on_pane_click(self.pane_id, False))
        self.frame.doubleClicked.connect(lambda: self.on_pane_click(self.pane_id, True))

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
        self._attempt_token = 0
        self._attempt_channel = None

        self.open_timer = QTimer(self)
        self.open_timer.setSingleShot(True)
        self.open_timer.timeout.connect(self._open_timeout)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(120)
        self.poll_timer.timeout.connect(self._poll_state)

        QTimer.singleShot(0, self._ensure_player)

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

    def stop_to_idle(self):
        if self.open_timer.isActive():
            self.open_timer.stop()
        if self.poll_timer.isActive():
            self.poll_timer.stop()
        if self.player is not None:
            dispose_player_async(self.player)
        self.player = self._new_player()
        self.assigned_channel = None
        self._attempt_channel = None
        self.label.setText(f"Pane {self.pane_id}: Idle")

    def pause_for_fullscreen(self):
        if self.open_timer.isActive():
            self.open_timer.stop()
        if self.poll_timer.isActive():
            self.poll_timer.stop()
        if self.player is not None:
            dispose_player_async(self.player)
        self.player = None
        if isinstance(self.assigned_channel, int):
            self.label.setText(f"Pane {self.pane_id}: CH{self.assigned_channel} (fullscreen)")
        else:
            self.label.setText(f"Pane {self.pane_id}: Idle")

    def play_channel(self, ch: int):
        self._ensure_player()
        ch = clamp_int(ch, 1, 16, 1)

        try:
            if self.assigned_channel == ch and self.player and self.player.get_state() == vlc.State.Playing:
                return
        except Exception:
            pass

        self._attempt_token += 1
        token = self._attempt_token
        self._attempt_channel = ch

        self.label.setText(f"Pane {self.pane_id}: Opening CH{ch}...")
        p = self._swap_player()

        url = self.cfg.url(ch)
        media = self.vlc.media_new(url)

        if self.cfg.user:
            media.add_option(f":rtsp-user={self.cfg.user}")
        if self.cfg.password:
            media.add_option(f":rtsp-pwd={self.cfg.password}")
        if self.cfg.tcp:
            media.add_option(":rtsp-tcp")
        media.add_option(f":network-caching={self.cfg.network_caching_ms}")

        p.set_media(media)

        try:
            p.play()
        except Exception:
            self._fail(token, ch, f"Pane {self.pane_id}: No stream on CH{ch}")
            return

        self.open_timer.start(self.cfg.open_timeout_ms)
        if not self.poll_timer.isActive():
            self.poll_timer.start()

    def _poll_state(self):
        if self.player is None:
            return

        state = self.player.get_state()
        token = self._attempt_token
        ch = self._attempt_channel
        if ch is None:
            return

        if state == vlc.State.Playing:
            if self.open_timer.isActive():
                self.open_timer.stop()
            self.poll_timer.stop()
            self.assigned_channel = ch
            self.label.setText(f"Pane {self.pane_id}: CH{ch} playing")
            return

        if state in (vlc.State.Error, vlc.State.Ended, vlc.State.Stopped):
            self._fail(token, ch, f"Pane {self.pane_id}: No stream on CH{ch}")

    def _open_timeout(self):
        if self.player is None:
            return
        ch = self._attempt_channel
        token = self._attempt_token
        if ch is None:
            return
        if self.player.get_state() != vlc.State.Playing:
            self._fail(token, ch, f"Pane {self.pane_id}: No stream on CH{ch}")

    def _fail(self, token: int, ch: int, msg: str):
        if token != self._attempt_token:
            return

        if self.open_timer.isActive():
            self.open_timer.stop()
        if self.poll_timer.isActive():
            self.poll_timer.stop()

        self.label.setText(msg)

        if self.player is not None:
            dispose_player_async(self.player)
        self.player = self._new_player()
        self.assigned_channel = None

    def shutdown(self):
        if self.open_timer.isActive():
            self.open_timer.stop()
        if self.poll_timer.isActive():
            self.poll_timer.stop()
        if self.player is not None:
            dispose_player_async(self.player)
            self.player = None


class AboutDialog(QDialog):
    def __init__(self, parent, cfg: RtspConfig, panes: int, virt: bool, active_count: int,
                 start: int, rows: int, cols: int, active_list: list):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.resize(780, 560)

        text = QTextEdit(self)
        text.setReadOnly(True)

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

        alist = [c for c in (active_list or []) if isinstance(c, int)]
        if len(alist) > 16:
            alist = alist[:16]

        info = []
        info.append(f"App: {APP_NAME}")
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
        info.append("")
        info.append(f"Displayed tiles: {panes}  (grid {rows}x{cols})")
        info.append(f"Virtual mode: {virt}")
        info.append(f"Active channels count: {active_count}")
        info.append(f"Viewport start (0-based): {start}")
        info.append(f"Active list: {alist}")
        info.append("")
        info.append("Files:")
        info.append(f"  rtsp.ini")
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


# ---------------- main window ----------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._cleaned = False

        self.cfg = RtspConfig("rtsp.ini")
        self.cfg.title = APP_NAME
        self.views = ViewsStore(self.cfg.views_file)

        vlc_args = ["--no-video-title-show"]
        if self.cfg.tcp:
            vlc_args.append("--rtsp-tcp")
        vlc_args.append(f"--network-caching={self.cfg.network_caching_ms}")
        self.vlc = vlc.Instance(vlc_args)

        self.virtual_mode = False
        self.active_channels = 16
        self.viewport_start = 0
        self.active_list = list(range(1, 17))

        self.grid_rows = 2
        self.grid_cols = 2

        self.fullscreen = None
        self.fullscreen_ctx = None

        self.setWindowTitle(APP_NAME)
        self.resize(1600, 1000)
        self._init_menu()

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
            b = QPushButton(str(ch), self)
            b.setMinimumHeight(34)
            b.setMinimumWidth(48)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.btn_channels.addButton(b, ch)
            grid_btn.addWidget(b, i // 8, i % 8)
        main.addLayout(grid_btn)

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

        self.chk_virtual = QCheckBox("Virtual", self)
        controls.addWidget(self.chk_virtual)

        self.btn_left = QPushButton("◀", self)
        self.btn_right = QPushButton("▶", self)
        self.btn_left.setFixedWidth(44)
        self.btn_right.setFixedWidth(44)
        controls.addWidget(self.btn_left)
        controls.addWidget(self.btn_right)

        controls.addWidget(QLabel("View:", self))
        self.cmb_views = QComboBox(self)
        controls.addWidget(self.cmb_views)

        self.btn_apply_view = QPushButton("Apply", self)
        self.btn_save_view = QPushButton("Save", self)
        self.btn_delete_view = QPushButton("Delete", self)
        self.btn_clear_pane = QPushButton("Clear pane", self)

        controls.addWidget(self.btn_apply_view)
        controls.addWidget(self.btn_save_view)
        controls.addWidget(self.btn_delete_view)
        controls.addWidget(self.btn_clear_pane)
        controls.addStretch(1)

        main.addLayout(controls)

        self.info = QLabel("", self)
        self.info.setStyleSheet("color: #ddd;")
        main.addWidget(self.info)

        self.panes_grid = QGridLayout()
        self.panes_grid.setSpacing(8)
        main.addLayout(self.panes_grid, 1)

        self.panes = []
        for pid in range(1, 17):
            pane = PlayerPane(pid, self.vlc, self.cfg, self.on_pane_clicked)
            self.panes.append(pane)

        self.active_pane = 1
        self.visible_panes = clamp_int(self.cfg.default_panes, 1, 16, 4)

        self._reload_views_combo()
        self._restore_state()

        self.btn_channels.buttonClicked[int].connect(self.on_channel_pressed)
        self.cmb_panes.currentTextChanged.connect(self._on_panes_changed)
        self.cmb_active.currentTextChanged.connect(self._on_active_channels_changed)
        self.chk_virtual.toggled.connect(self._on_virtual_toggled)
        self.btn_left.clicked.connect(self.scroll_left)
        self.btn_right.clicked.connect(self.scroll_right)

        self.btn_apply_view.clicked.connect(self.apply_selected_view)
        self.btn_save_view.clicked.connect(self.save_view_dialog)
        self.btn_delete_view.clicked.connect(self.delete_selected_view)
        self.btn_clear_pane.clicked.connect(self.clear_active_pane)

        self.cmb_panes.setCurrentText(str(self.visible_panes))
        self.cmb_active.setCurrentText(str(self.active_channels))
        self.chk_virtual.setChecked(self.virtual_mode)

        self._rebuild_panes_grid(self.visible_panes)
        self._clamp_active_list_to_count()
        self._clamp_viewport()
        self._apply_mapping(play=True)

        self._update_focus()
        self._update_scroll_buttons()

    def _init_menu(self):
        help_menu = self.menuBar().addMenu("&Help")
        act_about = QAction(f"About {APP_NAME}", self)
        act_about.triggered.connect(self.show_about)
        help_menu.addAction(act_about)

    # ---------- grid/layout ----------

    def _clear_layout(self, layout: QGridLayout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

    def _grid_dims(self, n: int):
        n = clamp_int(n, 1, 16, 4)
        if n in (4, 6, 8, 10, 12, 14) and (n % 2 == 0):
            return 2, n // 2
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))
        return rows, cols

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

    def _rebuild_panes_grid(self, n: int):
        n = clamp_int(n, 1, 16, 4)
        self.visible_panes = n
        self.grid_rows, self.grid_cols = self._grid_dims(n)

        self._clear_layout(self.panes_grid)

        for i in range(16):
            p = self.panes[i]
            if i < n:
                p.setVisible(True)
                r = i // self.grid_cols
                c = i % self.grid_cols
                self.panes_grid.addWidget(p, r, c)
            else:
                p.setVisible(False)

        self._apply_grid_stretch(self.grid_rows, self.grid_cols)

        if self.active_pane > n:
            self.active_pane = 1

        cw = self.centralWidget()
        if cw is not None:
            lay = cw.layout()
            if lay is not None:
                lay.invalidate()
            cw.updateGeometry()
            cw.repaint()

    def _on_panes_changed(self, txt: str):
        n = clamp_int(txt, 1, 16, self.visible_panes)
        self._rebuild_panes_grid(n)
        self._clamp_viewport()
        self._apply_mapping(play=True)
        self._update_focus()
        self._update_scroll_buttons()

    # ---------- pane focus / fullscreen ----------

    def on_pane_clicked(self, pane_id: int, is_double: bool):
        pane_id = clamp_int(pane_id, 1, 16, 1)
        if pane_id > self.visible_panes:
            pane_id = 1

        self.active_pane = pane_id
        self._update_focus()

        if is_double:
            self.open_fullscreen_for_active()

    def open_fullscreen_for_active(self):
        if self.fullscreen is not None:
            return

        pane = self.panes[self.active_pane - 1]
        ch = pane.assigned_channel
        if not isinstance(ch, int) or not (1 <= ch <= 16):
            return

        pane.pause_for_fullscreen()

        self.fullscreen_ctx = {
            "pane_id": self.active_pane,
            "channel": ch,
            "virtual": self.virtual_mode,
            "viewport_start": self.viewport_start
        }

        self.fullscreen = FullScreenWindow(self.vlc, self.cfg, ch, f"{APP_NAME} - CH{ch}")
        self.fullscreen.closed.connect(self._on_fullscreen_closed)
        self.fullscreen.showFullScreen()

    def _on_fullscreen_closed(self):
        ctx = self.fullscreen_ctx or {}
        self.fullscreen = None
        self.fullscreen_ctx = None

        if ctx.get("virtual"):
            self._apply_mapping(play=True)
        else:
            pid = clamp_int(ctx.get("pane_id", 1), 1, self.visible_panes, 1)
            ch = clamp_int(ctx.get("channel", 1), 1, 16, 1)
            self.panes[pid - 1].play_channel(ch)

        self._update_focus()

    # ---------- virtual list model ----------

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

    def _clamp_active_list_to_count(self):
        self.active_channels = clamp_int(self.active_channels, 1, 16, 16)

        if not isinstance(self.active_list, list):
            self.active_list = []

        tmp = []
        seen = set()
        for x in self.active_list:
            if isinstance(x, int) and 1 <= x <= 16 and x not in seen:
                tmp.append(x)
                seen.add(x)
        self.active_list = tmp

        if len(self.active_list) > self.active_channels:
            self.active_list = self.active_list[:self.active_channels]
            return

        if len(self.active_list) < self.active_channels:
            missing = [c for c in range(1, 17) if c not in set(self.active_list)]
            need = self.active_channels - len(self.active_list)
            self.active_list.extend(missing[:need])

    def _max_viewport_start(self):
        return max(0, len(self.active_list) - self.visible_panes)

    def _clamp_viewport(self):
        self.viewport_start = clamp_int(self.viewport_start, 0, self._max_viewport_start(), 0)

    def _apply_mapping(self, play: bool):
        if not self.virtual_mode:
            for i in range(self.visible_panes, 16):
                self.panes[i].stop_to_idle()
            return

        self._clamp_active_list_to_count()
        self._clamp_viewport()

        for i in range(self.visible_panes):
            pane = self.panes[i]
            idx = self.viewport_start + i
            if idx < len(self.active_list):
                ch = self.active_list[idx]
                if play:
                    pane.play_channel(ch)
                else:
                    pane.assigned_channel = ch
                    pane.label.setText(f"Pane {pane.pane_id}: CH{ch} (saved)")
            else:
                pane.stop_to_idle()

        for i in range(self.visible_panes, 16):
            self.panes[i].stop_to_idle()

    # ---------- controls ----------

    def _update_focus(self):
        for p in self.panes:
            p.set_focused(p.pane_id == self.active_pane and p.isVisible())

        if self.virtual_mode:
            start = self.viewport_start + 1
            end = min(self.viewport_start + self.visible_panes, len(self.active_list))
            self.info.setText(
                f"Active pane: {self.active_pane} | Virtual window: {start}-{end} / {len(self.active_list)} | Grid: {self.grid_rows}x{self.grid_cols}"
            )
        else:
            self.info.setText(
                f"Active pane: {self.active_pane} | Direct mode | Grid: {self.grid_rows}x{self.grid_cols}"
            )

    def _update_scroll_buttons(self):
        enable = self.virtual_mode and len(self.active_list) > self.visible_panes
        self.btn_left.setEnabled(enable)
        self.btn_right.setEnabled(enable)

    def _on_active_channels_changed(self, txt: str):
        self.active_channels = clamp_int(txt, 1, 16, self.active_channels)
        self._clamp_active_list_to_count()
        self._clamp_viewport()
        self._apply_mapping(play=True)
        self._update_focus()
        self._update_scroll_buttons()

    def _on_virtual_toggled(self, on: bool):
        self.virtual_mode = bool(on)
        self._clamp_active_list_to_count()
        self._clamp_viewport()
        self._apply_mapping(play=True)
        self._update_focus()
        self._update_scroll_buttons()

    def scroll_left(self):
        if not self.virtual_mode:
            return
        step = max(1, self.grid_rows)
        self.viewport_start -= step
        self._clamp_viewport()
        self._apply_mapping(play=True)
        self._update_focus()

    def scroll_right(self):
        if not self.virtual_mode:
            return
        step = max(1, self.grid_rows)
        self.viewport_start += step
        self._clamp_viewport()
        self._apply_mapping(play=True)
        self._update_focus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.scroll_left()
            return
        if event.key() == Qt.Key_Right:
            self.scroll_right()
            return
        super().keyPressEvent(event)

    def on_channel_pressed(self, ch: int):
        ch = clamp_int(ch, 1, 16, 1)

        if not self.virtual_mode:
            self.panes[self.active_pane - 1].play_channel(ch)
            return

        tile_index = self.active_pane - 1
        try:
            idx = self.active_list.index(ch)
            self.viewport_start = idx - tile_index
        except ValueError:
            pos = self.viewport_start + tile_index
            if 0 <= pos < len(self.active_list):
                self.active_list[pos] = ch
            else:
                self.active_list.append(ch)
                self.active_channels = min(16, len(self.active_list))
                self.cmb_active.setCurrentText(str(self.active_channels))
            self._clamp_active_list_to_count()

        self._clamp_viewport()
        self._apply_mapping(play=True)
        self._update_focus()
        self._update_scroll_buttons()

    def clear_active_pane(self):
        if self.virtual_mode:
            return
        self.panes[self.active_pane - 1].stop_to_idle()

    # ---------- views ----------

    def _reload_views_combo(self):
        names = self.views.list_names()
        self.cmb_views.clear()
        self.cmb_views.addItem("")
        for n in names:
            self.cmb_views.addItem(n)

    def apply_selected_view(self):
        name = self.cmb_views.currentText().strip()
        if not name:
            return
        v = self.views.get(name)
        if not isinstance(v, dict):
            return

        panes = clamp_int(v.get("panes", 4), 1, 16, 4)
        self.cmb_panes.setCurrentText(str(panes))
        self._rebuild_panes_grid(panes)

        is_virtual = bool(v.get("virtual", False))
        self.virtual_mode = is_virtual
        self.chk_virtual.setChecked(is_virtual)

        assign = self._normalize_assign_list(v.get("assign", []))

        if is_virtual:
            al = self._active_list_from_assign(assign)
            self.active_list = al if al else list(range(1, 17))

            ac = v.get("active_channels", len(self.active_list))
            self.active_channels = clamp_int(ac, 1, 16, min(16, len(self.active_list)))
            self.cmb_active.setCurrentText(str(self.active_channels))
            self._clamp_active_list_to_count()

            self.viewport_start = clamp_int(v.get("start", 0), 0, self._max_viewport_start(), 0)
            self._apply_mapping(play=True)
        else:
            for i in range(self.visible_panes):
                ch = assign[i]
                if isinstance(ch, int) and 1 <= ch <= 16:
                    self.panes[i].play_channel(ch)
                else:
                    self.panes[i].stop_to_idle()

        self._update_focus()
        self._update_scroll_buttons()

    def save_view_dialog(self):
        name, ok = QInputDialog.getText(self, "Save View", "View name:")
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            return

        if self.virtual_mode:
            assign = []
            for x in self.active_list[:16]:
                assign.append(x if isinstance(x, int) else None)
            while len(assign) < 16:
                assign.append(None)

            view_obj = {
                "panes": self.visible_panes,
                "virtual": True,
                "active_channels": self.active_channels,
                "start": self.viewport_start,
                "assign": assign
            }
        else:
            assign = []
            for i in range(16):
                if i < self.visible_panes:
                    ch = self.panes[i].assigned_channel
                    assign.append(ch if isinstance(ch, int) else None)
                else:
                    assign.append(None)

            view_obj = {
                "panes": self.visible_panes,
                "virtual": False,
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

    # ---------- about ----------

    def show_about(self):
        dlg = AboutDialog(
            self, self.cfg,
            self.visible_panes,
            self.virtual_mode,
            self.active_channels,
            self.viewport_start,
            self.grid_rows,
            self.grid_cols,
            self.active_list
        )
        dlg.exec_()

    # ---------- state ----------

    def _state_snapshot(self):
        direct_assign = []
        for i in range(16):
            ch = self.panes[i].assigned_channel
            direct_assign.append(ch if isinstance(ch, int) else None)

        virt_assign = []
        for x in self.active_list[:16]:
            virt_assign.append(x if isinstance(x, int) else None)
        while len(virt_assign) < 16:
            virt_assign.append(None)

        return {
            "panes": self.visible_panes,
            "active_pane": self.active_pane,
            "virtual": self.virtual_mode,
            "active_channels": self.active_channels,
            "viewport_start": self.viewport_start,
            "direct_assign": direct_assign,
            "virtual_assign": virt_assign
        }

    def _restore_state(self):
        st = safe_read_json(self.cfg.state_file, None)
        if not isinstance(st, dict):
            self.virtual_mode = False
            self.active_channels = 16
            self.viewport_start = 0
            self.active_list = list(range(1, 17))
            return

        self.visible_panes = clamp_int(st.get("panes", self.visible_panes), 1, 16, self.visible_panes)
        self.active_pane = clamp_int(st.get("active_pane", 1), 1, 16, 1)

        self.virtual_mode = bool(st.get("virtual", False))
        self.active_channels = clamp_int(st.get("active_channels", 16), 1, 16, 16)
        self.viewport_start = clamp_int(st.get("viewport_start", 0), 0, 15, 0)

        va = self._normalize_assign_list(st.get("virtual_assign", []))
        al = self._active_list_from_assign(va)
        self.active_list = al if al else list(range(1, 17))
        self._clamp_active_list_to_count()

        da = self._normalize_assign_list(st.get("direct_assign", []))
        for i in range(min(self.visible_panes, 16)):
            ch = da[i]
            if isinstance(ch, int) and 1 <= ch <= 16:
                self.panes[i].assigned_channel = ch
                self.panes[i].label.setText(f"Pane {i+1}: CH{ch} (saved)")
            else:
                self.panes[i].assigned_channel = None
                self.panes[i].label.setText(f"Pane {i+1}: Idle")

        self._clamp_viewport()

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
            self.fullscreen_ctx = None

        self._save_state()

        for p in self.panes:
            p.shutdown()
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
    app.setApplicationName(APP_NAME)
    try:
        QGuiApplication.setApplicationDisplayName(APP_NAME)
    except Exception:
        pass

    w = MainWindow()

    app.aboutToQuit.connect(w.cleanup)
    _tick = install_sigint_handler(w)

    w.show()

    if not w.virtual_mode:
        for i in range(w.visible_panes):
            ch = w.panes[i].assigned_channel
            if isinstance(ch, int) and 1 <= ch <= 16:
                w.panes[i].play_channel(ch)

    sys.exit(app.exec_())