# RTSPMatrix

RTSPMatrix is a lightweight, cross-platform RTSP viewer for Dahua and compatible DVR/NVR devices, built with PyQt5 and libVLC. Fast, portable grid monitor (1–16 streams), instant channel-to-tile assignment, saved views, and timeout handling so missing channels don’t freeze the UI.

## Motivation

Dahua SmartPSS Lite is unreliable in practice (incompatibilities, inconsistent behavior across macOS/Linux/Windows, breakage after updates). There is also no good portable, minimal RTSP viewer that runs locally, starts fast, and does not hang when a stream/channel is missing.

RTSPMatrix targets the “open RTSP now, no vendor bloat” use case.

## Features

- Configurable grid: **1–16** tiles
- **Channel buttons 1–16**
- Click a tile to focus it, press a channel button to assign a stream
- **Views**: save and load named layouts (grid size + assignments)
- **Timeout handling**: dead channels don’t freeze the GUI
- Credentials and RTSP parameters in a simple **INI** file
- **About** dialog (Help menu): runtime/component info (OS, Python, Qt/PyQt, python-vlc, libVLC)
- Clean shutdown on window close and **Ctrl+C** in console (SIGINT)

## RTSPMatrix-Virtual mode (fast scrolling)

Virtual mode is designed for low-latency “wall” navigation when you have more active channels than visible tiles.

Example: display **2×2** tiles while having **16 active channels**.
Internally it behaves like a virtual matrix **(columns × rows)**, e.g. **8×2** for 16 channels.

Key behavior:
- Only the currently visible tiles are decoded (e.g. 4 streams for 2×2).
- Pressing **Left/Right** scrolls by one **column**.
- The overlapping column keeps its existing player instance, so the stream continues without reload.
- Only the newly revealed column starts new streams.
This minimizes switching latency.

## Requirements

- Python 3.10+ (older may work)
- VLC installed (provides **libVLC**)
- Python packages:
  - `PyQt5`
  - `python-vlc`

## Install

```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install PyQt5 python-vlc
