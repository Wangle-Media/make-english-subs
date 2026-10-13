import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from makesubs.asr import Segment, Word, _drop_repeated_tail
from makesubs.audio import load_audio
from makesubs.pipeline import Options, output_path, planned_outputs
from makesubs.probe import probe
from makesubs.shape import MAX_LINE, Cue, build_cues, finalize, wrap
from makesubs.timecode import FrameRate, frames_to_tc, seconds_to_frames, tc_to_frames
from makesubs.writers import write_srt, write_subcap

DF = FrameRate.parse("30000/1001", drop_frame=True)
NDF24 = FrameRate.parse("24000/1001")


@pytest.mark.parametrize("tc,frames", [
    ("00:00:00;00", 0), ("00:00:59;29", 1799), ("00:01:00;02", 1800),
    ("00:10:00;00", 17982), ("01:00:00;00", 107892), ("09:59:58;00", 1078860),
])
def test_drop_frame_known_values(tc, frames):
    assert tc_to_frames(tc, DF) == frames
    assert frames_to_tc(frames, DF) == tc


def test_drop_frame_roundtrip_every_frame_for_11_minutes():
    for f in range(0, 30 * 60 * 11):
        assert tc_to_frames(frames_to_tc(f, DF), DF) == f


def test_non_drop_roundtrip():
    assert frames_to_tc(tc_to_frames("14:23:11:05", NDF24), NDF24) == "14:23:11:05"
    assert FrameRate.parse("25/1", drop_frame=True).drop_frame is False


def test_wrap_balances_and_never_drops_text():
    assert wrap("short") == "short"
    text = "We are able to differentiate different cultures and origins"
    lines = wrap(text).split("\n")
    assert len(lines) == 2 and all(len(x) <= MAX_LINE for x in lines)
    long = "word " * 40
    assert wrap(long).replace("\n", " ").split() == long.split()


def _words(text, start=0.0, step=0.4, prob=0.9):
    return [Word(start + i * step, start + i * step + 0.3, " " + w, prob)
            for i, w in enumerate(text.split())]


def test_build_cues_splits_long_runs_evenly():
    words = _words("one two three four five six seven eight nine ten " * 4)
    cues = build_cues([Segment(0, 20, "", "en", words)])
    assert len(cues) >= 2
    lengths = [len(c.text) for c in cues]
    assert max(lengths) <= 84 and min(lengths) > 20  # no tiny leftovers


def test_build_cues_breaks_on_pause_and_language():
    a = Segment(0, 2, "", "en", _words("hello there friend"))
    b = Segment(5, 7, "", "ar", _words("مرحبا يا صديقي", start=5))
    cues = build_cues([a, b])
    assert [c.lang for c in cues] == ["en", "ar"]


def test_drop_repeated_tail():
    prev = [Segment(0, 3, "", "en", _words("we are able to differentiate through the slightest"))]
    new = [Segment(3, 6, "", "en", _words("through the slightest words", start=3))]
    _drop_repeated_tail(prev, new)
    assert prev[0].text == "we are able to differentiate"


def test_finalize_frame_accurate_with_gaps():
    cues = [Cue(0.01, 0.3, "a", "en", 1), Cue(0.9, 1.5, "b", "en", 1), Cue(1.55, 4, "c", "en", 1)]
    out = finalize(cues, NDF24)
    for c in out:
        for t in (c.start, c.end):
            f = t * float(NDF24.rate)
            assert abs(f - round(f)) < 1e-6
    for a, b in zip(out, out[1:]):
        assert seconds_to_frames(b.start, NDF24) - seconds_to_frames(a.end, NDF24) >= 2
        assert a.end > a.start


def test_writers(tmp_path):
    cues = [Cue(1.0, 2.5, "مرحبا", "ar", 0.9), Cue(3.0, 4.0, "hmm", "en", 0.3, flagged=True)]
    srt = tmp_path / "x.srt"
    write_srt(srt, cues)
    text = srt.read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:02,500\nمرحبا" in text and "[?] hmm" in text
    sub = tmp_path / "x.avid.txt"
    write_subcap(sub, cues, DF, "09:59:58;00")
    raw = sub.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw
    body = raw.decode("utf-8-sig")
    assert "<begin subtitles>" in body and "<end subtitles>" in body
    assert "09:59:59;00 10:00:00;15" in body  # 1.0s -> +30 frames; 2.5s -> +75 frames


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("media") / "A001C001.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "3", "-ac", "2", "-c:v", "mpeg4", "-c:a", "pcm_s16le",
                    "-timecode", "10:00:00:00", str(path)], check=True)
    return path


def test_probe_and_audio_leave_source_untouched(clip):
    before = hashlib.sha256(clip.read_bytes()).hexdigest()
    info = probe(clip)
    assert info.start_tc == "10:00:00:00" and info.fps.nominal == 25
    assert info.total_channels == 2
    for ch in ("mix", "1", "2"):
        audio = load_audio(info, ch)
        assert 2.9 * 16000 < audio.size < 3.1 * 16000
    with pytest.raises(ValueError):
        load_audio(info, "3")
    assert hashlib.sha256(clip.read_bytes()).hexdigest() == before


def test_output_paths_are_sidecars(clip):
    info = probe(clip)
    outs = planned_outputs(info, Options(targets=["en", "da"], formats=["srt", "avid"]))
    names = sorted(p.name for p in outs)
    assert names == sorted(["A001C001.transcript.srt", "A001C001.transcript.avid.txt",
                            "A001C001.en.srt", "A001C001.en.avid.txt",
                            "A001C001.da.srt", "A001C001.da.avid.txt"])
    assert all(p.parent == clip.parent for p in outs)
    assert output_path(info, Options(), "en", "srt") != clip


def test_model_dir_avoids_non_ascii_user_folder_on_windows(monkeypatch):
    from huggingface_hub import constants

    from makesubs import asr
    monkeypatch.setattr(asr.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")
    monkeypatch.setattr(constants, "HF_HUB_CACHE", r"C:\Users\Søren\.cache\huggingface\hub")
    assert asr._model_dir().isascii()
    monkeypatch.setattr(constants, "HF_HUB_CACHE", r"C:\Users\Soren\.cache\huggingface\hub")
    assert asr._model_dir() is None
    monkeypatch.setattr(asr.sys, "platform", "darwin")
    monkeypatch.setattr(constants, "HF_HUB_CACHE", "/Users/Søren/.cache/huggingface/hub")
    assert asr._model_dir() is None
