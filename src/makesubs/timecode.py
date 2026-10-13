"""SMPTE timecode maths, including 29.97/59.94 drop-frame."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction

_TC_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})([:;.,])(\d{2,3})$")


@dataclass(frozen=True)
class FrameRate:
    rate: Fraction          # exact rate, e.g. 24000/1001
    drop_frame: bool = False

    @property
    def nominal(self) -> int:
        """Frames per timecode second (24, 25, 30, 60...)."""
        return round(self.rate)

    @classmethod
    def parse(cls, value: str, drop_frame: bool = False) -> "FrameRate":
        """Parse ffprobe-style '24000/1001', '25/1' or '25'."""
        rate = Fraction(value) if "/" in value else Fraction(value).limit_denominator(1001)
        if rate <= 0:
            raise ValueError(f"invalid frame rate: {value}")
        # Drop-frame only exists for 29.97 and 59.94.
        df = drop_frame and rate.denominator == 1001 and round(rate) in (30, 60)
        return cls(rate, df)

    def __str__(self) -> str:
        return f"{float(self.rate):.3f}{' DF' if self.drop_frame else ''}"


def seconds_to_frames(seconds: float, fps: FrameRate, mode: str = "round") -> int:
    exact = Fraction(seconds).limit_denominator(1_000_000) * fps.rate
    if mode == "floor":
        return math.floor(exact)
    if mode == "ceil":
        return math.ceil(exact - Fraction(1, 1000))  # tolerate float noise
    return round(exact)


def frames_to_seconds(frames: int, fps: FrameRate) -> float:
    return float(Fraction(frames) / fps.rate)


def _drop_per_minute(fps: FrameRate) -> int:
    return 2 if fps.nominal == 30 else 4


def tc_to_frames(tc: str, fps: FrameRate) -> int:
    m = _TC_RE.match(tc.strip())
    if not m:
        raise ValueError(f"invalid timecode: {tc!r}")
    h, mi, s, _sep, f = (int(g) if g.isdigit() else g for g in m.groups())
    nom = fps.nominal
    if f >= nom:
        raise ValueError(f"frame {f} out of range for {fps}: {tc!r}")
    total_minutes = 60 * h + mi
    frames = (3600 * h + 60 * mi + s) * nom + f
    if fps.drop_frame:
        frames -= _drop_per_minute(fps) * (total_minutes - total_minutes // 10)
    return frames


def frames_to_tc(frames: int, fps: FrameRate) -> str:
    nom = fps.nominal
    frames %= 24 * 3600 * nom  # wrap at 24h like decks do
    if fps.drop_frame:
        drop = _drop_per_minute(fps)
        per_10min = nom * 600 - drop * 9
        per_min = nom * 60 - drop
        d, m = divmod(frames, per_10min)
        if m > drop:
            frames += drop * 9 * d + drop * ((m - drop) // per_min)
        else:
            frames += drop * 9 * d
    f = frames % nom
    s = frames // nom % 60
    mi = frames // (nom * 60) % 60
    h = frames // (nom * 3600)
    sep = ";" if fps.drop_frame else ":"
    return f"{h:02d}:{mi:02d}:{s:02d}{sep}{f:02d}"
