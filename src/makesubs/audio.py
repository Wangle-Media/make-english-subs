"""Decode the audio of a clip to 16 kHz mono float32, without touching the source file."""
from __future__ import annotations

import av
import numpy as np

from .probe import MediaInfo

SAMPLE_RATE = 16000
_SCALE = {"s16": 32768.0, "s16p": 32768.0, "s32": 2147483648.0, "s32p": 2147483648.0,
          "u8": 128.0, "u8p": 128.0}


def channel_choices(info: MediaInfo) -> list[str]:
    """Human labels for every channel, numbered 1..N across all audio streams."""
    labels, n = [], 0
    for a in info.audio:
        for c in range(a.channels):
            n += 1
            labels.append(f"{n}" + (f" (stream {a.index + 1}, ch {c + 1})" if len(info.audio) > 1 else ""))
    return labels


def _resolve(info: MediaInfo, channel: str) -> list[tuple[int, int | None]]:
    """(audio stream index, channel or None for all channels) pairs to decode."""
    if not info.audio:
        raise RuntimeError(f"{info.path.name} has no audio streams")
    if channel == "mix":
        return [(a.index, None) for a in info.audio]
    wanted, n = int(channel), 0
    for a in info.audio:
        for c in range(a.channels):
            n += 1
            if n == wanted:
                return [(a.index, c)]
    raise ValueError(f"channel {wanted} does not exist; {info.path.name} has {n} channels")


def _frame_to_float(frame: av.AudioFrame) -> np.ndarray:
    """(channels, samples) float32 in -1..1, whatever the sample format."""
    arr = frame.to_ndarray()
    channels = len(frame.layout.channels)
    if not frame.format.is_planar:
        arr = arr.reshape(-1, channels).T
    scale = _SCALE.get(frame.format.name)
    arr = arr.astype(np.float32)
    if frame.format.name.startswith("u8"):
        arr -= 128.0
    return arr / scale if scale else arr


def _decode_stream(info: MediaInfo, audio_index: int, channel: int | None) -> np.ndarray:
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
    out: list[np.ndarray] = []

    def push(frame):
        for r in resampler.resample(frame):
            out.append(r.to_ndarray().reshape(-1))

    with av.open(str(info.path), mode="r") as container:
        stream = container.streams.audio[audio_index]
        for frame in container.decode(stream):
            data = _frame_to_float(frame)
            mono = data[channel] if channel is not None else data.mean(axis=0)
            f = av.AudioFrame.from_ndarray(np.ascontiguousarray(mono[None, :], dtype=np.float32),
                                           format="fltp", layout="mono")
            f.sample_rate = frame.sample_rate
            f.pts = None
            push(f)
        push(None)  # flush
    return np.concatenate(out) if out else np.zeros(0, np.float32)


def load_audio(info: MediaInfo, channel: str = "mix") -> np.ndarray:
    tracks = [_decode_stream(info, s, c) for s, c in _resolve(info, channel)]
    length = max(t.size for t in tracks)
    audio = np.zeros(length, np.float32)
    for t in tracks:
        audio[:t.size] += t
    audio /= len(tracks)
    if audio.size == 0:
        raise RuntimeError(f"no audio decoded from {info.path.name}")
    # Scratch audio is often very quiet; peak-normalise so VAD and ASR see a usable level.
    peak = float(np.max(np.abs(audio)))
    if 0 < peak < 0.5:
        audio *= 0.5 / peak
    return audio
