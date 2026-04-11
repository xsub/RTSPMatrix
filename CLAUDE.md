# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A PyQt5 + libVLC RTSP grid viewer for Dahua / compatible DVR/NVR devices. Targets the
"open RTSP now, no vendor bloat" use case where Dahua SmartPSS Lite is unreliable.
Single-host: all 16 channels come from one DVR via `channel=N` in the RTSP URL.

## Running

There is no build step, no tests, no linter. The code is run directly:

```bash
source venv/bin/activate
python rtspmatrix.py            # classic grid viewer
python rtspmatrix-vitual.py     # virtual / scrolling viewer (a.k.a. v3)
```

`rtspmatrix.py.orig`, `rtspmatrix.py.rej`, `rtspmatrix-vitual-try.py`, `*.diff`, `patch.txt`,
`last-working-checkout.sh` are scratch / backup files from manual patching sessions — do
not treat them as canonical sources. The two live entry points are `rtspmatrix.py` and
`rtspmatrix-vitual.py` (`rtspmatrix-v3.py` is an almost-identical sibling of the virtual
variant; see below).

## Two viewer variants — important architectural distinction

Both variants read the same `rtsp.ini`, `views.json`, `state.json` and share the same
URL scheme `rtsp://host:port/cam/realmonitor?channel=N&subtype=S`. Beyond that the
player-lifecycle models are completely different and should not be confused:

### `rtspmatrix.py` — classic "one player per pane"

- Each `PlayerPane` owns a single `vlc.MediaPlayer` bound to its own `QFrame`.
- Selecting a channel button calls `play_channel()` on the *active* pane, which
  swaps in a brand-new `MediaPlayer` (the old one is disposed asynchronously on a
  background thread via `dispose_player_async`). New player on every (re)open is
  intentional: it escapes broken libVLC / live555 internal state.
- Resilience is per-pane: a `poll_timer` samples `MediaPlayer.get_state()` and an
  `open_timer` enforces `open_timeout_ms`. On stall / error / open-timeout
  `_schedule_retry` arms `retry_timer` with exponential backoff
  (`retry_base_ms` × 2^attempt, capped at `retry_max_ms`). Each retry calls
  `_swap_player()` again — never reuses the failing instance.
- Switching channels = full reconnect.

### `rtspmatrix-vitual.py` (and the near-duplicate `rtspmatrix-v3.py`) — "virtual matrix" with player pool

Core invariant: **scrolling the visible window must NOT reconnect kept channels.**
Switching is implemented as a drawable rebind, not a player swap.

- `PlayerPool` is a `Dict[int, ChanPlayer]` keyed by channel number. Each
  `ChanPlayer` owns: a `vlc.MediaPlayer`, a 1×1 hidden `HiddenSink` `QFrame` (its
  parking spot when not visible), and the OS window handle it is currently bound to.
- `pool.ensure(desired_channels)` is the only mutation point: it releases any
  pool entry not in `desired`, and creates+starts a new player on its `HiddenSink`
  for any new channel. Kept channels are untouched (no reconnect).
- `_apply_window()` computes:
  - `desired = list(active_list)` — *all* active channels stay connected at all
    times (the historical sliding-window-with-buffer approach was abandoned because
    on slow networks the buffered channel hadn't finished connecting by the time
    the user scrolled to it; comment in `_desired_channels` documents this).
  - `visible = active_list[window_start : window_start+visible_panes]` — for each
    visible tile it calls `pool.bind_to(ch, tile.frame.winId())`, which is just
    `MediaPlayer.set_xwindow / set_hwnd / set_nsobject`. No reconnect.
  - All other (`desired - visible`) channels are reparked on their hidden sink via
    `pool.bind_hidden(ch)`.
- Fullscreen (`FullScreenWindow`): clicking a tile rebinds that channel's player
  to the fullscreen window's video drawable. The `fullscreen_channel` is locked
  out of `_apply_window`'s tile-rebinding so it doesn't get yanked back. Left/Right
  in fullscreen step through `active_list` and rebind in place — same player,
  different drawable. `_bind_channel_to_fullscreen` has a guard
  (`if self.fullscreen is None: return`) because the bind is deferred via
  `QTimer.singleShot(0, ...)` and the window may have already closed by the time
  the tick fires.
- Channel-button right-click toggles a channel into `excluded`, which is stripped
  from `active_list`; this releases the pool entry immediately.
- `Tile`, `HiddenSink`, and `FullScreenWindow.video` all set
  `Qt.WA_NativeWindow`. This is **load-bearing**: without it, `winId()` returns
  the toplevel window's handle and every player renders on top of every other.
- `dispose_player_async` in this file (unlike the classic variant) first detaches
  the OS window handle (`set_xwindow(0)` / `set_hwnd(0)` / `set_nsobject(0)`)
  *synchronously on the GUI thread* before the background stop/release. Comment
  explains: otherwise libVLC's render thread may write into a freed HWND/XWindow
  → use-after-free crash.

`rtspmatrix-v3.py` differs from `rtspmatrix-vitual.py` only in the icon-extraction
constants (`ARTLIST_COMPOSITE_HINT`, fallback search loop) — they are otherwise
the same code. If you change one, check whether the other should follow.

## Configuration files

- **`rtsp.ini`** (required, real credentials, **not** in git — see `.gitignore`).
  `rtsp-example.ini` is the template. Both variants parse this with
  `RawConfigParser(inline_comment_prefixes=())` (no inline `#` comments allowed).
  Sections: `[rtsp]` (host/port/user/password/path/subtype/tcp + tuning),
  `[app]` (title/default_panes/views_file/state_file).
  Resilience knobs (`poll_interval_ms`, `stall_timeout_ms`, `retry_base_ms`,
  `retry_max_ms`, `rtsp_timeout_s`, `disable_hw_decode`) are read by the classic
  variant; the virtual variant only uses `network_caching_ms` and `rtsp_timeout_s`.
- **`views.json`** — saved layouts. Schema differs between variants:
  - Classic: `{panes, assign[16]}`
  - Virtual: `{panes, virtual, active_channels, start, assign[16]}`
  A single `views.json` may contain entries written by either variant.
- **`state.json`** — last-session restore. Written on `closeEvent` / `aboutToQuit`
  / SIGINT, via `safe_write_json` (write-tmp-then-`os.replace`, so it's atomic).
  Schema is variant-specific in the same way as views.

## Platform notes (don't regress these)

- **macOS HW decode**: `disable_hw_decode=1` in `rtsp.ini` (default) passes
  `--avcodec-hw=none` to the VLC instance and `:avcodec-hw=none` per media. The
  classic variant's docstring calls out that VideoToolbox can deadlock on glitchy
  RTSP streams. Don't re-enable by default.
- **RTSP hardening** (both variants): `:rtsp-tcp` + `:rtsp-keepalive` +
  `:rtsp-timeout=N` are added to every media. Keepalive is what prevents the DVR
  from silently dropping idle sessions.
- **SIGINT**: both variants install a `signal.signal(SIGINT, ...)` that calls
  `QTimer.singleShot(0, window.close)`, plus a 200 ms no-op `QTimer` whose only
  purpose is to keep the Qt event loop returning to Python regularly so the
  signal handler actually gets a chance to run. Don't remove the dummy timer.
- **Player binding** is platform-switched on `sys.platform`: `set_xwindow` (linux
  / fallback), `set_hwnd` (win), `set_nsobject` (darwin). Any new code that
  attaches a player to a widget should go through the existing `_bind_player_window`
  / `bind_player` helper, not reinvent the platform check.

## Conventions worth preserving

- JSON state files are written via `safe_write_json` (tmp + `os.replace`); never
  open the target path directly for writing.
- Channel numbers are clamped to `[1, 16]` everywhere (`clamp_int` in the virtual
  variant, inline `max(1, min(16, ...))` in the classic). The DVR has 16 channels;
  this is a hard limit, not a magic number to be parameterised.
- New `MediaPlayer` on every retry / (re)assignment in the classic variant is
  deliberate — see the comment block at the top of `rtspmatrix.py`.
