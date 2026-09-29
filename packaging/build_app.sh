#!/usr/bin/env sh
# Build a standalone "Subtitle Tool" app (no Python or uv needed by the user).
# Output: dist/Subtitle Tool.app on macOS, dist/Subtitle Tool/ on Windows/Linux.
# About 270 MB unpacked / 115 MB zipped, including Whisper's runtime.
set -eu
cd "$(dirname "$0")/.."
uv run --extra gui --extra auto --with pyinstaller pyinstaller --noconfirm --windowed \
    --name "Subtitle Tool" \
    --collect-data faster_whisper --collect-binaries ctranslate2 \
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtQuick \
    --exclude-module PySide6.QtQml --exclude-module tkinter \
    packaging/launch.py
