"""Speech-to-subtitles with Whisper (faster-whisper, runs locally on the CPU).

The model is downloaded on first use to the Hugging Face cache
(~/.cache/huggingface). Sizes: tiny 75 MB, base 145 MB, small 480 MB,
medium 1.5 GB, large-v3-turbo 1.6 GB (best quality/speed trade-off).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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


def transcribe(
    media: Path,
    model: str = DEFAULT_MODEL,
    language: str | None = None,
    music: bool = False,
    progress=None,
) -> tuple[str, list[Line]]:
    """Return (detected language code, subtitle lines).

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

    if progress:
        progress(f"Loading Whisper '{model}' (downloads once)…")
    wm = WhisperModel(model, device="auto", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    segments, info = wm.transcribe(
        str(media),
        language=language,
        word_timestamps=True,
        vad_filter=not music,
        condition_on_previous_text=not music,
    )
    total = info.duration or 0
    words = []
    for seg in segments:
        if progress and total:
            progress(f"Transcribing… {int(100 * seg.end / total)}%")
        if seg.words:
            words += [(w.start, w.end, w.word) for w in seg.words]
        else:
            words.append((seg.start, seg.end, " " + seg.text.strip()))
    return info.language, _to_lines(words)


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
