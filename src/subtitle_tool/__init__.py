"""subtitle-tool: fetch, sync, translate and stack subtitles for language learning."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import srt

from .dual import dual_ass, dual_srt


def guess_title(video: Path) -> str:
    """'The.Matrix.1999.1080p.BluRay.x264.mkv' -> 'The Matrix 1999'."""
    name = re.sub(r"[._()\[\]]+", " ", video.stem)
    m = re.search(r"^(.*?\b(19|20)\d{2})\b", name)
    if m:
        return m.group(1).strip()
    return re.split(r"\b(1080p|720p|2160p|480p|bluray|web|hdrip|x264|x265)\b", name, flags=re.I)[0].strip()


def find_local_subs(video: Path, lang: str) -> Path | None:
    """A subtitle file already next to the movie, e.g. movie.srt or movie.en.srt."""
    for name in (f"{video.stem}.{lang}.srt", f"{video.stem}.srt"):
        p = video.with_name(name)
        if p.exists():
            return p
    return None


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch_subtitles(args, video: Path, dest: Path) -> None:
    from .opensubtitles import Client

    client = Client()
    query = args.title or guess_title(video)
    log(f"Searching OpenSubtitles for '{query}' ({args.lang})...")
    hits = client.search(query, language=args.lang, video=video)
    if not hits:
        raise SystemExit("No subtitles found. Try --title 'Movie Name' or pass --subs.")
    choice = 0
    if args.pick:
        for i, h in enumerate(hits[:10]):
            log(f"  [{i}] {h.label()}")
        choice = int(input("Pick a number: ") or 0)
    hit = hits[choice]
    log(f"Using: {hit.label()}")
    dest.write_text(client.download(hit.file_id), encoding="utf-8")


def run(args) -> None:
    video = Path(args.video).expanduser()
    if not video.exists():
        raise SystemExit(f"Video not found: {video}")
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else video.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / video.stem

    if args.auto:
        lang, synced = auto_subtitles(args, video, base)
    else:
        args.lang = args.lang or "en"
        lang, synced = args.lang, fetch_or_local(args, video, base)

    if not args.translate:
        return

    subs = list(srt.parse(synced.read_text(encoding="utf-8")))
    from .translate import translate_lines

    log(f"Translating {len(subs)} lines from '{lang}' to '{args.target}' with {args.backend}...")
    try:
        fi = translate_lines(
            [s.content for s in subs],
            target=args.target,
            backend=args.backend,
            progress=lambda d, t: log(f"  {d}/{t}"),
            source=lang,
        )
    except Exception as e:
        raise SystemExit(
            f"Translation failed ({type(e).__name__}: {e}).\n"
            f"The subtitles are saved at {synced}. "
            "Try --backend deepl (needs DEEPL_API_KEY) or --backend argos (offline)."
        )

    for path in write_translation_outputs(base, lang, args.target, subs, fi):
        log(f"Wrote {path}")


def auto_subtitles(args, video: Path, base: Path) -> tuple[str, Path]:
    """Transcribe the audio with Whisper; timestamps come out already in sync."""
    from .transcribe import transcribe

    last = [""]

    def progress(msg: str) -> None:
        if msg != last[0]:
            last[0] = msg
            log(msg)

    lang, lines = transcribe(video, model=args.model, language=args.lang, music=args.music, progress=progress)
    out = Path(f"{base}.{lang}.srt")
    out.write_text(
        srt.compose(
            [
                srt.Subtitle(i + 1, timedelta(milliseconds=l.start), timedelta(milliseconds=l.end), l.text)
                for i, l in enumerate(lines)
            ]
        ),
        encoding="utf-8",
    )
    log(f"Detected language '{lang}'. Wrote {out} ({len(lines)} lines)")
    return lang, out


def fetch_or_local(args, video: Path, base: Path) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.srt"
        local = Path(args.subs).expanduser() if args.subs else None
        if local is None and not args.search:
            local = find_local_subs(video, args.lang)
            if local:
                log(f"Using existing {local.name} (pass --search to fetch from OpenSubtitles instead)")
        if local:
            raw.write_text(read_text(local), encoding="utf-8")
        else:
            fetch_subtitles(args, video, raw)

        synced = Path(f"{base}.{args.lang}.srt")
        if args.no_sync:
            synced.write_text(raw.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            from .sync import sync_to_video

            log("Syncing subtitles to the audio (ffsubsync)...")
            sync_to_video(video, raw, synced)
        log(f"Wrote {synced}")
    return synced


def write_translation_outputs(
    base: Path, lang: str, target: str, subs: list[srt.Subtitle], translated: list[str]
) -> list[Path]:
    """Write movie.fi.srt, movie.en-fi.srt and movie.en-fi.ass."""
    target_srt = Path(f"{base}.{target}.srt")
    target_srt.write_text(
        srt.compose(
            [srt.Subtitle(s.index, s.start, s.end, t) for s, t in zip(subs, translated)],
            reindex=False,
        ),
        encoding="utf-8",
    )
    dual = Path(f"{base}.{lang}-{target}.srt")
    dual.write_text(dual_srt(subs, translated), encoding="utf-8")
    dual_a = Path(f"{base}.{lang}-{target}.ass")
    dual_a.write_text(dual_ass(subs, translated), encoding="utf-8")
    return [target_srt, dual, dual_a]


def default_backend() -> str:
    return "deepl" if os.environ.get("DEEPL_API_KEY") else "google"


def models_command(argv: list[str]) -> None:
    """subtitle-tool models [--delete NAME|all]: list or remove downloaded Whisper models."""
    from .transcribe import delete_model, downloaded_models

    p = argparse.ArgumentParser(prog="subtitle-tool models", description="Downloaded Whisper models")
    p.add_argument("--delete", metavar="NAME", help="model name (e.g. large-v3-turbo) or 'all'")
    args = p.parse_args(argv)
    models = downloaded_models()
    if args.delete:
        targets = [m for m in models if args.delete in ("all", m[0], m[1])]
        if not targets:
            raise SystemExit(f"No downloaded model called '{args.delete}'.")
        for name, repo, _size in targets:
            print(f"Deleted {name}, freed {delete_model(repo) / 1e9:.2f} GB")
        return
    if not models:
        print("No Whisper models downloaded yet.")
    for name, _repo, size in models:
        print(f"{name:16} {size / 1e9:.2f} GB")
    if models:
        print("\nDelete one with: subtitle-tool models --delete NAME   (or --delete all)")


def main() -> None:
    if sys.argv[1:2] == ["models"]:
        models_command(sys.argv[2:])
        return
    if sys.argv[1:2] == ["gui"]:
        try:
            from .gui import main as gui_main
        except ImportError as e:
            raise SystemExit(
                f"The editor window needs the GUI extras ({e.name} missing). Reinstall with:\n"
                "  uv tool install --force 'subtitle-tool[gui,auto] @ git+https://github.com/aaro-cmd/subtitle-tool'"
            )

        gui_main(sys.argv[2:])
        return
    p = argparse.ArgumentParser(
        prog="subtitle-tool",
        description="Find subtitles for a movie file, sync them to its audio, and "
        "optionally add a Finnish translation stacked under the English. "
        "Run `subtitle-tool gui [movie]` for the editor window, "
        "`subtitle-tool models` to list or delete downloaded Whisper models.",
    )
    p.add_argument("video", help="path to the movie file")
    p.add_argument("--title", help="movie name to search (default: guessed from filename)")
    p.add_argument("--subs", help="use this subtitle file (default: movie.en.srt or movie.srt next to the video, else search)")
    p.add_argument("--search", action="store_true",
                   help="search OpenSubtitles even if movie.srt / movie.en.srt already exists")
    p.add_argument("--lang", help="subtitle language: to fetch (default: en), or spoken in the video with --auto "
                   "(default: detect)")
    p.add_argument("-a", "--auto", action="store_true",
                   help="make subtitles from the audio with Whisper instead of searching (add -t to translate)")
    p.add_argument("--model", default="small",
                   help="Whisper model for --auto: tiny, base, small (default), medium, large-v3-turbo")
    p.add_argument("--music", action="store_true", help="--auto tuned for songs and music videos")
    p.add_argument("--pick", action="store_true", help="choose from search results interactively")
    p.add_argument("--no-sync", "--nosync", dest="no_sync", action="store_true", help="skip audio sync")
    p.add_argument("-t", "--translate", action="store_true", help="also translate and write dual-language subs")
    p.add_argument("--target", default="fi", help="translation language (default: fi)")
    p.add_argument("--backend", choices=["google", "deepl", "argos"], default=default_backend(),
                   help="deepl: best Finnish, needs DEEPL_API_KEY (default when set). "
                   "google: free, no key. argos: offline")
    p.add_argument("-o", "--out-dir", help="output folder (default: next to the video)")
    run(p.parse_args())
