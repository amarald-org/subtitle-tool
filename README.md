# subtitle-tool

Find English subtitles for a movie file you already have, sync them to the audio,
and optionally add a Finnish translation stacked under each English line for
language learning.

## Install

**macOS / Linux**

```sh
git clone https://github.com/aaro-cmd/subtitle-tool && cd subtitle-tool
./install.sh
```

**Windows** (PowerShell)

```powershell
git clone https://github.com/aaro-cmd/subtitle-tool; cd subtitle-tool
powershell -ExecutionPolicy ByPass -File .\install.ps1
```

The script installs [uv](https://docs.astral.sh/uv/) if missing, offers to install
`ffmpeg`, then runs `uv tool install`, which puts a `subtitle-tool` command on your PATH.

Without the script, if you already have uv and ffmpeg:

```sh
uv tool install git+https://github.com/aaro-cmd/subtitle-tool   # install the command
uv tool upgrade subtitle-tool                                  # update later
uvx --from git+https://github.com/aaro-cmd/subtitle-tool subtitle-tool movie.mkv   # run once, no install
```

`uvx` runs a tool in a throwaway environment; `uv tool install` keeps it installed;
`uv run subtitle-tool` only works inside a clone of this repo (useful while developing).

## API keys

Set these once. On macOS/Linux add the lines to `~/.zshrc` (or `~/.bashrc`); on Windows use
`setx NAME "value"` and open a new terminal.

```sh
# Required for searching subtitles. Free: https://www.opensubtitles.com/consumers
export OPENSUBTITLES_API_KEY=...
# Optional, raises the daily download limit:
export OPENSUBTITLES_USERNAME=...
export OPENSUBTITLES_PASSWORD=...

# For --backend deepl (best Finnish). Free plan: https://www.deepl.com/pro-api
export DEEPL_API_KEY=...
```

## Usage

```sh
# English subs, synced to the movie
subtitle-tool "$HOME/Movies/The Matrix (1999)/The.Matrix.1999.mkv"

# Plus Finnish translation and dual-language files
subtitle-tool "$HOME/Movies/The Matrix (1999)/The.Matrix.1999.mkv" --translate --backend deepl

# Another language: any code DeepL/Google supports
subtitle-tool movie.mkv --translate --backend deepl --target sv
```

### Automatic subtitles (Whisper)

For your own videos, YouTube downloads or songs with no subtitles online:

```sh
subtitle-tool video.webm --auto              # transcribe; language is detected
subtitle-tool video.webm --auto -t           # transcribe + translate to Finnish
subtitle-tool song.mp3 --auto -t --music     # tuned for singing
subtitle-tool video.webm --auto --model large-v3-turbo   # slower, most accurate
```

This uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your own computer
(no upload, no key). The model downloads once on first use: `small` (default) is 480 MB,
`large-v3-turbo` 1.6 GB. Timestamps come from the audio, so no sync step is needed.

If `movie.en.srt` or `movie.srt` already sits next to the video, it is used instead of
searching (pass `--search` to force a new download). `--subs file.srt` picks a specific file.
Syncing still runs by default; it is quick and leaves already-correct subtitles alone.
Use `--nosync` to skip it.

## Editor window

```sh
subtitle-tool gui "$HOME/Movies/The Matrix (1999)/The.Matrix.1999.mkv"
```

The video plays on the left; the right side lists every subtitle with its times, the original
line and the translation (in a different colour). The list follows playback, clicking a row
jumps there, and double-clicking a cell edits it (Enter saves, Shift+Enter adds a line).

| Button | What it does |
| --- | --- |
| ✨ Auto | Subtitles from the audio with Whisper, plus the translation |
| Search / Sync / Shift | Find subtitles online, align them to the audio, move them all |
| ⏺ Set start / ⏹ Set end | Write subtitles by hand: start a line at the current moment, type, then end it |
| ◀ Undo / Redo ▶ | Step back and forward through changes |
| Translate | Fill empty translation cells (clear a cell to re-translate it) |
| Save | Write the same files as the command line |
| Keys… | OpenSubtitles and DeepL keys, remembered by the app |

Shortcuts (⌘ on Mac, Ctrl elsewhere; plain Ctrl also works on Mac): Space play/pause,
S save, Z undo, Y redo, B set start, E set end, Delete removes selected rows.

Playback uses Qt's built-in player, which ships its own FFmpeg inside the Python package,
so nothing else needs installing. Installed through uv the Qt package is about 1 GB, because
PyPI ships Qt as one big bundle. `packaging/build_app.sh` builds a standalone app instead
(about 115 MB zipped, Whisper included) that people can download without Python.

Other flags: `--title "The Matrix"` to override the search, `--pick` to choose from
results, `--nosync`, `--backend deepl|google|argos`, `-o outdir`.

Output, next to the video by default:

| File | Contents |
| --- | --- |
| `movie.en.srt` | English, synced to the audio |
| `movie.fi.srt` | Finnish only |
| `movie.en-fi.srt` | English with Finnish underneath (yellow italics) |
| `movie.en-fi.ass` | Same, styled; best in VLC / mpv / IINA |

## How it works

1. **Search**: OpenSubtitles REST API, by file hash (exact release match) and by title guessed from the filename.
2. **Sync**: [ffsubsync](https://github.com/smacke/ffsubsync) aligns the subtitle timing to speech detected in the audio.
3. **Translate**:
   - `google` (default): free, no key. Unofficial endpoint, may rate-limit or block some networks.
   - `deepl`: best Finnish quality. Free tier is 500k characters/month (a movie is roughly 50–80k). Set `DEEPL_API_KEY`.
   - `argos`: fully offline. `uv sync --extra offline` (large download: pulls PyTorch) then `--backend argos`.
4. **Stack**: writes the dual `.srt` and `.ass`.

Movies themselves are never downloaded.
