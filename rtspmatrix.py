# rtspmatrix.py
# Changes vs your current:
# - VLC quieter: --quiet / --verbose=0
# - RTSP hardening: :rtsp-tcp + :rtsp-keepalive + :rtsp-timeout
# - macOS deadlock mitigation: disable HW decode (VideoToolbox) via --avcodec-hw=none and :avcodec-hw=none
# - Auto-resume: when stream drops or stalls -> exponential backoff retry, using NEW MediaPlayer on each retry
# - AboutDialog fixed (QT_VERSION_STR / PYQT_VERSION_STR)

import sys
import os
import math
import signal
import logging
import platform
import time
import configparser
import importlib.metadata

import vlc
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QT_VERSION_STR, PYQT_VERSION_STR
from PyQt5.QtGui import QGuiApplication, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame,
    QVBoxLayout, QHBoxLayout, QGridLayout, QSizePolicy,
    QPushButton, QButtonGroup, QLabel, QComboBox,
    QDialog, QTextEdit, QInputDialog, QMessageBox
)

from rtspmatrix_common import (
    log,
    setup_logging,
    safe_read_json,
    safe_write_json,
    parse_labels,
    dispose_player_async,
    join_disposal_threads,
    bind_player_to_window,
    profiler,
)

# App icon — tried in order; first hit wins.  .ico works everywhere; .png
# is the fallback for macOS/Linux where .ico support is sometimes patchy.
_ICON_CANDIDATES = (
    os.path.join("assets", "rtspmatrix.ico"),
    os.path.join("assets", "rtspmatrix_icon.png"),
)


def _load_app_icon():
    for p in _ICON_CANDIDATES:
        if os.path.isfile(p):
            return QIcon(p)
    return None


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
        # `subtype` is the legacy/global stream selector.  Two new keys override
        # it per-context for the bandwidth/CPU win:
        #   subtype_tile = stream used while rendered in a grid tile (default
        #                  same as `subtype` for backward compat; set to 1 for
        #                  the substream)
        #   subtype_full = stream used while in fullscreen / single-pane (the
        #                  classic variant has no fullscreen yet, so this is
        #                  reserved for future use)
        self.subtype = s.getint("subtype", 0)
        self.subtype_tile = s.getint("subtype_tile", self.subtype)
        self.subtype_full = s.getint("subtype_full", self.subtype)
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
        self.log_level = a.get("log_level", "INFO")
        self.log_file = a.get("log_file", "")
        # Delay (in ms) between opening individual RTSP streams during a
        # batch (pane-count increase / view apply).  Set to 0 to open all
        # at once.  200 ms smooths out the DVR+network load burst.
        self.stagger_open_ms = int(a.get("stagger_open_ms", 200))
        # Enable the internal profiler (named timers / counters / gauges
        # dumped to the log every profile_dump_interval_s seconds).  Zero
        # overhead when disabled.
        self.profile = a.get("profile", "false").strip().lower() in (
            "1", "true", "yes", "on"
        )
        self.profile_dump_interval_s = int(a.get("profile_dump_interval_s", 10))

        raw_labels = cp["view"].get("labels", "") if cp.has_section("view") else ""
        self.labels = parse_labels(raw_labels)

    def url(self, channel: int, hd: bool = False) -> str:
        ch = max(1, min(16, int(channel)))
        sub = self.subtype_full if hd else self.subtype_tile
        return f"rtsp://{self.host}:{self.port}{self.path}?channel={ch}&subtype={sub}"

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
    doubleClicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Accept + return so we never call super().mousePressEvent on a
            # widget that may have been destroyed by the connected slot
            # (e.g. clicked -> close() -> Qt destroys this frame), which
            # otherwise raises "wrapped C/C++ object has been deleted".
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


class PlayerPane(QWidget):
    """
    Auto-resume:
      - libVLC state events drive the state machine on the GUI thread
        (queued via a Qt signal because the events fire on libVLC's threads)
      - a slow watchdog timer covers the silent-stall case (libVLC keeps
        reporting Playing but no new frames arrive)
      - on drop/stall the retry uses a NEW MediaPlayer (old disposed async)
        to escape broken live555 state
    """

    # libVLC events fire on libVLC's internal threads.  Emitting a Qt signal
    # marshals the call back to the GUI thread via a queued connection.  The
    # int is the player generation: events from a swapped-out player carry an
    # older generation and the slot drops them.
    state_event = pyqtSignal(str, int)

    def __init__(self, pane_id: int, vlc_instance: vlc.Instance, cfg: RtspConfig,
                 on_focus, on_dblclick=None):
        super().__init__()
        self.pane_id = pane_id
        self.vlc = vlc_instance
        self.cfg = cfg
        self.on_focus = on_focus
        self.on_dblclick = on_dblclick

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
        self.frame.doubleClicked.connect(self._dblclicked)

        # Stats overlay in top-left of the video frame.  WA_NativeWindow so
        # it gets its own NSView / X window and stacks above the libVLC
        # render surface on macOS (a plain QLabel child would be obscured
        # by the Metal/CoreGraphics output).
        self.fps_label = QLabel("", self.frame)
        self.fps_label.setAttribute(Qt.WA_NativeWindow, True)
        self.fps_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.fps_label.setStyleSheet(
            "QLabel {"
            " color: #00ff00;"
            " font-family: 'Courier New', 'Courier', 'Menlo', monospace;"
            " font-size: 12px;"
            " font-weight: bold;"
            " background: rgba(0, 0, 0, 150);"
            " padding: 2px 4px;"
            "}"
        )
        self.fps_label.move(5, 5)
        self.fps_label.hide()

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
        self._last_progress_ts = 0.0
        self._is_playing = False
        self._player_gen = 0
        # Timestamp of the most recent _start_play; cleared on first
        # "playing" event so the profiler records time-to-first-frame.
        self._play_started_ts = 0.0

        # The watchdog only checks "are we still making forward progress" —
        # it does not poll get_state().  All state transitions come from
        # libVLC events via state_event.
        watchdog_ms = max(self.cfg.poll_interval_ms, 1000)
        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.setInterval(watchdog_ms)
        self.watchdog_timer.timeout.connect(self._watchdog_tick)

        self.open_timer = QTimer(self)
        self.open_timer.setSingleShot(True)
        self.open_timer.timeout.connect(self._open_timeout)

        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self._retry_now)

        # Queued connection (default for cross-thread emit) is what makes
        # the libVLC-thread -> GUI-thread handoff safe.
        self.state_event.connect(self._on_state_event)

        # Per-pane stats: FPS + bitrate + lost frames.  FPS is counted from
        # libVLC "time" events (one per displayed frame PTS update) which
        # is far more reliable than MediaStats.decoded_video on RTSP
        # streams.  Bitrate comes from MediaStats.input_bitrate.
        self._time_event_count = 0
        self._last_time_event_count = 0
        self._last_decoded_video = 0
        self._last_read_bytes = 0
        self._last_lost_pictures = 0
        self._last_stats_ts = 0.0
        self._last_bitrate_bps = 0.0
        self._last_fps = 0.0
        self._last_lost = 0
        self.stats_timer = QTimer(self)
        self.stats_timer.setInterval(1000)
        self.stats_timer.timeout.connect(self._update_stats)
        self.stats_timer.start()

        QTimer.singleShot(0, self._ensure_player)

    def _clicked(self):
        self.on_focus(self.pane_id)

    def _dblclicked(self):
        if self.on_dblclick is not None:
            self.on_dblclick(self.pane_id)

    def set_focused(self, focused: bool):
        if focused:
            self.frame.setStyleSheet("background: black; border: 3px solid #66aaff;")
        else:
            self.frame.setStyleSheet("background: black; border: 3px solid #333;")

    def _bind_player_window(self, player: vlc.MediaPlayer):
        bind_player_to_window(player, int(self.frame.winId()))

    def _detach_player_window(self):
        """Tell libVLC to stop drawing into our QFrame, but keep the player
        alive.  Used during grid rebuilds, where the QFrame's native window
        handle is about to be invalidated by reparenting."""
        if self.player is None:
            return
        try:
            bind_player_to_window(self.player, 0)
        except Exception:
            log.exception("pane %d: detach window failed", self.pane_id)

    def rebind_to_current_frame(self):
        """Re-attach the live player to the (possibly fresh) QFrame winId
        after a grid rebuild.  Cheap: no reconnect, no media reload."""
        if self.player is None:
            return
        try:
            self._bind_player_window(self.player)
        except Exception:
            pass

    # ---- event wiring ----

    # Map vlc.EventType -> short name string carried in the Qt signal.
    _EVENT_MAP = {
        vlc.EventType.MediaPlayerOpening:          "opening",
        vlc.EventType.MediaPlayerBuffering:        "buffering",
        vlc.EventType.MediaPlayerPlaying:          "playing",
        vlc.EventType.MediaPlayerPaused:           "paused",
        vlc.EventType.MediaPlayerStopped:          "stopped",
        vlc.EventType.MediaPlayerEndReached:       "ended",
        vlc.EventType.MediaPlayerEncounteredError: "error",
        vlc.EventType.MediaPlayerTimeChanged:      "time",
    }

    def _wire_player_events(self, player, gen):
        em = player.event_manager()
        for ev_type, name in self._EVENT_MAP.items():
            # Default args bake the current name + gen into the closure so the
            # callback can be called from any thread without surprises.
            def _cb(_event, _name=name, _gen=gen):
                try:
                    self.state_event.emit(_name, _gen)
                except Exception:
                    pass
            try:
                em.event_attach(ev_type, _cb)
            except Exception:
                log.exception("pane %d: event_attach failed for %s", self.pane_id, name)

    def _new_player(self) -> vlc.MediaPlayer:
        p = self.vlc.media_player_new()
        self._bind_player_window(p)
        self._player_gen += 1
        self._wire_player_events(p, self._player_gen)
        # New player -> fresh stats counters, so deltas don't go negative.
        self._time_event_count = 0
        self._last_time_event_count = 0
        self._last_decoded_video = 0
        self._last_read_bytes = 0
        self._last_lost_pictures = 0
        self._last_stats_ts = 0.0
        self._last_bitrate_bps = 0.0
        self._last_fps = 0.0
        self._last_lost = 0
        return p

    def _ensure_player(self):
        if self.player is None:
            self.player = self._new_player()

    def _swap_player(self) -> vlc.MediaPlayer:
        old = self.player
        self.player = self._new_player()
        if old is not None:
            dispose_player_async(old)
            profiler.count("player_swap")
        return self.player

    def _cancel_retry(self):
        self._retry_pending = False
        if self.retry_timer.isActive():
            self.retry_timer.stop()

    def stop_to_idle(self, keep_channel=False):
        self._cancel_retry()
        if self.open_timer.isActive():
            self.open_timer.stop()
        if self.watchdog_timer.isActive():
            self.watchdog_timer.stop()
        if self.player is not None:
            dispose_player_async(self.player)
        self.player = self._new_player()
        self._retry_attempt = 0
        self._opening_since_ts = 0.0
        self._last_progress_ts = 0.0
        self._is_playing = False
        if keep_channel and self.assigned_channel is not None:
            self.label.setText(
                f"Pane {self.pane_id}: {self.cfg.channel_text(self.assigned_channel)} (hidden)")
        else:
            self.assigned_channel = None
            self.label.setText(f"Pane {self.pane_id}: Idle")

    def play_channel(self, ch: int):
        ch = int(max(1, min(16, ch)))

        if not self.cfg.is_channel_active(ch):
            self.stop_to_idle()
            return

        # Idempotent: already playing this exact channel and the player is in
        # a healthy state -> no-op.  Avoids the wasteful "kill the player and
        # start over" cycle on every double-press of a channel button.
        if self.assigned_channel == ch and self.player is not None and self._is_playing:
            return

        self._ensure_player()
        self._cancel_retry()
        self._retry_attempt = 0

        self.assigned_channel = ch
        now = time.monotonic()
        self._opening_since_ts = now
        self._last_progress_ts = 0.0
        self._is_playing = False

        self._start_play(ch, reason="play")
        if not self.watchdog_timer.isActive():
            self.watchdog_timer.start()

    def _start_play(self, ch: int, reason: str):
        with profiler.time("start_play"):
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
                log.exception("pane %d: MediaPlayer.play() raised", self.pane_id)

            # Stash the start timestamp so the "playing" event handler can
            # record time-to-first-frame.
            self._play_started_ts = time.perf_counter()
            profiler.count("start_play.calls")
            if reason.startswith("retry"):
                profiler.count("start_play.retries")

            log.info("pane %d: opening CH%d (%s) url=%s",
                     self.pane_id, ch, reason, url)
            self.label.setText(f"Pane {self.pane_id}: Opening {self.cfg.channel_text(ch)} ({reason})")
            self.open_timer.start(self.cfg.open_timeout_ms)

    def _schedule_retry(self, why: str):
        if self._retry_pending or self.assigned_channel is None:
            return
        self._retry_pending = True
        self._retry_attempt += 1
        profiler.count("retry_scheduled")
        profiler.count(f"retry_reason.{why.split('(')[0].strip()}")

        delay = min(self.cfg.retry_max_ms, self.cfg.retry_base_ms * (2 ** max(0, self._retry_attempt - 1)))
        ch = self.assigned_channel
        self.label.setText(f"Pane {self.pane_id}: {self.cfg.channel_text(ch)} lost ({why}), retry in {delay}ms")
        log.warning("pane %d: CH%d retry#%d in %dms (%s)",
                    self.pane_id, ch, self._retry_attempt, delay, why)
        self.retry_timer.start(delay)

    def _retry_now(self):
        self._retry_pending = False
        if self.assigned_channel is None:
            return
        ch = self.assigned_channel
        self._opening_since_ts = time.monotonic()
        self._start_play(ch, reason=f"retry#{self._retry_attempt}")

    def _on_state_event(self, name: str, gen: int):
        """Slot — runs on the GUI thread.  libVLC event callbacks emit
        state_event from libVLC threads; queued connection delivers it here."""
        profiler.count(f"state_event.{name}")
        # Stale event from a swapped-out / disposed player — ignore.
        if gen != self._player_gen or self.assigned_channel is None:
            return

        ch = self.assigned_channel
        now = time.monotonic()

        if name == "playing":
            # Time-to-first-frame: elapsed from _start_play to "playing".
            if self._play_started_ts > 0:
                ttff_ms = (time.perf_counter() - self._play_started_ts) * 1000.0
                profiler.record("time_to_first_frame_ms", ttff_ms)
                self._play_started_ts = 0.0
            self._is_playing = True
            self._last_progress_ts = now
            self._opening_since_ts = 0.0
            self._retry_attempt = 0
            if self.open_timer.isActive():
                self.open_timer.stop()
            self.label.setText(f"Pane {self.pane_id}: {self.cfg.channel_text(ch)} playing")
            return

        if name == "time":
            # Forward-progress heartbeat — fires whenever the player advances.
            self._last_progress_ts = now
            self._time_event_count += 1
            # Time events implicitly mean the player is playing.  Treat as
            # a safety net in case a transient paused/stopped event
            # cleared the flag and no explicit "playing" followed.
            if not self._is_playing:
                self._is_playing = True
            return

        if name in ("opening", "buffering"):
            self.label.setText(f"Pane {self.pane_id}: {self.cfg.channel_text(ch)} {name}")
            return

        if name in ("error", "ended", "stopped"):
            self._is_playing = False
            self._schedule_retry(f"event={name}")
            return

        # paused: just track the flag, no action
        if name == "paused":
            self._is_playing = False

    def _watchdog_tick(self):
        """Catches the silent-stall case: libVLC isn't firing time events
        even though we believed it was Playing.  All other state transitions
        come from libVLC events, not from this timer."""
        if self.player is None or self.assigned_channel is None:
            return
        if not self._is_playing:
            return
        if self._last_progress_ts <= 0.0:
            return
        now = time.monotonic()
        if (now - self._last_progress_ts) * 1000.0 > self.cfg.stall_timeout_ms:
            self._is_playing = False
            self._schedule_retry("watchdog(no progress)")

    def _open_timeout(self):
        if self.player is None or self.assigned_channel is None:
            return
        if not self._is_playing:
            self._schedule_retry("open-timeout")

    @staticmethod
    def _format_rate(bps: float) -> str:
        if bps >= 1_000_000:
            return f"{bps / 1_000_000:.1f} Mbps"
        if bps >= 1_000:
            return f"{bps / 1_000:.0f} kbps"
        return f"{int(bps)} bps"

    def _update_stats(self):
        """Once-per-second sample.

        FPS comes from counting libVLC "time" events — fires on every PTS
        update (~1 per displayed frame) and is reliable on all builds.
        Bitrate + lost frames come from MediaStats.  If the counter is
        broken on this libVLC build, the overlay still shows FPS and a
        placeholder for bitrate."""
        with profiler.time("update_stats_pane"):
            self._update_stats_impl()

    def _update_stats_impl(self):
        if self.player is None or self.assigned_channel is None:
            self.fps_label.hide()
            self._last_bitrate_bps = 0.0
            self._last_fps = 0.0
            self._last_lost = 0
            return

        now = time.monotonic()
        tec = self._time_event_count

        # FPS from time events — one per displayed frame PTS update.
        fps = self._last_fps
        if self._last_stats_ts > 0.0:
            dt = now - self._last_stats_ts
            if dt > 0:
                dtec = max(0, tec - self._last_time_event_count)
                fps = dtec / dt

        # Bitrate + lost from MediaStats (best-effort).
        bps = self._last_bitrate_bps
        dl = 0

        media = self.player.get_media()
        if media is not None:
            stats = vlc.MediaStats()
            try:
                ok = media.get_stats(stats)
            except Exception:
                ok = False
            if ok:
                rbytes = int(stats.read_bytes)
                lost   = int(stats.lost_pictures)
                bps_from_vlc = float(stats.input_bitrate) * 8_000_000.0

                if self._last_stats_ts > 0.0:
                    dt = now - self._last_stats_ts
                    if dt > 0:
                        dl_raw = lost - self._last_lost_pictures
                        dl = dl_raw if 0 <= dl_raw < 1_000_000 else 0

                        if bps_from_vlc > 0:
                            bps = bps_from_vlc
                        else:
                            db_raw = rbytes - self._last_read_bytes
                            if 0 <= db_raw < (1 << 30):
                                bps = db_raw * 8.0 / dt
                            # else leave bps at previous value (wrap)

                if log.isEnabledFor(logging.DEBUG):
                    log.debug(
                        "pane %d stats: decoded=%d read_bytes=%d lost=%d "
                        "input_bitrate=%.6f -> fps(evt)=%.1f bps=%.0f",
                        self.pane_id, int(stats.decoded_video), rbytes, lost,
                        float(stats.input_bitrate), fps, bps,
                    )

                self._last_read_bytes = rbytes
                self._last_lost_pictures = lost
                self._last_decoded_video = int(stats.decoded_video)

        self._last_fps = fps
        self._last_bitrate_bps = bps
        self._last_lost = dl
        self._last_time_event_count = tec
        self._last_stats_ts = now

        # Always render once we have a playing player + assigned channel.
        # Show dashes for values that are still zero so the user can see
        # the overlay came up, and which field is missing.
        fps_txt = f"{fps:>4.0f} fps" if fps > 0 else "  -- fps"
        bps_txt = self._format_rate(bps) if bps > 0 else " -- bps"
        if dl > 0:
            text = f"{fps_txt}\n{bps_txt}\nlost {dl}"
        else:
            text = f"{fps_txt}\n{bps_txt}"
        self.fps_label.setText(text)
        self.fps_label.adjustSize()
        if not self.fps_label.isVisible():
            self.fps_label.show()
            self.fps_label.raise_()

    def shutdown(self):
        self._cancel_retry()
        if self.open_timer.isActive():
            self.open_timer.stop()
        if self.watchdog_timer.isActive():
            self.watchdog_timer.stop()
        if self.stats_timer.isActive():
            self.stats_timer.stop()
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

        self._app_icon = _load_app_icon()
        if self._app_icon:
            self.setWindowIcon(self._app_icon)

        root = QWidget(self)
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(8)

        self.btn_channels = QButtonGroup(self)
        self.btn_channels.setExclusive(False)
        # Wrap the channel-buttons grid in a QWidget so fullscreen mode can
        # hide the whole strip with a single setVisible(False).
        self._btn_panel = QWidget(self)
        grid_btn = QGridLayout(self._btn_panel)
        grid_btn.setContentsMargins(0, 0, 0, 0)
        grid_btn.setSpacing(6)
        for i, ch in enumerate(range(1, 17)):
            lbl = self.cfg.label_for(ch)
            text = f"{ch}\n{lbl}" if lbl else str(ch)
            b = QPushButton(text, self._btn_panel)
            b.setMinimumHeight(46)
            b.setMinimumWidth(110)
            if not self.cfg.is_channel_active(ch):
                b.setEnabled(False)
                b.setToolTip(f"CH{ch}: no camera")
            self.btn_channels.addButton(b, ch)
            grid_btn.addWidget(b, i // 8, i % 8)
        for c in range(8):
            grid_btn.setColumnStretch(c, 1)
        main.addWidget(self._btn_panel)

        # Controls container (same reason).
        self._controls_panel = QWidget(self)
        controls = QHBoxLayout(self._controls_panel)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(10)

        controls.addWidget(QLabel("Streams:", self))
        self.cmb_panes = QComboBox(self)
        self.cmb_panes.addItems([str(i) for i in range(1, 17)])
        controls.addWidget(self.cmb_panes)

        # Layout orientation toggle.  Most useful when n=2 (side-by-side vs
        # stacked) but applies globally: "horizontal" prefers wide grids,
        # "vertical" prefers tall ones.  Square layouts are unchanged.
        self.btn_orient = QPushButton("\u2194", self)
        self.btn_orient.setFixedWidth(40)
        controls.addWidget(self.btn_orient)

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

        main.addWidget(self._controls_panel)

        self.info = QLabel("Active pane: 1", self)
        self.info.setStyleSheet("color: #ddd;")
        main.addWidget(self.info)

        # Aggregate stats bar refresh — each PlayerPane samples its own
        # stats every 1s; this timer just re-renders the total 1s later.
        self._agg_stats_timer = QTimer(self)
        self._agg_stats_timer.setInterval(1000)
        self._agg_stats_timer.timeout.connect(self._update_aggregate_stats)
        self._agg_stats_timer.start()

        # Profiler plumbing: a periodic dump timer + a tight event-loop
        # tick that measures scheduling jitter (if the jitter spikes,
        # something is blocking the GUI thread).  Both short-circuit on
        # profiler.enabled is False, so zero overhead otherwise.
        if profiler.enabled:
            self._profile_dump_timer = QTimer(self)
            self._profile_dump_timer.setInterval(
                self.cfg.profile_dump_interval_s * 1000)
            self._profile_dump_timer.timeout.connect(self._profile_dump_tick)
            self._profile_dump_timer.start()

            self._profile_jitter_last = time.perf_counter()
            self._profile_jitter_timer = QTimer(self)
            self._profile_jitter_timer.setInterval(100)  # 10 Hz scheduling probe
            self._profile_jitter_timer.timeout.connect(self._profile_jitter_tick)
            self._profile_jitter_timer.start()

        self.panes_grid = QGridLayout()
        self.panes_grid.setSpacing(8)
        main.addLayout(self.panes_grid, 1)

        self.panes = [PlayerPane(pid, self.vlc, self.cfg,
                                 self.set_active_pane,
                                 on_dblclick=self.open_fullscreen_for_pane)
                      for pid in range(1, 17)]
        self.grid_rows = 0
        self.grid_cols = 0

        # Fullscreen is done in-place: the main window goes fullscreen, the
        # toolbar/controls hide, every other pane hides, and row/col
        # stretches are rebalanced so the chosen pane's cell takes all
        # available space.  No reparenting, no winId churn, no second RTSP
        # session — the pane's live player just gets a bigger surface and
        # libVLC scales the video to fit (aspect ratio preserved by default).
        self._fullscreen_active = False
        self._fs_pane_id = None
        self._fs_saved_row_stretches = []
        self._fs_saved_col_stretches = []
        self._fs_was_maximized = False

        self.active_pane = 1
        # Start at 0 so the first _apply_panes_visibility call treats every
        # visible pane as "newly revealed" and (re)plays its saved channel.
        self.visible_panes = 0
        self.split_orientation = "horizontal"

        self._reload_views_combo()

        self.btn_channels.buttonClicked[int].connect(self.on_channel_pressed)
        self.cmb_panes.currentTextChanged.connect(self._on_panes_changed)
        self.btn_orient.clicked.connect(self.toggle_orientation)
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
        self._update_orient_button()
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
        return {
            "panes": self.visible_panes,
            "active_pane": self.active_pane,
            "split_orientation": self.split_orientation,
            "assign": assign[:16],
        }

    def _restore_state(self):
        st = safe_read_json(self.cfg.state_file, None)
        if not isinstance(st, dict):
            return
        panes = st.get("panes")
        active = st.get("active_pane")
        assign = st.get("assign")
        orient = st.get("split_orientation")
        if isinstance(panes, int):
            self.visible_panes = max(1, min(16, panes))
        if isinstance(active, int):
            self.active_pane = max(1, min(16, active))
        if orient in ("horizontal", "vertical"):
            self.split_orientation = orient
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
        self._update_aggregate_stats()

    def _update_aggregate_stats(self):
        """Assemble the status line: active pane + how many of the visible
        tiles are actually playing + total network bitrate + lost frames."""
        visible = self.panes[:max(0, self.visible_panes)]
        playing = sum(1 for p in visible
                      if p._is_playing and p.assigned_channel is not None)
        total_bps = sum(p._last_bitrate_bps for p in visible)
        total_lost = sum(p._last_lost for p in visible)

        if total_bps >= 1_000_000:
            rate = f"{total_bps / 1_000_000:.1f} Mbps"
        elif total_bps >= 1_000:
            rate = f"{total_bps / 1_000:.0f} kbps"
        else:
            rate = "0 bps"

        pieces = [
            f"Active pane: {self.active_pane}",
            f"streams: {playing}/{len(visible)}",
            f"total: {rate}",
        ]
        if total_lost > 0:
            pieces.append(f"lost: {total_lost}")
        self.info.setText("  |  ".join(pieces))

        # Feed gauges to the profiler (cheap no-op when disabled).
        profiler.gauge("visible_panes", len(visible))
        profiler.gauge("playing_panes", playing)
        profiler.gauge("total_bps", int(total_bps))
        profiler.gauge("total_lost_per_sec", total_lost)

    def _profile_dump_tick(self):
        # Sample per-pane state as gauges right before we dump, so the
        # summary shows the current live values.
        alive_players = sum(1 for p in self.panes if p.player is not None)
        profiler.gauge("alive_players", alive_players)
        profiler.gauge("fullscreen_active", int(self._fullscreen_active))
        profiler.gauge("active_pane", self.active_pane)
        profiler.dump()

    def _profile_jitter_tick(self):
        now = time.perf_counter()
        expected_dt = self._profile_jitter_timer.interval() / 1000.0
        actual_dt = now - self._profile_jitter_last
        jitter_ms = (actual_dt - expected_dt) * 1000.0
        # Only record positive jitter (late fires); early fires are a Qt
        # scheduling artefact not caused by blocking.
        if jitter_ms > 0:
            profiler.record("gui_loop_jitter_ms", jitter_ms)
        self._profile_jitter_last = now

    def set_active_pane(self, pane_id: int):
        # In fullscreen, a click on the sole visible tile is "exit",
        # matching the any-key-exits convention.
        if self._fullscreen_active:
            QTimer.singleShot(0, self._exit_fullscreen)
            return
        if pane_id > self.visible_panes:
            pane_id = 1
        if self.active_pane == pane_id:
            return
        self.active_pane = pane_id
        self._update_focus()
        self._save_state()

    # ---------- fullscreen ----------

    def open_fullscreen_for_pane(self, pane_id: int):
        """Double-click handler.  Makes the ALREADY-PLAYING tile take over
        the whole screen: main window goes fullscreen, toolbar/controls hide,
        every other pane hides, and row/col stretches are adjusted so the
        chosen pane's cell gets all the space.

        The pane's live MediaPlayer is never touched — libVLC resizes its
        video output to fit the new frame size, preserving aspect ratio.
        No RTSP reconnect, no winId change, no black flicker."""
        if self._fullscreen_active:
            return
        if not (1 <= pane_id <= self.visible_panes):
            return
        pane = self.panes[pane_id - 1]
        if pane.assigned_channel is None:
            return

        log.info("user: fullscreen pane %d (CH%d)", pane_id, pane.assigned_channel)

        # Where is the target pane in the current grid?  _rebuild_grid puts
        # pane i at (i // cols, i % cols).
        target_idx = pane_id - 1
        cols = max(1, self.grid_cols)
        target_r = target_idx // cols
        target_c = target_idx % cols

        # Snapshot the stretches we'll restore on exit.
        self._fs_saved_row_stretches = [
            self.panes_grid.rowStretch(r) for r in range(max(1, self.grid_rows))
        ]
        self._fs_saved_col_stretches = [
            self.panes_grid.columnStretch(c) for c in range(cols)
        ]
        self._fs_was_maximized = self.isMaximized()
        self._fs_pane_id = pane_id
        self._fullscreen_active = True

        # Hide every other visible pane.  Hidden widgets in a QGridLayout
        # do not consume space by default, so the remaining cell expands.
        for i in range(self.visible_panes):
            if i != target_idx:
                self.panes[i].setVisible(False)

        # Hide toolbar / controls / info.
        self._btn_panel.setVisible(False)
        self._controls_panel.setVisible(False)
        self.info.setVisible(False)

        # Drive all the stretch onto the target cell so the remaining pane
        # fills the entire grid area.
        for r in range(max(1, self.grid_rows)):
            self.panes_grid.setRowStretch(r, 1 if r == target_r else 0)
        for c in range(cols):
            self.panes_grid.setColumnStretch(c, 1 if c == target_c else 0)

        self.showFullScreen()
        # Main window needs focus so keyPressEvent is delivered here, not
        # to a pushbutton or combobox that still has it.
        self.setFocus()

    def _exit_fullscreen(self):
        if not self._fullscreen_active:
            return
        log.info("user: exit fullscreen (pane %d)", self._fs_pane_id)
        self._fullscreen_active = False
        self._fs_pane_id = None

        # Restore toolbar / controls / info.
        self._btn_panel.setVisible(True)
        self._controls_panel.setVisible(True)
        self.info.setVisible(True)

        # Re-show every pane that's part of the current layout.
        for i in range(self.visible_panes):
            self.panes[i].setVisible(True)

        # Restore stretches.
        cols = max(1, self.grid_cols)
        rows = max(1, self.grid_rows)
        for r in range(rows):
            v = self._fs_saved_row_stretches[r] if r < len(self._fs_saved_row_stretches) else 1
            self.panes_grid.setRowStretch(r, v)
        for c in range(cols):
            v = self._fs_saved_col_stretches[c] if c < len(self._fs_saved_col_stretches) else 1
            self.panes_grid.setColumnStretch(c, v)

        # Exit fullscreen.  showNormal or showMaximized depending on prior
        # state so the user doesn't lose their maximized window.
        if self._fs_was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _close_fullscreen_if_any(self):
        if self._fullscreen_active:
            self._exit_fullscreen()

    def keyPressEvent(self, event):
        # Any key exits fullscreen per user spec.
        if self._fullscreen_active:
            event.accept()
            QTimer.singleShot(0, self._exit_fullscreen)
            return
        super().keyPressEvent(event)

    def _grid_dims(self, n: int):
        """Pick (rows, cols) for n tiles, honouring self.split_orientation.

        - "horizontal": prefer wide layouts (more cols than rows).
          n=2 -> (1, 2), n=3 -> (1, 3), n=8 -> (2, 4), n=16 -> (4, 4).
        - "vertical": prefer tall layouts (more rows than cols).
          n=2 -> (2, 1), n=3 -> (3, 1), n=8 -> (4, 2), n=16 -> (4, 4).
        """
        n = max(1, min(16, int(n)))
        orient = getattr(self, "split_orientation", "horizontal")
        if orient == "vertical":
            cols = max(1, int(math.floor(math.sqrt(n))))
            rows = int(math.ceil(n / cols))
        else:
            rows = max(1, int(math.floor(math.sqrt(n))))
            cols = int(math.ceil(n / rows))
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
        with profiler.time("rebuild_grid"):
            self._rebuild_grid_impl()

    def _rebuild_grid_impl(self):
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
        with profiler.time("apply_panes_visibility"):
            self._apply_panes_visibility_impl(n)

    def _apply_panes_visibility_impl(self, n: int):
        n = max(1, min(16, int(n)))
        prev = self.visible_panes
        self.visible_panes = n

        # Layout reshuffling and a live fullscreen rebind don't mix — close
        # any open fullscreen first so the tile rebinds happen on a clean
        # slate.
        self._close_fullscreen_if_any()

        # Stop streams that just disappeared (saves bandwidth + decode CPU)
        # but keep assigned_channel so switching back restores them.
        for i in range(n, prev):
            self.panes[i].stop_to_idle(keep_channel=True)

        self._rebuild_grid()

        # (Re)start saved channels for panes that just became visible — the
        # previous behaviour silently left them stuck in "(saved)".
        batch = []
        for i in range(prev, n):
            ch = self.panes[i].assigned_channel
            if isinstance(ch, int) and self.cfg.is_channel_active(ch):
                batch.append((i + 1, ch))
        if batch:
            self._play_batch_staggered(batch)

        if self.active_pane > n:
            self.active_pane = 1
        self._update_focus()

    def _update_orient_button(self):
        if self.split_orientation == "horizontal":
            self.btn_orient.setText("\u2194")  # ↔
            self.btn_orient.setToolTip("Layout: side-by-side  (click to stack)")
        else:
            self.btn_orient.setText("\u2195")  # ↕
            self.btn_orient.setToolTip("Layout: stacked  (click for side-by-side)")

    def toggle_orientation(self):
        self._close_fullscreen_if_any()
        self.split_orientation = (
            "vertical" if self.split_orientation == "horizontal" else "horizontal"
        )
        self._update_orient_button()
        self._rebuild_grid()
        self._update_focus()
        self._save_state()

    def _on_panes_changed(self, txt: str):
        try:
            n = int(txt)
        except Exception:
            return
        self._apply_panes_visibility(n)
        self._save_state()

    def _play_batch_staggered(self, items):
        """Open a batch of channels sequentially with a small delay between
        each so the DVR + network aren't hammered with N concurrent RTSP
        handshakes.  `items` is a list of (pane_id, channel) pairs.

        The pane's assigned_channel is set immediately (state is coherent)
        so if the user reassigns a pane before its slot fires, we detect
        the mismatch and skip the stale open."""
        step = max(0, int(self.cfg.stagger_open_ms))

        for idx, (pane_id, ch) in enumerate(items):
            pane = self.panes[pane_id - 1]
            pane.assigned_channel = ch

            if step == 0:
                pane.play_channel(ch)
                continue

            if idx == 0:
                pane.play_channel(ch)
                continue

            delay = idx * step
            pane.label.setText(
                f"Pane {pane_id}: {self.cfg.channel_text(ch)} (queued, +{delay}ms)")

            def _go(pid=pane_id, target_ch=ch):
                p = self.panes[pid - 1]
                # Skip if the user reassigned this pane in the meantime.
                if p.assigned_channel != target_ch:
                    return
                p.play_channel(target_ch)
            QTimer.singleShot(delay, _go)

    def on_channel_pressed(self, ch: int):
        log.info("user: assign CH%d -> pane %d", ch, self.active_pane)
        self.panes[self.active_pane - 1].play_channel(ch)
        self._save_state()

    def clear_active_pane(self):
        self.panes[self.active_pane - 1].stop_to_idle()
        self._save_state()

    def apply_selected_view(self):
        name = self.cmb_views.currentText().strip()
        if not name:
            return
        v = self.views.get(name)
        if not isinstance(v, dict):
            return
        log.info("user: apply view %r", name)
        panes = int(v.get("panes", 4))
        assign = v.get("assign", [])

        # Pre-set every pane's assigned_channel and idle the ones the view
        # leaves empty.  _apply_panes_visibility will then read those
        # assignments through its own staggered-open path.
        for i in range(1, 17):
            p = self.panes[i - 1]
            ch = assign[i - 1] if i - 1 < len(assign) else None
            if isinstance(ch, int) and 1 <= ch <= 16 and i <= panes:
                p.assigned_channel = ch
            elif i > panes:
                # out of range for the new pane count; keep state but skip
                pass
            else:
                p.stop_to_idle()

        self.cmb_panes.setCurrentText(str(panes))
        # When the pane count is the same as before, _apply_panes_visibility
        # does NOT iterate the "newly revealed" range.  Force a re-open of
        # visible panes in that case by going through the stagger helper.
        if self.visible_panes == panes:
            batch = [(i, self.panes[i - 1].assigned_channel)
                     for i in range(1, panes + 1)
                     if isinstance(self.panes[i - 1].assigned_channel, int)
                     and self.cfg.is_channel_active(self.panes[i - 1].assigned_channel)]
            if batch:
                self._play_batch_staggered(batch)
        else:
            self._apply_panes_visibility(panes)

        self._save_state()

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
        log.info("shutting down")
        self._close_fullscreen_if_any()
        self._save_state()
        for p in self.panes:
            p.shutdown()
        # Wait for in-flight stop()/release() workers before tearing down the
        # VLC instance.  Otherwise libVLC frees state out from under them.
        join_disposal_threads(timeout_total=3.0)
        try:
            self.vlc.release()
        except Exception:
            log.exception("vlc.Instance.release() raised")
        log.info("shutdown complete")

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
    # Configure logging from rtsp.ini *before* anything else so early
    # failures (missing libVLC, broken config) end up in the log too.
    try:
        _early_cfg = RtspConfig("rtsp.ini")
        setup_logging(_early_cfg.log_level, _early_cfg.log_file)
        if _early_cfg.profile:
            profiler.enable()
    except Exception:
        setup_logging("INFO", "")
        log.exception("Failed to read rtsp.ini for early logging setup")
    log.info("RTSPMatrix starting")

    app = QApplication(sys.argv)
    _icon = _load_app_icon()
    if _icon:
        app.setWindowIcon(_icon)
    w = MainWindow()
    app.aboutToQuit.connect(w.cleanup)
    _tick = install_sigint_handler(w)
    w.show()
    sys.exit(app.exec_())