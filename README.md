# subtitle-tool

Find English subtitles for a movie file you already have, sync them to the audio,
and optionally add a Finnish translation stacked under each English line for
language learning.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and `ffmpeg` (`brew install ffmpeg`).

```sh
uv sync
```

Get a free OpenSubtitles API key at https://www.opensubtitles.com/consumers and set it:

```sh
export OPENSUBTITLES_API_KEY=...
# optional, raises the daily download limit:
export OPENSUBTITLES_USERNAME=...
export OPENSUBTITLES_PASSWORD=...
```

## Usage

```sh
# Fetch English subs and sync them to the movie
uv run subtitle-tool ~/Movies/The.Matrix.1999.1080p.mkv

# Same, plus Finnish translation and dual-language files
uv run subtitle-tool ~/Movies/The.Matrix.1999.1080p.mkv --translate

# Use a subtitle file you already have instead of searching
uv run subtitle-tool movie.mkv --subs movie.en.srt --translate
```

Useful flags: `--title "The Matrix"` to override the search, `--pick` to choose from
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
