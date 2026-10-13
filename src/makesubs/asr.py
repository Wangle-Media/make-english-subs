"""Speech recognition: VAD chunking, per-chunk language detection, Whisper backends."""
from __future__ import annotations

import gc
import platform
import re
import sys
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .audio import SAMPLE_RATE

Progress = Callable[[str], None]

# Phrases Whisper is known to invent on silence/noise (mostly from subtitled training data).
_HALLUCINATIONS = [
    r"ترجمة\s*نانسي\s*قنقر", r"اشتركوا?\s*في\s*القناة", r"شكرا\s*(لكم\s*)?على\s*المشاهدة",
    r"thanks?\s+(you\s+)?for\s+watching", r"subtitles?\s+by", r"please\s+subscribe",
    r"amara\.org", r"^\W*$",
]
_HALLUCINATION_RE = re.compile("|".join(_HALLUCINATIONS), re.IGNORECASE)


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float


@dataclass
class Segment:
    start: float
    end: float
    text: str
    lang: str
    words: list[Word] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        if not self.words:
            return 0.0
        return sum(w.prob for w in self.words) / len(self.words)


@dataclass
class Transcript:
    source: list[Segment]                 # in the spoken language(s)
    english: list[Segment] | None = None  # Whisper's own translation, if requested
    languages: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- engines

class Engine:
    name = "?"

    def detect(self, audio: np.ndarray) -> dict[str, float]:
        raise NotImplementedError

    def run(self, audio: np.ndarray, language: str, task: str) -> list[Segment]:
        raise NotImplementedError

    def unload(self) -> None:
        pass


class FasterWhisperEngine(Engine):
    def __init__(self, model: str = "large-v3", device: str = "auto"):
        from .tools import load_nvidia_libs
        load_nvidia_libs()
        import ctranslate2
        from faster_whisper import WhisperModel

        if device == "auto":
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        compute = "float16" if device == "cuda" else "int8"
        self.name = f"faster-whisper {model} ({device}, {compute})"
        self.model = WhisperModel(model, device=device, compute_type=compute)

    def detect(self, audio):
        _lang, _p, probs = self.model.detect_language(audio)
        return dict(probs)

    def run(self, audio, language, task):
        segs, _info = self.model.transcribe(
            audio, language=language, task=task, beam_size=5, word_timestamps=True,
            vad_filter=False, condition_on_previous_text=False)
        return [Segment(s.start, s.end, s.text.strip(), language if task == "transcribe" else "en",
                        [Word(w.start, w.end, w.word, w.probability) for w in (s.words or [])])
                for s in segs]

    def unload(self):
        del self.model
        gc.collect()


class MlxEngine(Engine):
    def __init__(self, model: str = "mlx-community/whisper-large-v3-mlx"):
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder

        self.repo = model
        self.name = f"mlx-whisper {model}"
        self._mx = mx
        self.model = ModelHolder.get_model(model, mx.float16)

    def detect(self, audio):
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
        mel = log_mel_spectrogram(audio, n_mels=self.model.dims.n_mels, padding=N_SAMPLES)
        seg = pad_or_trim(mel, N_FRAMES, axis=-2).astype(self._mx.float16)
        _tokens, probs = self.model.detect_language(seg)
        return dict(probs)

    def run(self, audio, language, task):
        import mlx_whisper
        res = mlx_whisper.transcribe(
            audio, path_or_hf_repo=self.repo, language=language, task=task,
            word_timestamps=True, condition_on_previous_text=False, verbose=None)
        return [Segment(s["start"], s["end"], s["text"].strip(),
                        language if task == "transcribe" else "en",
                        [Word(w["start"], w["end"], w["word"], w.get("probability", 0.0))
                         for w in s.get("words", [])])
                for s in res["segments"]]

    def unload(self):
        from mlx_whisper.transcribe import ModelHolder
        ModelHolder.model = None
        ModelHolder.model_path = None
        del self.model
        gc.collect()
        try:
            self._mx.clear_cache()
        except AttributeError:
            pass


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def create_engine(backend: str = "auto", model: str | None = None) -> Engine:
    if backend == "auto":
        backend = "mlx" if is_apple_silicon() else "faster-whisper"
    if backend == "mlx":
        return MlxEngine(model or "mlx-community/whisper-large-v3-mlx")
    return FasterWhisperEngine(model or "large-v3")


# ---------------------------------------------------------------- pipeline

@dataclass
class Chunk:
    start: int  # samples
    end: int
    lang: str = ""

    @property
    def seconds(self) -> float:
        return (self.end - self.start) / SAMPLE_RATE


def speech_chunks(audio: np.ndarray, max_len: float = 25.0, max_gap: float = 0.6) -> list[Chunk]:
    """Find speech with Silero VAD and group it into chunks of at most max_len seconds."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    opts = VadOptions(threshold=0.4, min_silence_duration_ms=500, speech_pad_ms=200,
                      max_speech_duration_s=max_len)
    chunks: list[Chunk] = []
    for ts in get_speech_timestamps(audio, opts):
        if chunks and (ts["start"] - chunks[-1].end) / SAMPLE_RATE < max_gap \
                and (ts["end"] - chunks[-1].start) / SAMPLE_RATE <= max_len:
            chunks[-1].end = ts["end"]
        else:
            chunks.append(Chunk(ts["start"], ts["end"]))
    return chunks


def _assign_languages(engine: Engine, audio: np.ndarray, chunks: list[Chunk],
                      allowed: list[str]) -> None:
    """Detect language per chunk, restricted to the languages expected on set.

    allowed == ["auto"] accepts any language Whisper knows.
    Very short chunks give unreliable detection, so they inherit from a neighbour.
    """
    if len(allowed) == 1 and allowed[0] != "auto":
        for c in chunks:
            c.lang = allowed[0]
        return
    for c in chunks:
        if c.seconds >= 1.5:
            probs = engine.detect(audio[c.start:c.end])
            pool = list(probs) if allowed == ["auto"] else allowed
            c.lang = max(pool, key=lambda lang: probs.get(lang, 0.0))
    if all(not c.lang for c in chunks):  # only very short chunks: detect on all speech
        probs = engine.detect(np.concatenate([audio[c.start:c.end] for c in chunks]))
        pool = list(probs) if allowed == ["auto"] else allowed
        chunks[0].lang = max(pool, key=lambda lang: probs.get(lang, 0.0))
    for i, c in enumerate(chunks):
        if not c.lang:
            near = [x for x in chunks[i - 1::-1] if x.lang][:1] if i else []
            near = near or [x for x in chunks[i + 1:] if x.lang][:1]
            c.lang = near[0].lang if near else allowed[0]


def _offset(segs: list[Segment], seconds: float) -> list[Segment]:
    for s in segs:
        s.start += seconds
        s.end += seconds
        for w in s.words:
            w.start += seconds
            w.end += seconds
    return [s for s in segs if not _HALLUCINATION_RE.search(s.text)]


def _norm(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


def _drop_repeated_tail(prev: list[Segment], new: list[Segment]) -> None:
    """When speech is cut mid-sentence, Whisper often completes the cut-off words at the end
    of one chunk and hears them again at the start of the next. Drop the earlier copy."""
    if not prev or not new or not prev[-1].words or not new[0].words:
        return
    tail = [w for s in prev[-2:] for w in s.words]
    head = new[0].words
    for n in range(min(8, len(tail), len(head)), 0, -1):
        if [_norm(w.text) for w in tail[-n:]] == [_norm(w.text) for w in head[:n]] \
                and (n >= 2 or len(_norm(head[0].text)) > 3):
            remove = n
            for seg in reversed(prev):
                k = min(remove, len(seg.words))
                del seg.words[len(seg.words) - k:]
                remove -= k
                seg.text = "".join(w.text for w in seg.words).strip()
                if seg.words:
                    seg.end = seg.words[-1].end
                if remove == 0:
                    break
            prev[:] = [s for s in prev if s.words]
            return


def transcribe(engine: Engine, audio: np.ndarray, spoken: list[str], want_english: bool,
               progress: Progress = print) -> Transcript:
    # Shorter chunks localise language switches better; longer ones give Whisper more context.
    multilingual = spoken == ["auto"] or len(spoken) > 1
    chunks = speech_chunks(audio, max_len=15.0 if multilingual else 25.0)
    if not chunks:
        progress("  no speech found")
        return Transcript([], [] if want_english else None, [])
    total = sum(c.seconds for c in chunks)
    progress(f"  {len(chunks)} speech chunks, {total:.0f}s of speech")
    _assign_languages(engine, audio, chunks, spoken)

    source: list[Segment] = []
    english: list[Segment] | None = [] if want_english else None
    for i, c in enumerate(chunks, 1):
        progress(f"  chunk {i}/{len(chunks)} [{c.lang}] {c.start / SAMPLE_RATE:.1f}s")
        piece = audio[c.start:c.end]
        t0 = c.start / SAMPLE_RATE
        segs = _offset(engine.run(piece, c.lang, "transcribe"), t0)
        _drop_repeated_tail(source, segs)
        source += segs
        if english is not None:
            if c.lang == "en":
                en = [Segment(s.start, s.end, s.text, s.lang, list(s.words)) for s in segs]
            else:
                en = _offset(engine.run(piece, c.lang, "translate"), t0)
            _drop_repeated_tail(english, en)
            english += en
    langs = list(dict.fromkeys(c.lang for c in chunks))
    return Transcript(source, english, langs)
