"""Dialect speech models: a second pass over the stretches in their language."""
import numpy as np
import pytest

from makesubs import asr, pipeline
from makesubs.asr import SAMPLE_RATE as SR, Segment, Transcript, Word
from makesubs.pipeline import Options

LEVANTINE = asr.DIALECTS["levantine"][1]


class FakeEngine:
    """Hands back '<model>:<lang>' for every stretch it is given, and logs loads."""
    def __init__(self, model, events):
        self.model, self.events, self.name = model, events, f"fake {model}"
        events.append(("load", model))

    def run(self, audio, lang, task):
        label = f"{self.model}:{lang}"
        return [Segment(0.0, 1.0, label, lang, [Word(0.0, 1.0, " " + label, 0.9)])]

    def unload(self):
        self.events.append(("unload", self.model))


def _seg(start, text, lang):
    return Segment(start, start + 1, text, lang, [Word(start, start + 1, " " + text, 0.9)])


def test_retranscribe_replaces_only_the_dialect_stretches():
    t = Transcript([_seg(0.5, "Action!", "en"), _seg(6.5, "Cut!", "en")], [], ["en", "ar"],
                   [(0, 2 * SR, "en"), (3 * SR, 5 * SR, "ar"), (6 * SR, 8 * SR, "en")])
    asr.retranscribe(FakeEngine("dialect", []), np.zeros(9 * SR, np.float32), t, {"ar"},
                     progress=lambda s: None)
    assert [(s.text, s.start) for s in t.source] == [
        ("Action!", 0.5), ("dialect:ar", 3.0), ("Cut!", 6.5)]


def test_dialect_model_transcribes_only_its_language(clip, tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(pipeline.asr, "create_engine",
                        lambda backend, model=None: FakeEngine(model or "large-v3", events))
    seen_later = []

    def transcribe(engine, audio, spoken, want, progress, later=frozenset()):
        seen_later.append(later)  # the main model leaves the Arabic to the dialect model
        return Transcript([_seg(0.5, "Action!", "en")], [], ["en", "ar"],
                          [(0, 2 * SR, "en"), (3 * SR, 5 * SR, "ar")])

    monkeypatch.setattr(pipeline.asr, "transcribe", transcribe)
    monkeypatch.setattr(pipeline, "load_audio", lambda *a: np.zeros(6 * SR, np.float32))
    opts = Options(targets=["en"], spoken=["ar", "en"], translator="whisper",
                   dialect="levantine", out_dir=tmp_path)
    job = pipeline.run([clip], opts, lambda s: None)[0]
    assert not job.error, job.error
    assert seen_later == [frozenset({"ar"})]
    assert [s.text for s in job.transcript.source] == ["Action!", f"{LEVANTINE}:ar"]
    # one model in memory at a time
    assert events == [("load", "large-v3"), ("unload", "large-v3"),
                      ("load", LEVANTINE), ("unload", LEVANTINE)]
    srt = (tmp_path / f"{clip.stem}.transcript.srt").read_text(encoding="utf-8")
    assert "Action!" in srt and f"{LEVANTINE}:ar" in srt


def test_dialect_model_not_loaded_without_its_language(clip, tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(pipeline.asr, "create_engine",
                        lambda backend, model=None: FakeEngine(model or "large-v3", events))
    monkeypatch.setattr(pipeline.asr, "transcribe", lambda *a, **k: Transcript(
        [_seg(0.5, "Action!", "en")], [], ["en"], [(0, 2 * SR, "en")]))
    monkeypatch.setattr(pipeline, "load_audio", lambda *a: np.zeros(3 * SR, np.float32))
    opts = Options(targets=["en"], spoken=["ar", "en"], translator="whisper",
                   dialect="levantine", out_dir=tmp_path)
    pipeline.run([clip], opts, lambda s: None)
    assert events == [("load", "large-v3"), ("unload", "large-v3")]  # no Arabic, no 2nd pass


def test_dialect_needs_its_language():
    with pytest.raises(RuntimeError, match="for Arabic, which isn't among the spoken"):
        pipeline.validate(Options(spoken=["da"], translator="whisper", dialect="levantine"))
    with pytest.raises(RuntimeError, match="Unknown dialect model"):
        pipeline.validate(Options(spoken=["ar"], translator="whisper", dialect="egyptian"))
