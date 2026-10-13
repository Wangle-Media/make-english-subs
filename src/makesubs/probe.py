"""Read-only inspection of a media file (PyAV bundles FFmpeg, so no separate install)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import av

from .timecode import FrameRate


@dataclass
class AudioStream:
    index: int            # position among audio streams (0-based)
    stream_index: int     # index in the container
    codec: str
    channels: int
    rate: int


@dataclass
class MediaInfo:
    path: Path
    duration: float
    fps: FrameRate | None
    start_tc: str | None          # source timecode of first frame, if any
    reel: str | None
    audio: list[AudioStream] = field(default_factory=list)

    @property
    def total_channels(self) -> int:
        return sum(a.channels for a in self.audio)

    def summary(self) -> str:
        chans = ", ".join(f"a{a.index}:{a.channels}ch" for a in self.audio) or "no audio"
        return (f"{self.path.name}: {self.duration:.1f}s, "
                f"{self.fps or 'unknown fps'}, TC {self.start_tc or 'none'}, {chans}")


def _first_tag(tags_list: list[dict], *keys: str) -> str | None:
    for tags in tags_list:
        low = {k.lower(): v for k, v in (tags or {}).items()}
        for key in keys:
            if low.get(key):
                return str(low[key]).strip()
    return None


def probe(path: Path) -> MediaInfo:
    path = Path(path)
    try:
        container = av.open(str(path), mode="r")
    except av.FFmpegError as e:
        raise RuntimeError(f"could not read {path.name}: {e}") from e
    with container:
        streams = list(container.streams)
        all_tags = [dict(container.metadata)] + [dict(s.metadata) for s in streams]
        start_tc = _first_tag(all_tags, "timecode")
        reel = _first_tag(all_tags, "reel_name", "com.apple.proapps.reel", "reel")

        video = next((s for s in container.streams.video
                      if not (s.disposition & av.stream.Disposition.attached_pic)), None)
        fps = None
        if video is not None:
            rate = video.average_rate or video.base_rate or video.guessed_rate
            if rate:
                fps = FrameRate.parse(str(rate), drop_frame=bool(start_tc and ";" in start_tc))

        audio = [AudioStream(i, s.index, s.codec_context.name, s.codec_context.channels, s.rate)
                 for i, s in enumerate(container.streams.audio)]
        if container.duration:
            duration = container.duration / av.time_base
        elif video is not None and video.duration and video.time_base:
            duration = float(video.duration * video.time_base)
        else:
            duration = 0.0
    return MediaInfo(path, duration, fps, start_tc, reel, audio)
