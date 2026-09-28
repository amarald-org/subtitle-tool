"""Write stacked dual-language subtitles (English on top, Finnish below)."""

from __future__ import annotations

import re

import srt


def dual_srt(top: list[srt.Subtitle], bottom_texts: list[str], color: str = "#ffe066") -> str:
    subs = []
    for sub, lower in zip(top, bottom_texts):
        content = sub.content.strip()
        if lower:
            content += f'\n<font color="{color}"><i>{lower}</i></font>'
        subs.append(srt.Subtitle(index=sub.index, start=sub.start, end=sub.end, content=content))
    return srt.compose(subs)


def _ass_time(td) -> str:
    cs = int(round(td.total_seconds() * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(text: str) -> str:
    text = srt.make_legal_content(text)
    for tag, ass in (("<i>", r"{\i1}"), ("</i>", r"{\i0}"), ("<b>", r"{\b1}"), ("</b>", r"{\b0}")):
        text = text.replace(tag, ass)
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("\n", r"\N")


def dual_ass(top: list[srt.Subtitle], bottom_texts: list[str]) -> str:
    """ASS with two styles so players can render the languages distinctly."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: EN,Arial,58,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,50,1
Style: FI,Arial,54,&H0066E0FF,&H000000FF,&H00000000,&H64000000,0,1,0,0,100,100,0,0,1,3,1,2,60,60,50,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for sub, lower in zip(top, bottom_texts):
        start, end = _ass_time(sub.start), _ass_time(sub.end)
        text = _ass_text(sub.content.strip())
        if lower:
            # One event per cue so the two languages always stack, never overlap.
            text += r"\N{\rFI}" + _ass_text(lower)
        lines.append(f"Dialogue: 0,{start},{end},EN,,0,0,0,,{text}")
    return header + "\n".join(lines) + "\n"
