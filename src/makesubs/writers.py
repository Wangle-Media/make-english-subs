"""Sidecar subtitle writers for Premiere, Resolve (SRT) and Avid (SubCap)."""
from __future__ import annotations

from pathlib import Path

from .shape import Cue, wrap
from .timecode import FrameRate, frames_to_tc, seconds_to_frames, tc_to_frames

FLAG = "[?] "


def _srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _text(cue: Cue, mark_flagged: bool) -> str:
    return (FLAG if mark_flagged and cue.flagged else "") + wrap(cue.text)


def write_srt(path: Path, cues: list[Cue], mark_flagged: bool = True) -> None:
    """Times are relative to the first frame of the clip (what Premiere/Resolve expect)."""
    blocks = [f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{_text(c, mark_flagged)}\n"
              for i, c in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8", newline="\n")


def write_subcap(path: Path, cues: list[Cue], fps: FrameRate, start_tc: str,
                 mark_flagged: bool = True) -> None:
    """Avid DS / SubCap text file. Timecodes are absolute and must match the sequence."""
    base = tc_to_frames(start_tc, fps)
    lines = ["@ makesubs", "", "<begin subtitles>", ""]
    for c in cues:
        a = frames_to_tc(base + seconds_to_frames(c.start, fps), fps)
        b = frames_to_tc(base + seconds_to_frames(c.end, fps), fps)
        lines += [f"{a} {b}", _text(c, mark_flagged), ""]
    lines.append("<end subtitles>")
    # BOM so Media Composer on Windows detects UTF-8 (Arabic/Danish characters).
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig", newline="\r\n")
