# RTSPMatrix

RTSPMatrix is a lightweight, cross-platform RTSP viewer for Dahua and compatible DVR/NVR devices, built with PyQt5 and libVLC. Fast, portable grid monitor (1–16 streams), instant channel-to-tile assignment, saved views, and timeout handling so missing channels don’t freeze the UI.

## Motivation

Dahua SmartPSS Lite is unreliable in practice (incompatibilities, inconsistent behavior across macOS/Linux/Windows, breakage after updates). There is also no good portable, minimal RTSP viewer that runs locally, starts fast, and does not hang the UI when a channel/stream is missing.

RTSPMatrix targets the “open RTSP now, no vendor bloat” use case.

## Features

- Configurable grid: **1–16** independent streams (tiles)
- **Channel buttons 1–16**
- Click a tile to focus it, press a channel button to assign that stream to the focused tile
- **Views**: save and load named layouts (grid size + channel assignments)
- **Timeout handling**: missing channels don’t freeze the GUI
- Credentials and RTSP parameters in a simple **INI** config
- **About** dialog: component/runtime info (OS, Python, Qt/PyQt, python-vlc, libVLC)
- Clean shutdown on window close and **Ctrl+C** in console (SIGINT)

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
