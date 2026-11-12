"""The review file: saved by the pipeline, edited in the review window, written back out."""
import numpy as np

from makesubs import pipeline, project
from makesubs.asr import SAMPLE_RATE as SR, Segment, Transcript, Word
from makesubs.pipeline import Options
from makesubs.project import Line, Project, Track
from makesubs.timecode import FrameRate


class FakeEngine:
    name = "fake"

    def unload(self):
        pass


def _seg(start, end, text, lang, prob=0.9):
    # numpy numbers, as real recognition gives them
    return Segment(np.float64(start), np.float64(end), text, lang,
                   [Word(np.float64(start), np.float64(end), " " + text, np.float32(prob))])


def _run(clip, tmp_path, monkeypatch, **kw):
    source = [_seg(0.2, 1.0, "Marhaba Nidal", "ar"), _seg(2.1, 2.8, "Wein Yasmin", "ar", 0.3)]
    english = [_seg(0.2, 1.0, "Hello Nidal", "en"), _seg(2.1, 2.8, "Where is Yasmin", "en", 0.3)]
    monkeypatch.setattr(pipeline.asr, "create_engine", lambda *a, **k: FakeEngine())
    monkeypatch.setattr(pipeline.asr, "transcribe", lambda *a, **k: Transcript(
        source, english, ["ar"], [(0, 3 * SR, "ar")]))
    monkeypatch.setattr(pipeline, "load_audio", lambda *a: np.zeros(3 * SR, np.float32))
    opts = Options(targets=["en"], spoken=["ar"], translator="whisper", out_dir=tmp_path,
                   formats=["srt", "avid"], **kw)
    job = pipeline.run([clip], opts, lambda s: None)[0]
    assert not job.error, job.error
    return job


def test_pipeline_saves_a_review_file_that_writes_the_same_subtitles(clip, tmp_path, monkeypatch):
    job = _run(clip, tmp_path, monkeypatch)
    assert job.review_file == tmp_path / f"{clip.stem}.makesubs.json"
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*") if p.suffix in (".srt", ".txt")
              and "review" not in p.name}
    assert set(before) == {f"{clip.stem}.{t}{s}" for t in ("transcript", "en")
                           for s in (".srt", ".avid.txt")}
    proj = project.load(job.review_file)
    assert [t.tag for t in proj.tracks] == ["transcript", "en"]
    assert proj.fps and proj.start_tc == "10:00:00:00"
    written = proj.export()
    assert {p.name: p.read_bytes() for p in written} == before  # untouched: identical files
    assert "[?] Where is Yasmin" in (tmp_path / f"{clip.stem}.en.srt").read_text("utf-8")


def test_checked_and_edited_lines_are_written_out(clip, tmp_path, monkeypatch):
    job = _run(clip, tmp_path, monkeypatch)
    proj = project.load(job.review_file)
    en = proj.track("en")
    line = en.lines[1]
    assert line.needs_check and not line.edited
    line.text, line.checked = "Where's Yasmin?", True
    assert line.edited
    proj.save()
    proj.export()
    srt = (tmp_path / f"{clip.stem}.en.srt").read_text("utf-8")
    assert "Where's Yasmin?" in srt and "[?]" not in srt
    again = project.load(job.review_file).track("en").lines[1]
    assert again.checked and again.text == "Where's Yasmin?" and again.machine_text == "Where is Yasmin"


def _proj(tmp_path, *tracks):
    clip = tmp_path / "A001.mov"
    return Project(clip=clip, out_dir=tmp_path, fps=FrameRate.parse("25"),
                   start_tc="01:00:00:00", tracks=list(tracks))


def test_hand_timing_wins_and_follows_on_aligned_tracks(tmp_path):
    src = Track("transcript", "", [Line(1.0, 2.0, "Marhaba", "ar"), Line(4.0, 5.0, "Yalla", "ar")])
    en = Track("en", "en", [Line(1.0, 2.0, "Hello", "en"), Line(4.0, 5.0, "Let's go", "en")])
    proj = _proj(tmp_path, src, en)
    assert proj.aligned(en, src)
    auto = proj.timed(en)
    assert auto[0].start == 1.0 and auto[0].end > 2.0  # lingers like the pipeline's
    proj.set_timing(en, 0, start=0.8, end=3.0)
    for t in (src, en):
        c = proj.timed(t)[0]
        assert (c.start, c.end) == (0.8, 3.0)
    proj.reset_timing(en, 0)
    assert proj.timed(en)[0].start == 1.0


def test_counterpart_by_time_when_tracks_differ(tmp_path):
    src = Track("transcript", "", [Line(1.0, 2.0, "a", "ar"), Line(2.5, 3.0, "b", "ar"),
                                   Line(6.0, 7.0, "c", "ar")])
    en = Track("en", "en", [Line(1.1, 2.9, "A and B", "en"), Line(6.1, 7.2, "C", "en")])
    proj = _proj(tmp_path, src, en)
    assert not proj.aligned(en, src)
    assert proj.counterpart(en, src, 1).text == "c"
    assert proj.counterpart(en, src, 0).text == "a"


def test_script_line_comes_from_the_track_that_was_compared(tmp_path):
    en = Track("en", "en", [Line(1.0, 2.0, "Hello", "en", script_ref="S1", script_match="changed",
                                 script_text="Nidal: Good morning")])
    da = Track("da", "da", [Line(1.0, 2.0, "Hej", "da")])
    proj = _proj(tmp_path, en, da)
    assert proj.script_line(da, 0).script_text == "Nidal: Good morning"
    assert proj.track("en").lines[0].needs_check
