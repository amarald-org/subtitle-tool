"""Align a subtitle file to a video's audio.

The audio is decoded with PyAV (bundled FFmpeg libraries, no ffmpeg program
needed), speech is detected with webrtcvad at 100 frames/second, and ffsubsync
finds the best offset/framerate between that speech track and the subtitles.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

RATE = 16000  # webrtcvad accepts 8/16/32/48 kHz
WINDOW = RATE // 100  # 10 ms windows = ffsubsync's 100 Hz sample rate


def speech_track(media: Path, progress=None) -> np.ndarray:
    """1.0 where someone is speaking, 0.0 elsewhere, one value per 10 ms."""
    import av
    import webrtcvad

    vad = webrtcvad.Vad(3)
    out: list[float] = []
    pending = b""
    with av.open(str(media)) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        duration = float(stream.duration * stream.time_base) if stream.duration else (
            container.duration / 1_000_000 if container.duration else 0
        )
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        last_pct = -1
        for frame in container.decode(stream):
            for chunk in resampler.resample(frame):
                pending += bytes(chunk.planes[0])[: chunk.samples * 2]
            n = len(pending) // (WINDOW * 2)
            for i in range(n):
                window = pending[i * WINDOW * 2 : (i + 1) * WINDOW * 2]
                try:
                    out.append(1.0 if vad.is_speech(window, RATE) else 0.0)
                except Exception:
                    out.append(0.0)
            pending = pending[n * WINDOW * 2 :]
            if progress and duration and frame.time is not None:
                pct = int(100 * frame.time / duration)
                if pct != last_pct:
                    last_pct = pct
                    progress(f"Listening for speech… {pct}%")
    return np.array(out)


def sync_to_video(video: Path, subs: Path, out: Path, progress=None) -> None:
    import logging

    from ffsubsync.ffsubsync import make_parser, run

    logging.getLogger("ffsubsync").setLevel(logging.WARNING)
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("ffsubsync"):
            logging.getLogger(name).setLevel(logging.WARNING)

    speech = speech_track(video, progress)
    if not speech.any():
        raise RuntimeError("No speech found in the audio, so there is nothing to sync to.")
    if progress:
        progress("Aligning subtitles to the speech…")
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "speech.npz"
        np.savez_compressed(ref, speech=speech)
        args = make_parser().parse_args([str(ref), "-i", str(subs), "-o", str(out)])
        result = run(args)
    if not result.get("sync_was_successful") or not out.exists():
        raise RuntimeError("Sync failed: couldn't match the subtitles to the speech.")
