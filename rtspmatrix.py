import sys
import os
import json
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
    QDialog, QTextEdit, QInputDialog, QMessageBox
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
        # allow ';' inside values (password) without treating it as comment
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
        "FrontDesk": {"panes":4, "assign":[1,2,3,4]},
        "Night": {"panes":1, "assign":[5]}
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

        self._attempt_token += 1
        token = self._attempt_token
        self._attempt_channel = ch

        self.label.setText(f"Pane {self.pane_id}: Opening CH{ch}...")
        p = self._swap_player()

        url = self.cfg.url(ch)
        media = self.vlc.media_new(url)

        # creds passed as VLC options (no URL encoding pitfalls)
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
    def __init__(self, parent, cfg: RtspConfig, panes: int):
        super().__init__(parent)
        self.setWindowTitle("About RTSPMatrix")
        self.resize(720, 520)

        text = QTextEdit(self)
        text.setReadOnly(True)

        py_ver = sys.version.replace("\n", " ")
        qt_ver = QApplication.qtVersion()
        pyqt_ver = getattr(sys.modules.get("PyQt5"), "__version__", "unknown")

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
        info.append(f"Active panes: {panes}")
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

        root = QWidget(self)
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(8)

        # ---- top channel buttons 1..16
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

        # ---- controls row: panes count, views, save/load/delete, clear, about
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

        # ---- status
        self.info = QLabel("Active pane: 1", self)
        self.info.setStyleSheet("color: #ddd;")
        main.addWidget(self.info)

        # ---- 4x4 panes (hide/show up to N)
        self.panes_grid = QGridLayout()
        self.panes_grid.setSpacing(8)

        self.panes = []
        for pid in range(1, 17):
            pane = PlayerPane(pid, self.vlc, self.cfg, self.set_active_pane)
            self.panes.append(pane)
            r = (pid - 1) // 4
            c = (pid - 1) % 4
            self.panes_grid.addWidget(pane, r, c)

        main.addLayout(self.panes_grid, 1)

        # ---- state
        self.active_pane = 1
        self.visible_panes = max(1, min(16, int(self.cfg.default_panes)))

        self._reload_views_combo()

        # ---- wiring
        self.btn_channels.buttonClicked[int].connect(self.on_channel_pressed)
        self.cmb_panes.currentTextChanged.connect(self._on_panes_changed)
        self.btn_apply_view.clicked.connect(self.apply_selected_view)
        self.btn_save_view.clicked.connect(self.save_view_dialog)
        self.btn_delete_view.clicked.connect(self.delete_selected_view)
        self.btn_clear_pane.clicked.connect(self.clear_active_pane)
        self.btn_about.clicked.connect(self.show_about)

        # ---- restore last state (optional)
        self._restore_state()

        # ---- apply initial UI state
        self.cmb_panes.setCurrentText(str(self.visible_panes))
        self._apply_panes_visibility(self.visible_panes)
        self._update_focus()

    # ----- views/state helpers

    def _reload_views_combo(self):
        names = self.views.list_names()
        self.cmb_views.clear()
        self.cmb_views.addItem("")
        for n in names:
            self.cmb_views.addItem(n)

    def _state_snapshot(self):
        assign = []
        for p in self.panes:
            assign.append(p.assigned_channel)
        return {
            "panes": self.visible_panes,
            "active_pane": self.active_pane,
            "assign": assign[:16]
        }

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

        # apply assignments after panes exist; do not auto-play here (keeps startup fast)
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

    # ----- pane focus / layout

    def _update_focus(self):
        for p in self.panes:
            p.set_focused(p.pane_id == self.active_pane and p.isVisible())
        self.info.setText(f"Active pane: {self.active_pane}")

    def set_active_pane(self, pane_id: int):
        if pane_id > self.visible_panes:
            pane_id = 1
        self.active_pane = pane_id
        self._update_focus()

    def _apply_panes_visibility(self, n: int):
        n = max(1, min(16, int(n)))
        self.visible_panes = n
        for i, p in enumerate(self.panes, start=1):
            p.setVisible(i <= n)
        if self.active_pane > n:
            self.active_pane = 1
        self._update_focus()

    def _on_panes_changed(self, txt: str):
        try:
            n = int(txt)
        except Exception:
            return
        self._apply_panes_visibility(n)

    # ----- channel actions

    def on_channel_pressed(self, ch: int):
        pane = self.panes[self.active_pane - 1]
        pane.play_channel(ch)

    def clear_active_pane(self):
        self.panes[self.active_pane - 1].stop_to_idle()

    # ----- views actions

    def apply_selected_view(self):
        name = self.cmb_views.currentText().strip()
        if not name:
            return
        v = self.views.get(name)
        if not isinstance(v, dict):
            return

        panes = v.get("panes", 4)
        assign = v.get("assign", [])

        self.cmb_panes.setCurrentText(str(int(panes)))
        self._apply_panes_visibility(int(panes))

        # apply: start streams for assigned channels; idle for None/missing
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

    # ----- about

    def show_about(self):
        dlg = AboutDialog(self, self.cfg, self.visible_panes)
        dlg.exec_()

    # ----- shutdown

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

    # ensure Python gets chances to process signals while Qt loop runs
    tick = QTimer()
    tick.setInterval(200)
    tick.timeout.connect(lambda: None)
    tick.start()
    return tick  # keep reference alive


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()

    app.aboutToQuit.connect(w.cleanup)
    _tick = install_sigint_handler(w)

    w.show()
    sys.exit(app.exec_())
