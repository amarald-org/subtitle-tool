"""subtitle-tool: fetch, sync, translate and stack subtitles for language learning."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

import srt

from .dual import dual_ass, dual_srt


def guess_title(video: Path) -> str:
    """'The.Matrix.1999.1080p.BluRay.x264.mkv' -> 'The Matrix 1999'."""
    name = re.sub(r"[._]+", " ", video.stem)
    m = re.search(r"^(.*?\b(19|20)\d{2})\b", name)
    if m:
        return m.group(1).strip()
    return re.split(r"\b(1080p|720p|2160p|480p|bluray|web|hdrip|x264|x265)\b", name, flags=re.I)[0].strip()


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

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.srt"
        if args.subs:
            raw.write_text(Path(args.subs).expanduser().read_text(encoding="utf-8-sig"), encoding="utf-8")
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

    if not args.translate:
        return

    subs = list(srt.parse(synced.read_text(encoding="utf-8")))
    from .translate import translate_lines

    log(f"Translating {len(subs)} lines to '{args.target}' with {args.backend}...")
    try:
        fi = translate_lines(
            [s.content for s in subs],
            target=args.target,
            backend=args.backend,
            progress=lambda d, t: log(f"  {d}/{t}"),
        )
    except Exception as e:
        raise SystemExit(
            f"Translation failed ({type(e).__name__}: {e}).\n"
            f"The synced subtitles are saved at {synced}. "
            "Try --backend deepl (needs DEEPL_API_KEY) or --backend argos (offline)."
        )

    target_srt = Path(f"{base}.{args.target}.srt")
    target_srt.write_text(
        srt.compose([srt.Subtitle(s.index, s.start, s.end, t) for s, t in zip(subs, fi)]),
        encoding="utf-8",
    )
    log(f"Wrote {target_srt}")

    dual = Path(f"{base}.{args.lang}-{args.target}.srt")
    dual.write_text(dual_srt(subs, fi), encoding="utf-8")
    log(f"Wrote {dual}")
    dual_a = Path(f"{base}.{args.lang}-{args.target}.ass")
    dual_a.write_text(dual_ass(subs, fi), encoding="utf-8")
    log(f"Wrote {dual_a}")


def main() -> None:
    p = argparse.ArgumentParser(
        prog="subtitle-tool",
        description="Find subtitles for a movie file, sync them to its audio, and "
        "optionally add a Finnish translation stacked under the English.",
    )
    p.add_argument("video", help="path to the movie file")
    p.add_argument("--title", help="movie name to search (default: guessed from filename)")
    p.add_argument("--subs", help="use this subtitle file instead of searching OpenSubtitles")
    p.add_argument("--lang", default="en", help="subtitle language to fetch (default: en)")
    p.add_argument("--pick", action="store_true", help="choose from search results interactively")
    p.add_argument("--no-sync", action="store_true", help="skip audio sync")
    p.add_argument("-t", "--translate", action="store_true", help="also translate and write dual-language subs")
    p.add_argument("--target", default="fi", help="translation language (default: fi)")
    p.add_argument("--backend", choices=["google", "deepl", "argos"], default="google",
                   help="google: free, no key. deepl: best Finnish, needs DEEPL_API_KEY. argos: offline")
    p.add_argument("-o", "--out-dir", help="output folder (default: next to the video)")
    run(p.parse_args())
