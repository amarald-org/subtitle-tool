#!/usr/bin/env sh
# Install subtitle-tool on macOS or Linux.
#   ./install.sh                     (from a clone)
#   curl -LsSf <raw url>/install.sh | sh
set -eu

REPO="git+https://github.com/aaro-cmd/subtitle-tool"

ask() {
    # Prompt on the terminal even when the script is piped into sh.
    printf "%s [y/N] " "$1"
    read -r answer </dev/tty || answer=""
    [ "$answer" = "y" ] || [ "$answer" = "Y" ]
}

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv (Python package manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg is needed to sync subtitles to the audio."
    if command -v brew >/dev/null 2>&1; then
        ask "Install it with Homebrew now?" && brew install ffmpeg
    elif command -v apt-get >/dev/null 2>&1; then
        ask "Install it with apt now (needs sudo)?" && sudo apt-get install -y ffmpeg
    elif command -v dnf >/dev/null 2>&1; then
        ask "Install it with dnf now (needs sudo)?" && sudo dnf install -y ffmpeg
    elif command -v pacman >/dev/null 2>&1; then
        ask "Install it with pacman now (needs sudo)?" && sudo pacman -S --noconfirm ffmpeg
    else
        echo "Please install ffmpeg with your package manager: https://ffmpeg.org/download.html"
    fi
fi

SRC="$REPO"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo "")
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    SRC="$SCRIPT_DIR"
fi

echo "Installing subtitle-tool from $SRC..."
uv tool install --force "$SRC"
uv tool update-shell >/dev/null 2>&1 || true

cat <<'MSG'

Done. Open a new terminal, then set your API keys (add these lines to ~/.zshrc or ~/.bashrc):

  export OPENSUBTITLES_API_KEY=...   # https://www.opensubtitles.com/consumers
  export DEEPL_API_KEY=...           # https://www.deepl.com/pro-api (free plan)

Try it:  subtitle-tool "/path/to/Movie.mkv" --translate --backend deepl
MSG
