#!/usr/bin/env sh
# Build the standalone app for this platform into dist/ (no Python needed by users).
set -eu
cd "$(dirname "$0")/.."
uv run --extra gui --extra auto --with pyinstaller --with pillow \
    pyinstaller --noconfirm --distpath dist --workpath build packaging/subtitle-tool.spec
