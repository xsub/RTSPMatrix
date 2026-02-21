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
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QButtonGroup, QLabel, QComboBox,
    QDialog, QTextEdit, QInputDialog, QMessageBox,
    QCheckBox
)


# ---------- utils ----------

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

        a = cp["app"] if cp.has_section("app") else {}
        self.title = a.get("title", "RTSPMatrix")
        self.default_panes = int(a.get("default_panes", 4))
        self.views_file = a.get("views_file", "views.json")
        self.state_file = a.get("state_file", "state.json")

    def url(self, channel: int) -> str:
        ch = max(1, min(16, int(channel)))
        return f"rtsp://{self.host}:{self.port}{self.path}?channel={ch}&subtype={self.subtype}"


# ---------- views ----------

class ViewsStore:
    """
    views.json:
    {
      "views": {
        "Direct4": {"panes":4, "virtual":false, "assign":[1,2,3,4]},
        "Virtual14x6": {"panes":6, "virtual":true, "active_channels":14, "start":0}
      }
    }
    """
    def __init__(self, path: str):
        self.path = path
        self.data = safe_read_json(self.path, {"views": {}})
        if "views" not in self.data or not isinstance(self.data["views"], dict):
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


# ---------- UI ----------

class ClickableFrame(QFrame):
    clicked = pyqtSignal()
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class PlayerPane(QWidget):
    def __init__(self, pane_id: int, vlc_instance: vlc.Instance, cfg: RtspConfig, on_focus):
        super().__init__()
        self.pane_id = pane_id
        self.vlc = vlc_instance
        self.cfg = cfg
        self.on_focus = on_focus

        self.frame = ClickableFrame(self)
        self.frame.setFrameShape(QFrame.Box)
        self.frame.setStyleSheet("background: black; border: 3px solid #333;")
        self.frame.setMinimumSize(240, 160)
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
        self._attempt_token = 0
        self._attempt_channel = None

        self.open_timer = QTimer(self)
        self.open_timer.setSingleShot(True)
        self.open_timer.timeout.connect(self._open_timeout)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(120)
        self.poll_timer.timeout.connect(self._poll_state)

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

    def play_channel(self, ch: int):
        self._ensure_player()
        ch = int(max(1, min(16, ch)))

        # avoid restart if already playing this channel
        if self.assigned_channel == ch and self.player is not None and self.player.get_state() == vlc.State.Playing:
            return

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
    def __init__(self, parent, cfg: RtspConfig, panes: int, virtual_mode: bool, active_channels: int, start: int, rows: int, cols: int):
        super().__init__(parent)
        self.setWindowTitle("About RTSPMatrix")
        self.resize(760, 540)

        text = QTextEdit(self)
        text.setReadOnly(True)

        py_ver = sys.version.replace("\n", " ")
        qt_ver = QApplication.qtVersion()

        try:
            pyqt_ver = importlib.metadata.version("PyQt5")
        except Exception:
            pyqt_ver = "unknown"

        try:
            pv_vlc = importlib.metadata.version("python-vlc")
        except Exception:
            pv_vlc = "unknown"

        try:
            libvlc_ver = vlc.libvlc_get_version().decode("utf-8", errors="ignore")
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
        info.append("")
        info.append(f"Displayed tiles: {panes}  (grid {rows}x{cols})")
        info.append(f"Virtual mode: {virtual_mode}")
        info.append(f"Active channels: {active_channels}")
        info.append(f"Viewport start (0-based): {start}")
        info.append("")
        info.append(f"Config: rtsp.ini")
        info.append(f"Views file: {cfg.views_file}")
        info.append(f"State file: {cfg.state_file}")

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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._cleaned = False

        self.cfg = RtspConfig("rtsp.ini")
        self.views = ViewsStore(self.cfg.views_file)

        vlc_args = ["--no-video-title-show"]
        if self.cfg.tcp:
            vlc_args.append("--rtsp-tcp")
        vlc_args.append(f"--network-caching={self.cfg.network_caching_ms}")
        self.vlc = vlc.Instance(vlc_args)

        self.setWindowTitle(self.cfg.title)
        self.resize(1600, 1000)

        # virtual mode state
        self.virtual_mode = False
        self.active_channels = 16          # total "available" channels in virtual set (1..N)
        self.viewport_start = 0            # 0-based start index within active channels
        self.grid_rows = 2
        self.grid_cols = 2

        root = QWidget(self)
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(8)

        # ---- channel buttons 1..16
        self.btn_channels = QButtonGroup(self)
        self.btn_channels.setExclusive(False)
        grid_btn = QGridLayout()
        grid_btn.setSpacing(6)
        for i, ch in enumerate(range(1, 17)):
            b = QPushButton(str(ch), self)
            b.setMinimumHeight(34)
            b.setMinimumWidth(48)
            self.btn_channels.addButton(b, ch)
            grid_btn.addWidget(b, i // 8, i % 8)
        main.addLayout(grid_btn)

        # ---- controls row
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
        self.btn_about = QPushButton("About", self)

        controls.addWidget(self.btn_apply_view)
        controls.addWidget(self.btn_save_view)
        controls.addWidget(self.btn_delete_view)
        controls.addWidget(self.btn_clear_pane)
        controls.addStretch(1)
        controls.addWidget(self.btn_about)

        main.addLayout(controls)

        # ---- status
        self.info = QLabel("", self)
        self.info.setStyleSheet("color: #ddd;")
        main.addWidget(self.info)

        # ---- panes grid (dynamic)
        self.panes_grid = QGridLayout()
        self.panes_grid.setSpacing(8)
        main.addLayout(self.panes_grid, 1)

        self.panes = []
        for pid in range(1, 17):
            pane = PlayerPane(pid, self.vlc, self.cfg, self.set_active_pane)
            self.panes.append(pane)

        # ---- state
        self.active_pane = 1
        self.visible_panes = max(1, min(16, int(self.cfg.default_panes)))

        self._reload_views_combo()
        self._restore_state()

        # ---- wiring
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
        self.btn_about.clicked.connect(self.show_about)

        # ---- apply initial UI state
        self.cmb_panes.setCurrentText(str(self.visible_panes))
        self.cmb_active.setCurrentText(str(self.active_channels))
        self.chk_virtual.setChecked(self.virtual_mode)

        self._rebuild_panes_grid(self.visible_panes)
        self._apply_virtual_window_if_needed()
        self._update_focus()

        self._update_scroll_buttons()

    # ---- layout helpers

    def _clear_layout(self, layout: QGridLayout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                layout.removeWidget(w)

    def _grid_dims(self, n: int):
        n = max(1, min(16, int(n)))

        # preferred wide 2-row layouts for common counts
        if n in (4, 6, 8, 10):
            return 2, n // 2

        # near-square fallback
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))
        return rows, cols

    def _rebuild_panes_grid(self, n: int):
        n = max(1, min(16, int(n)))
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

        if self.active_pane > n:
            self.active_pane = 1

    def _on_panes_changed(self, txt: str):
        try:
            n = int(txt)
        except Exception:
            return
        self._rebuild_panes_grid(n)
        self._clamp_viewport()
        self._apply_virtual_window_if_needed()
        self._update_focus()
        self._update_scroll_buttons()

    def _on_active_channels_changed(self, txt: str):
        try:
            n = int(txt)
        except Exception:
            return
        self.active_channels = max(1, min(16, n))
        self._clamp_viewport()
        self._apply_virtual_window_if_needed()
        self._update_focus()
        self._update_scroll_buttons()

    def _on_virtual_toggled(self, on: bool):
        self.virtual_mode = bool(on)
        self._clamp_viewport()
        self._apply_virtual_window_if_needed()
        self._update_focus()
        self._update_scroll_buttons()

    # ---- focus/status

    def _update_focus(self):
        for p in self.panes:
            p.set_focused(p.pane_id == self.active_pane and p.isVisible())

        if self.virtual_mode:
            start = self.viewport_start + 1
            end = min(self.viewport_start + self.visible_panes, self.active_channels)
            self.info.setText(
                f"Active pane: {self.active_pane} | Virtual window: {start}-{end} / {self.active_channels} | Grid: {self.grid_rows}x{self.grid_cols}"
            )
        else:
            self.info.setText(
                f"Active pane: {self.active_pane} | Direct mode | Grid: {self.grid_rows}x{self.grid_cols}"
            )

    def set_active_pane(self, pane_id: int):
        if pane_id > self.visible_panes:
            pane_id = 1
        self.active_pane = pane_id
        self._update_focus()

    # ---- virtual mode core

    def _max_viewport_start(self):
        return max(0, self.active_channels - self.visible_panes)

    def _clamp_viewport(self):
        self.viewport_start = max(0, min(self.viewport_start, self._max_viewport_start()))

    def _apply_virtual_window_if_needed(self):
        if not self.virtual_mode:
            return

        self._clamp_viewport()

        for i in range(self.visible_panes):
            target = self.viewport_start + i + 1
            pane = self.panes[i]
            if target <= self.active_channels:
                if pane.assigned_channel != target:
                    pane.play_channel(target)
            else:
                pane.stop_to_idle()

        # hide panes above visible already done; clear any beyond visible
        for i in range(self.visible_panes, 16):
            self.panes[i].stop_to_idle()

    def _update_scroll_buttons(self):
        enable = self.virtual_mode and self.active_channels > self.visible_panes
        self.btn_left.setEnabled(enable)
        self.btn_right.setEnabled(enable)

    def scroll_left(self):
        if not self.virtual_mode:
            return
        step = max(1, self.grid_rows)  # one column
        self.viewport_start -= step
        self._clamp_viewport()
        self._apply_virtual_window_if_needed()
        self._update_focus()

    def scroll_right(self):
        if not self.virtual_mode:
            return
        step = max(1, self.grid_rows)  # one column
        self.viewport_start += step
        self._clamp_viewport()
        self._apply_virtual_window_if_needed()
        self._update_focus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.scroll_left()
            return
        if event.key() == Qt.Key_Right:
            self.scroll_right()
            return
        super().keyPressEvent(event)

    # ---- channel buttons behavior

    def on_channel_pressed(self, ch: int):
        ch = int(max(1, min(16, ch)))

        if not self.virtual_mode:
            self.panes[self.active_pane - 1].play_channel(ch)
            return

        # virtual: "jump" so selected channel appears in the active tile (keep window pattern)
        tile_index = self.active_pane - 1
        start = (ch - 1) - tile_index
        self.viewport_start = start
        self._clamp_viewport()
        self._apply_virtual_window_if_needed()
        self._update_focus()

    def clear_active_pane(self):
        if self.virtual_mode:
            # virtual: clearing a single tile breaks the window model; do nothing
            return
        self.panes[self.active_pane - 1].stop_to_idle()

    # ---- views

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

        panes = int(v.get("panes", 4))
        panes = max(1, min(16, panes))
        self.cmb_panes.setCurrentText(str(panes))
        self._rebuild_panes_grid(panes)

        is_virtual = bool(v.get("virtual", False))
        self.virtual_mode = is_virtual
        self.chk_virtual.setChecked(is_virtual)

        if is_virtual:
            self.active_channels = int(v.get("active_channels", self.active_channels))
            self.active_channels = max(1, min(16, self.active_channels))
            self.cmb_active.setCurrentText(str(self.active_channels))

            self.viewport_start = int(v.get("start", 0))
            self._clamp_viewport()
            self._apply_virtual_window_if_needed()
        else:
            assign = v.get("assign", [])
            if not isinstance(assign, list):
                assign = []
            for i in range(self.visible_panes):
                ch = assign[i] if i < len(assign) else None
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
            view_obj = {
                "panes": self.visible_panes,
                "virtual": True,
                "active_channels": self.active_channels,
                "start": self.viewport_start
            }
        else:
            assign = []
            for i in range(self.visible_panes):
                assign.append(self.panes[i].assigned_channel)
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

    # ---- about

    def show_about(self):
        dlg = AboutDialog(
            self, self.cfg,
            self.visible_panes,
            self.virtual_mode,
            self.active_channels,
            self.viewport_start,
            self.grid_rows,
            self.grid_cols
        )
        dlg.exec_()

    # ---- state

    def _state_snapshot(self):
        assign = []
        for i in range(16):
            assign.append(self.panes[i].assigned_channel)

        return {
            "panes": self.visible_panes,
            "active_pane": self.active_pane,
            "virtual": self.virtual_mode,
            "active_channels": self.active_channels,
            "viewport_start": self.viewport_start,
            "assign": assign[:16]
        }

    def _restore_state(self):
        st = safe_read_json(self.cfg.state_file, None)
        if not isinstance(st, dict):
            self.virtual_mode = False
            self.active_channels = 16
            self.viewport_start = 0
            return

        panes = st.get("panes")
        active = st.get("active_pane")
        virt = st.get("virtual")
        active_channels = st.get("active_channels")
        viewport_start = st.get("viewport_start")
        assign = st.get("assign")

        if isinstance(panes, int):
            self.visible_panes = max(1, min(16, panes))
        if isinstance(active, int):
            self.active_pane = max(1, min(16, active))

        self.virtual_mode = bool(virt) if virt is not None else False

        if isinstance(active_channels, int):
            self.active_channels = max(1, min(16, active_channels))
        else:
            self.active_channels = 16

        if isinstance(viewport_start, int):
            self.viewport_start = viewport_start
        else:
            self.viewport_start = 0

        # direct-mode labels only; no auto-play here
        if isinstance(assign, list):
            for i in range(min(16, len(assign))):
                ch = assign[i]
                if isinstance(ch, int) and 1 <= ch <= 16:
                    self.panes[i].assigned_channel = ch
                    self.panes[i].label.setText(f"Pane {i+1}: CH{ch} (saved)")
                else:
                    self.panes[i].assigned_channel = None
                    self.panes[i].label.setText(f"Pane {i+1}: Idle")

    def _save_state(self):
        safe_write_json(self.cfg.state_file, self._state_snapshot())

    # ---- shutdown

    def cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True

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
    w = MainWindow()

    app.aboutToQuit.connect(w.cleanup)
    _tick = install_sigint_handler(w)

    w.show()
    sys.exit(app.exec_())