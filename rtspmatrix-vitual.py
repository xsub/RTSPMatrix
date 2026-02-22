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
from collections import deque

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
        # allow ';' in password without inline comment parsing
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
        # close AFTER event handler returns (prevents "wrapped C/C++ object deleted" crash)
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
        self.on_pane_click = on_pane_click  # callback(pane_widget, is_double)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.frame = ClickableFrame(self)
        self.frame.setFrameShape(QFrame.Box)
        self.frame.setStyleSheet("background: black; border: 3px solid #333;")
        self.frame.setMinimumSize(160, 120)
        self.frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.frame.clicked.connect(lambda: self.on_pane_click(self, False))
        self.frame.doubleClicked.connect(lambda: self.on_pane_click(self, True))

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
    def __init__(self, parent, cfg: RtspConfig, panes: int, virt: bool,
                 active_count: int, viewport_col: int, rows: int, cols: int, active_list: list):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.resize(820, 600)

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

        total_cols = int(math.ceil(max(1, active_count) / max(1, rows)))

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
        info.append(f"Displayed tiles: {panes} (grid {rows}x{cols})")
        info.append(f"Virtual mode: {virt}")
        info.append(f"Active channels count: {active_count}")
        info.append(f"Virtual matrix: {total_cols} columns x {rows} rows")
        info.append(f"Viewport column (0-based): {viewport_col}")
        info.append(f"Active list (from view assign): {alist}")
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

        # virtual mode state
        self.virtual_mode = False
        self.active_channels = 16               # how many active channels from active_list to use
        self.active_list = list(range(1, 17))   # ordered list of active channels
        self.viewport_col = 0                   # leftmost visible column in virtual matrix

        # grid state
        self.visible_panes = clamp_int(self.cfg.default_panes, 1, 16, 4)
        self.grid_rows = 2
        self.grid_cols = 2

        # persistent virtual columns (each column has `grid_rows` PlayerPane)
        self.virtual_cols = deque()

        # focus (widget reference; robust when panes move)
        self.active_pane_widget = None

        # fullscreen
        self.fullscreen = None
        self.fullscreen_pane = None

        # ui
        self.setWindowTitle(APP_NAME)
        self.resize(1600, 1000)
        self._init_menu()

        root = QWidget(self)
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(8)

        # channel buttons 1..16
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

        # controls row
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

        # status
        self.info = QLabel("", self)
        self.info.setStyleSheet("color: #ddd;")
        main.addWidget(self.info)

        # panes grid
        self.panes_grid = QGridLayout()
        self.panes_grid.setSpacing(8)
        main.addLayout(self.panes_grid, 1)

        # create all panes
        self.panes = []
        for pid in range(1, 17):
            pane = PlayerPane(pid, self.vlc, self.cfg, self.on_pane_clicked)
            self.panes.append(pane)

        # restore state
        self._reload_views_combo()
        self._restore_state()

        # wiring
        self.btn_channels.buttonClicked[int].connect(self.on_channel_pressed)
        self.cmb_panes.currentTextChanged.connect(self._on_panes_changed)
        self.cmb_active.currentTextChanged.connect(self._on_active_channels_changed)
        self.chk_virtual.toggled.connect(self._on_virtual_toggled)
        self.btn_left.clicked.connect(lambda: self.scroll_cols(-1))
        self.btn_right.clicked.connect(lambda: self.scroll_cols(+1))

        self.btn_apply_view.clicked.connect(self.apply_selected_view)
        self.btn_save_view.clicked.connect(self.save_view_dialog)
        self.btn_delete_view.clicked.connect(self.delete_selected_view)
        self.btn_clear_pane.clicked.connect(self.clear_active_pane)

        # apply initial ui state
        self.cmb_panes.setCurrentText(str(self.visible_panes))
        self.cmb_active.setCurrentText(str(self.active_channels))
        self.chk_virtual.setChecked(self.virtual_mode)

        # build layout and start visible streams
        self._rebuild_layout(self.visible_panes)
        self._clamp_active_list_to_count()
        self._clamp_viewport_col()
        self._apply_virtual_visible_streams(full_reload=True)  # first start
        self._update_focus()
        self._update_scroll_buttons()

    def _init_menu(self):
        help_menu = self.menuBar().addMenu("&Help")
        act_about = QAction(f"About {APP_NAME}", self)
        act_about.triggered.connect(self.show_about)
        help_menu.addAction(act_about)

    # ---------- layout/scaling ----------

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

    def _grid_dims(self, n: int):
        n = clamp_int(n, 1, 16, 4)
        # prefer 2 rows for fast column scrolling experience
        if n in (4, 6, 8, 10, 12, 14) and (n % 2 == 0):
            return 2, n // 2
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))
        return rows, cols

    def _rebuild_layout(self, n: int):
        n = clamp_int(n, 1, 16, 4)
        self.visible_panes = n
        self.grid_rows, self.grid_cols = self._grid_dims(n)

        self._clear_layout(self.panes_grid)

        if self.virtual_mode:
            self._init_virtual_columns()
            self._layout_virtual_columns()
        else:
            # direct mode: place first N panes row-major
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

        cw = self.centralWidget()
        if cw is not None:
            lay = cw.layout()
            if lay is not None:
                lay.invalidate()
            cw.updateGeometry()
            cw.repaint()

        if self.active_pane_widget is None or not self.active_pane_widget.isVisible():
            self.active_pane_widget = self._first_visible_pane()

    def _first_visible_pane(self):
        for p in self.panes:
            if p.isVisible():
                return p
        return None

    # ---------- virtual persistent scrolling ----------

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

    def _total_virtual_cols(self):
        rows = max(1, self.grid_rows)
        return int(math.ceil(max(1, len(self.active_list)) / rows))

    def _max_viewport_col(self):
        return max(0, self._total_virtual_cols() - self.grid_cols)

    def _clamp_viewport_col(self):
        self.viewport_col = clamp_int(self.viewport_col, 0, self._max_viewport_col(), 0)

    def _init_virtual_columns(self):
        """
        Initialize columns of panes for the visible grid.
        Column-major assignment: each column has `grid_rows` panes.
        These panes are rotated during scroll so overlapping columns keep playing.
        """
        self.virtual_cols.clear()

        rows = max(1, self.grid_rows)
        cols = max(1, self.grid_cols)
        need = rows * cols

        pool = [self.panes[i] for i in range(need)]
        for i in range(need, 16):
            self.panes[i].setVisible(False)

        for c in range(cols):
            col = []
            for r in range(rows):
                idx = c * rows + r
                if idx < len(pool):
                    p = pool[idx]
                    p.setVisible(True)
                    col.append(p)
            # ensure column length rows (should be)
            self.virtual_cols.append(col)

    def _layout_virtual_columns(self):
        """
        Place current virtual_cols into the grid without restarting streams.
        """
        rows = max(1, self.grid_rows)
        cols = max(1, self.grid_cols)

        for c in range(cols):
            for r in range(rows):
                if c >= len(self.virtual_cols):
                    continue
                if r >= len(self.virtual_cols[c]):
                    continue
                p = self.virtual_cols[c][r]
                self.panes_grid.removeWidget(p)
                self.panes_grid.addWidget(p, r, c)

    def _channel_for_cell(self, abs_col: int, row: int):
        """
        Map virtual matrix cell (abs_col, row) to channel from active_list.
        Column-major packing: abs_col*rows + row.
        """
        rows = max(1, self.grid_rows)
        idx = abs_col * rows + row
        if 0 <= idx < len(self.active_list):
            return self.active_list[idx]
        return None

    def _apply_virtual_visible_streams(self, full_reload: bool):
        """
        Start streams only for visible panes.
        full_reload=True means start all visible panes based on viewport_col.
        full_reload=False means caller already preserved overlapping columns and retuned only new column.
        """
        if not self.virtual_mode:
            return

        rows = max(1, self.grid_rows)
        cols = max(1, self.grid_cols)

        self._clamp_active_list_to_count()
        self._clamp_viewport_col()

        if full_reload:
            # start all visible panes for current viewport
            for c in range(cols):
                abs_col = self.viewport_col + c
                for r in range(rows):
                    if c >= len(self.virtual_cols) or r >= len(self.virtual_cols[c]):
                        continue
                    p = self.virtual_cols[c][r]
                    ch = self._channel_for_cell(abs_col, r)
                    if isinstance(ch, int):
                        p.play_channel(ch)
                    else:
                        p.stop_to_idle()

    def scroll_cols(self, delta_cols: int):
        """
        Scroll virtual matrix by columns.
        Persistent behavior:
        - columns that stay visible keep their PlayerPane instances -> no stream reload
        - only the newly revealed column is (re)loaded
        """
        if not self.virtual_mode:
            return

        rows = max(1, self.grid_rows)
        cols = max(1, self.grid_cols)

        if cols <= 0 or rows <= 0:
            return

        step = 1 if delta_cols > 0 else -1
        count = abs(int(delta_cols))

        for _ in range(count):
            if step > 0:
                if self.viewport_col >= self._max_viewport_col():
                    break
                self.viewport_col += 1

                # rotate columns left: [A,B,C] -> [B,C,A]
                old_left = self.virtual_cols.popleft()
                self.virtual_cols.append(old_left)

                # new rightmost column is old_left; load channels for abs_col = viewport_col + cols - 1
                abs_col = self.viewport_col + cols - 1
                for r in range(rows):
                    if r >= len(old_left):
                        continue
                    ch = self._channel_for_cell(abs_col, r)
                    if isinstance(ch, int):
                        old_left[r].play_channel(ch)
                    else:
                        old_left[r].stop_to_idle()

            else:
                if self.viewport_col <= 0:
                    break
                self.viewport_col -= 1

                # rotate columns right: [A,B,C] -> [C,A,B]
                old_right = self.virtual_cols.pop()
                self.virtual_cols.appendleft(old_right)

                # new leftmost column is old_right; load channels for abs_col = viewport_col
                abs_col = self.viewport_col
                for r in range(rows):
                    if r >= len(old_right):
                        continue
                    ch = self._channel_for_cell(abs_col, r)
                    if isinstance(ch, int):
                        old_right[r].play_channel(ch)
                    else:
                        old_right[r].stop_to_idle()

            self._layout_virtual_columns()

        self._update_focus()
        self._update_scroll_buttons()

    def _virtual_jump_to_channel(self, ch: int):
        """
        Jump by scrolling columns (persistent) so that channel becomes visible.
        Finds its absolute column in active_list and scrolls minimal number of columns.
        """
        rows = max(1, self.grid_rows)
        if rows <= 0:
            return

        try:
            idx = self.active_list.index(ch)
        except ValueError:
            return

        target_abs_col = idx // rows

        # current focused pane position (col in visible grid) affects desired viewport_col
        focus_col = 0
        if self.active_pane_widget is not None:
            pos = self._find_widget_pos_in_virtual(self.active_pane_widget)
            if pos is not None:
                _, focus_col = pos

        desired_viewport = target_abs_col - focus_col
        desired_viewport = max(0, min(desired_viewport, self._max_viewport_col()))
        delta = desired_viewport - self.viewport_col
        if delta != 0:
            self.scroll_cols(delta)

    def _find_widget_pos_in_virtual(self, pane_widget: PlayerPane):
        rows = max(1, self.grid_rows)
        cols = max(1, self.grid_cols)
        for c in range(cols):
            if c >= len(self.virtual_cols):
                continue
            for r in range(rows):
                if r >= len(self.virtual_cols[c]):
                    continue
                if self.virtual_cols[c][r] is pane_widget:
                    return (r, c)
        return None

    # ---------- focus / fullscreen ----------

    def on_pane_clicked(self, pane_widget: PlayerPane, is_double: bool):
        if pane_widget is None or not pane_widget.isVisible():
            return

        self.active_pane_widget = pane_widget
        self._update_focus()

        if is_double:
            self.open_fullscreen_for_pane(pane_widget)

    def open_fullscreen_for_pane(self, pane_widget: PlayerPane):
        if self.fullscreen is not None:
            return

        ch = pane_widget.assigned_channel
        if not isinstance(ch, int) or not (1 <= ch <= 16):
            return

        self.fullscreen_pane = pane_widget
        pane_widget.pause_for_fullscreen()

        self.fullscreen = FullScreenWindow(self.vlc, self.cfg, ch, f"{APP_NAME} - CH{ch}")
        self.fullscreen.closed.connect(self._on_fullscreen_closed)
        self.fullscreen.showFullScreen()

    def _on_fullscreen_closed(self):
        if self.fullscreen is None:
            return
        self.fullscreen = None

        # restore pane stream (same channel) without touching others
        p = self.fullscreen_pane
        self.fullscreen_pane = None
        if p is not None and p.isVisible() and isinstance(p.assigned_channel, int):
            p.play_channel(p.assigned_channel)

        self._update_focus()

    # ---------- controls ----------

    def _update_focus(self):
        for p in self.panes:
            if not p.isVisible():
                p.set_focused(False)
            else:
                p.set_focused(p is self.active_pane_widget)

        if self.virtual_mode:
            rows = max(1, self.grid_rows)
            total_cols = self._total_virtual_cols()
            start_col = self.viewport_col
            end_col = min(self.viewport_col + self.grid_cols - 1, max(0, total_cols - 1))
            self.info.setText(
                f"Focus: {getattr(self.active_pane_widget,'pane_id', '-')}"
                f" | Virtual matrix: {total_cols}x{rows} (cols x rows)"
                f" | View cols: {start_col}-{end_col}"
                f" | Display: {self.grid_rows}x{self.grid_cols}"
            )
        else:
            self.info.setText(
                f"Focus: {getattr(self.active_pane_widget,'pane_id', '-')}"
                f" | Direct mode | Display: {self.grid_rows}x{self.grid_cols}"
            )

    def _update_scroll_buttons(self):
        enable = self.virtual_mode and self._total_virtual_cols() > self.grid_cols
        self.btn_left.setEnabled(enable)
        self.btn_right.setEnabled(enable)

    def _on_panes_changed(self, txt: str):
        n = clamp_int(txt, 1, 16, self.visible_panes)
        self._rebuild_layout(n)
        self._clamp_viewport_col()
        if self.virtual_mode:
            self._apply_virtual_visible_streams(full_reload=True)
        self._update_focus()
        self._update_scroll_buttons()

    def _on_active_channels_changed(self, txt: str):
        self.active_channels = clamp_int(txt, 1, 16, self.active_channels)
        self._clamp_active_list_to_count()
        self._clamp_viewport_col()
        if self.virtual_mode:
            self._apply_virtual_visible_streams(full_reload=True)
        self._update_focus()
        self._update_scroll_buttons()

    def _on_virtual_toggled(self, on: bool):
        self.virtual_mode = bool(on)
        self.viewport_col = 0
        self._rebuild_layout(self.visible_panes)
        self._clamp_active_list_to_count()
        self._clamp_viewport_col()
        if self.virtual_mode:
            self._apply_virtual_visible_streams(full_reload=True)
        self._update_focus()
        self._update_scroll_buttons()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.scroll_cols(-1)
            return
        if event.key() == Qt.Key_Right:
            self.scroll_cols(+1)
            return
        super().keyPressEvent(event)

    def on_channel_pressed(self, ch: int):
        ch = clamp_int(ch, 1, 16, 1)

        if not self.virtual_mode:
            if self.active_pane_widget is not None:
                self.active_pane_widget.play_channel(ch)
            return

        # virtual: prefer "jump" by scrolling columns persistently
        self._virtual_jump_to_channel(ch)

    def clear_active_pane(self):
        if self.virtual_mode:
            return
        if self.active_pane_widget is not None:
            self.active_pane_widget.stop_to_idle()

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
        self.visible_panes = panes
        self.cmb_panes.setCurrentText(str(panes))

        self.virtual_mode = bool(v.get("virtual", False))
        self.chk_virtual.setChecked(self.virtual_mode)

        assign = self._normalize_assign_list(v.get("assign", []))
        self.active_list = self._active_list_from_assign(assign) or list(range(1, 17))

        self.active_channels = clamp_int(v.get("active_channels", len(self.active_list)), 1, 16, min(16, len(self.active_list)))
        self.cmb_active.setCurrentText(str(self.active_channels))

        # 'start' is viewport column in this implementation
        self.viewport_col = clamp_int(v.get("start", 0), 0, 1000, 0)

        self._rebuild_layout(self.visible_panes)
        self._clamp_active_list_to_count()
        self._clamp_viewport_col()

        if self.virtual_mode:
            self._apply_virtual_visible_streams(full_reload=True)
        else:
            # direct: assign per visible pane row-major placement
            # (uses first N panes); start streams
            for i in range(self.visible_panes):
                ch = assign[i]
                if isinstance(ch, int):
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
            # store active_list (padded to 16 with nulls)
            assign = []
            for x in self.active_list[:16]:
                assign.append(x if isinstance(x, int) else None)
            while len(assign) < 16:
                assign.append(None)

            view_obj = {
                "panes": self.visible_panes,
                "virtual": True,
                "active_channels": self.active_channels,
                "start": self.viewport_col,
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
            self.viewport_col,
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
            "virtual": self.virtual_mode,
            "active_channels": self.active_channels,
            "viewport_col": self.viewport_col,
            "direct_assign": direct_assign,
            "virtual_assign": virt_assign
        }

    def _restore_state(self):
        st = safe_read_json(self.cfg.state_file, None)
        if not isinstance(st, dict):
            self.virtual_mode = False
            self.active_channels = 16
            self.active_list = list(range(1, 17))
            self.viewport_col = 0
            return

        self.visible_panes = clamp_int(st.get("panes", self.visible_panes), 1, 16, self.visible_panes)
        self.virtual_mode = bool(st.get("virtual", False))
        self.active_channels = clamp_int(st.get("active_channels", 16), 1, 16, 16)
        self.viewport_col = clamp_int(st.get("viewport_col", 0), 0, 1000, 0)

        va = self._normalize_assign_list(st.get("virtual_assign", []))
        self.active_list = self._active_list_from_assign(va) or list(range(1, 17))
        self._clamp_active_list_to_count()
        self._clamp_viewport_col()

        # restore direct assignments labels (no autoplay here; started after show)
        da = self._normalize_assign_list(st.get("direct_assign", []))
        for i in range(min(self.visible_panes, 16)):
            ch = da[i]
            if isinstance(ch, int):
                self.panes[i].assigned_channel = ch
                self.panes[i].label.setText(f"Pane {i+1}: CH{ch} (saved)")
            else:
                self.panes[i].assigned_channel = None
                self.panes[i].label.setText(f"Pane {i+1}: Idle")

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
            self.fullscreen_pane = None

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

    # start direct-mode saved streams on startup (virtual already starts its visible streams)
    if not w.virtual_mode:
        for i in range(w.visible_panes):
            ch = w.panes[i].assigned_channel
            if isinstance(ch, int) and 1 <= ch <= 16:
                w.panes[i].play_channel(ch)

    sys.exit(app.exec_())