"""Speech-to-subtitles with Whisper (faster-whisper, runs locally on the CPU).

The model is downloaded on first use to the Hugging Face cache
(~/.cache/huggingface). Sizes: tiny 75 MB, base 145 MB, small 480 MB,
medium 1.5 GB, large-v3-turbo 1.6 GB (best quality/speed trade-off).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Must be set before huggingface_hub is imported. Plain HTTP downloads write into
# the cache as they go, which lets us show real progress (Xet writes at the end).
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

MODELS = ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]
DEFAULT_MODEL = "small"
MAX_CHARS = 84  # two lines of ~42, the usual subtitle limit
MAX_SECONDS = 6.0
LINE_WIDTH = 42


@dataclass
class Line:
    start: int  # ms
    end: int  # ms
    text: str


def _quiet_libraries() -> None:
    """Hide harmless library chatter (HF token hint, FFmpeg decoder notes)."""
    try:
        from huggingface_hub.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
    except Exception:
        pass
    try:
        import av.logging

        av.logging.set_level(av.logging.ERROR)
    except Exception:
        pass


def repo_for(model: str) -> str:
    from faster_whisper.utils import _MODELS

    return _MODELS.get(model, model)


def _repo_cache_dir(repo: str) -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE

    return Path(HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


ALLOW = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def ensure_model(model: str, progress=None) -> str:
    """Local path of the model, downloading it first with % / MB/s progress."""
    import threading
    import time

    from huggingface_hub import HfApi, snapshot_download

    _quiet_libraries()
    repo = repo_for(model)
    try:
        return snapshot_download(repo, allow_patterns=ALLOW, local_files_only=True)
    except Exception:
        pass

    total = 0
    try:
        info = HfApi().model_info(repo, files_metadata=True)
        total = sum((f.size or 0) for f in info.siblings if f.rfilename in
                    ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json")
                    or f.rfilename.startswith("vocabulary."))
    except Exception:
        pass

    result: dict = {}

    def download():
        try:
            result["path"] = snapshot_download(repo, allow_patterns=ALLOW)
        except Exception as e:
            result["error"] = e

    cache = _repo_cache_dir(repo)
    start_size = _dir_size(cache) if cache.exists() else 0
    t = threading.Thread(target=download, daemon=True)
    t.start()
    last_time, last_size = time.time(), start_size
    speed = 0.0
    while t.is_alive():
        t.join(0.5)
        if not progress:
            continue
        size = _dir_size(cache) if cache.exists() else 0
        now = time.time()
        if now - last_time >= 1:
            speed = 0.6 * speed + 0.4 * (size - last_size) / (now - last_time)
            last_time, last_size = now, size
        mb = size / 1e6
        if total:
            pct = min(99, int(100 * size / total))
            eta = f", {int((total - size) / speed)} s left" if speed > 0 else ""
            progress(f"Downloading Whisper '{model}': {pct}% ({mb:.0f} of {total / 1e6:.0f} MB, "
                     f"{speed / 1e6:.1f} MB/s{eta})")
        else:
            progress(f"Downloading Whisper '{model}': {mb:.0f} MB ({speed / 1e6:.1f} MB/s)")
    if "error" in result:
        raise result["error"]
    return result["path"]


def downloaded_models() -> list[tuple[str, str, int]]:
    """(model name, repo id, bytes on disk) for Whisper models in the cache."""
    from faster_whisper.utils import _MODELS
    from huggingface_hub import scan_cache_dir

    names = {}
    for name, repo in _MODELS.items():
        names.setdefault(repo, name)
    try:
        cache = scan_cache_dir()
    except Exception:
        return []
    return [(names[r.repo_id], r.repo_id, r.size_on_disk) for r in cache.repos if r.repo_id in names]


def delete_model(repo: str) -> int:
    """Remove a downloaded model; returns bytes freed."""
    from huggingface_hub import scan_cache_dir

    cache = scan_cache_dir()
    hashes = [rev.commit_hash for r in cache.repos if r.repo_id == repo for rev in r.revisions]
    if not hashes:
        return 0
    strategy = cache.delete_revisions(*hashes)
    strategy.execute()
    return strategy.expected_freed_size


def transcribe(
    media: Path,
    model: str = DEFAULT_MODEL,
    language: str | None = None,
    music: bool = False,
    progress=None,
    on_lines=None,
) -> tuple[str, list[Line]]:
    """Return (detected language code, subtitle lines).

    on_lines(lang, new_lines) is called as lines become final, so a UI can fill in
    while the rest of the audio is still being transcribed.

    music=True keeps quiet/sung passages (no voice-activity filter) and doesn't
    let the previous line steer the next, which helps with lyrics.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "Automatic subtitles need the 'auto' extra: "
            "uv tool install --force 'subtitle-tool[gui,auto] @ git+https://github.com/aaro-cmd/subtitle-tool'"
        ) from e

    path = ensure_model(model, progress)
    if progress:
        progress(f"Loading Whisper '{model}'…")
    wm = WhisperModel(path, device="auto", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    segments, info = wm.transcribe(
        str(media),
        language=language,
        word_timestamps=True,
        vad_filter=not music,
        condition_on_previous_text=not music,
    )
    total = info.duration or 0
    pending: list[tuple[float, float, str]] = []
    done: list[Line] = []
    for seg in segments:
        if progress and total:
            progress(f"Transcribing… {int(100 * seg.end / total)}%")
        if seg.words:
            pending += [(w.start, w.end, w.word) for w in seg.words]
        else:
            pending.append((seg.start, seg.end, " " + seg.text.strip()))
        lines = _to_lines(pending)
        if len(lines) > 1:
            # Everything but the last line is final; the last may still grow.
            final = lines[:-1]
            cut = lines[-1].start / 1000
            pending = [w for w in pending if w[0] >= cut - 1e-6]
            done += final
            if on_lines:
                on_lines(info.language, final)
    final = _to_lines(pending)
    done += final
    if on_lines and final:
        on_lines(info.language, final)
    return info.language, done


def _balance(text: str) -> str:
    """Up to two lines of similar length, split at the space nearest the middle."""
    if len(text) <= LINE_WIDTH:
        return text
    mid = len(text) // 2
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    if not spaces:
        return text
    cut = min(spaces, key=lambda i: abs(i - mid))
    return text[:cut] + "\n" + text[cut + 1 :]


def _to_lines(words: list[tuple[float, float, str]]) -> list[Line]:
    """Group words into readable subtitle cues, breaking at punctuation where possible."""
    lines: list[Line] = []
    cur: list[tuple[float, float, str]] = []

    def text_of(ws) -> str:
        return "".join(w[2] for w in ws).strip()

    def flush(upto: int | None = None):
        take = cur[:upto] if upto is not None else list(cur)
        del cur[: len(take)]
        text = text_of(take)
        if text:
            lines.append(Line(int(take[0][0] * 1000), int(take[-1][1] * 1000), _balance(text)))

    for w in words:
        if cur:
            too_long = len(text_of(cur)) + len(w[2]) > MAX_CHARS or w[1] - cur[0][0] > MAX_SECONDS
            if w[0] - cur[-1][1] > 0.8:  # pause in speech
                flush()
            elif too_long:
                # Prefer to end at the last comma/period in the back two thirds.
                cut = None
                for k in range(len(cur) - 1, len(cur) // 3 - 1, -1):
                    if cur[k][2].rstrip().endswith((".", "?", "!", ",", ";", ":")):
                        cut = k + 1
                        break
                flush(cut)
        cur.append(w)
        if w[2].rstrip().endswith((".", "?", "!")) and len(text_of(cur)) > 12:
            flush()
    flush()

    # Keep cues on screen long enough to read, without overlapping the next one.
    for i, line in enumerate(lines):
        min_end = line.start + 1000
        nxt = lines[i + 1].start if i + 1 < len(lines) else None
        if line.end < min_end:
            line.end = min(min_end, nxt) if nxt else min_end
    return lines
