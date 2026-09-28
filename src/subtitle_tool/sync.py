"""Align a subtitle file to a video's audio using ffsubsync."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def sync_to_video(video: Path, subs: Path, out: Path) -> None:
    exe = Path(sys.executable).with_name("ffsubsync")
    cmd = [str(exe), str(video), "-i", str(subs), "-o", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out.exists():
        tail = "\n".join((result.stderr or result.stdout).splitlines()[-15:])
        raise RuntimeError(f"ffsubsync failed:\n{tail}")
