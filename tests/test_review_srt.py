"""Reviewing without the original clip: its sound under another name, or finished .srt files."""
from pathlib import Path

import pytest

from makesubs import project
from makesubs.project import Line, Project, Track
from makesubs.timecode import FrameRate


@pytest.fixture
def made(tmp_path):
    """Subtitles as the pipeline leaves them: review file, .srt and Avid files, no clip."""
    src = Track("transcript", "", [
        Line(1.0, 2.0, "Sabah el kheir", "ar", 0.95, machine_text="Sabah el kheir"),
        Line(3.0, 4.0, "Wein Yasmin", "ar", 0.35, True, machine_text="Wein Yasmin"),
        Line(5.0, 6.0, "Yalla", "ar", 0.9, machine_text="Yalla"),
    ])
    en = Track("en", "en", [
        Line(1.0, 2.0, "Good morning", "en", 0.95, machine_text="Good morning"),
        Line(3.0, 4.0, "Where is Yasmin", "en", 0.35, True, machine_text="Where is Yasmin"),
        Line(5.0, 6.0, "Let's go", "en", 0.9, machine_text="Let's go"),
    ])
    proj = Project(clip=tmp_path / "A001C002.mov", out_dir=tmp_path, fps=FrameRate.parse("25"),
                   start_tc="01:00:00:00", formats=["srt", "avid"], tracks=[src, en])
    proj.export()
    proj.save()
    return tmp_path


def read(folder: Path, name: str) -> str:
    return (folder / name).read_text("utf-8-sig")


def test_sound_under_another_name_keeps_the_subtitle_names(made):
    wav = made / "ref_sound.wav"
    wav.write_bytes(b"")
    proj = project.load(made / "A001C002.makesubs.json")
    proj.attach(wav)
    proj.track("en").lines[2].text = "Come on"
    names = sorted(p.name for p in proj.export())
    assert names == ["A001C002.en.avid.txt", "A001C002.en.srt", "A001C002.transcript.avid.txt",
                     "A001C002.transcript.srt"]
    assert "Come on" in read(made, "A001C002.en.srt")
    proj.save()
    again = project.load(made / "A001C002.makesubs.json")
    assert again.stem == "A001C002" and again.clip == wav
    assert again.title == "A001C002  (ref_sound.wav)"


def test_sound_with_the_same_name_is_found(made):
    (made / "A001C002.wav").write_bytes(b"")
    assert project.load(made / "A001C002.makesubs.json").clip == made / "A001C002.wav"
    (made / "A001C002.mov").write_bytes(b"")  # picture beats sound
    assert project.load(made / "A001C002.makesubs.json").clip == made / "A001C002.mov"


def test_srt_opens_with_its_other_languages(made):
    (made / "A001C002.makesubs.json").unlink()
    before = {n: read(made, n) for n in ("A001C002.en.srt", "A001C002.transcript.srt",
                                         "A001C002.en.avid.txt")}
    proj = project.open_any(made / "A001C002.en.srt")
    assert proj.timing == "screen" and proj.stem == "A001C002"
    assert [t.tag for t in proj.tracks] == ["transcript", "en"]
    en = proj.track("en")
    assert [ln.text for ln in en.lines] == ["Good morning", "Where is Yasmin", "Let's go"]
    assert [ln.flagged for ln in en.lines] == [False, True, False]  # from the [?]
    assert proj.issue(en, 1) == "unsure"
    # opened timing is on-screen timing: kept exactly, nothing written when nothing changed
    assert [c.start for c in proj.timed(en)] == [ln.start for ln in en.lines]
    assert proj.export() == []
    # checking the unsure line takes its [?] away, and nothing else changes
    for t in proj.tracks:
        t.lines[1].checked = True
    written = sorted(p.name for p in proj.export())
    assert written == ["A001C002.en.srt", "A001C002.transcript.srt"]  # no fps: no Avid
    for n in written:
        assert read(made, n) == before[n].replace("[?] ", "")
    assert read(made, "A001C002.en.avid.txt") == before["A001C002.en.avid.txt"]


def test_srt_with_a_clip_writes_the_avid_file_again(made):
    (made / "A001C002.makesubs.json").unlink()
    before = read(made, "A001C002.en.avid.txt")
    proj = project.open_any(made / "A001C002.en.srt")
    proj.fps = FrameRate.parse("25")  # what attaching the camera clip gives
    proj._avid_from_files()
    assert proj.avid_start == "01:00:00:00" and "avid" in proj.formats
    proj.track("en").lines[1].checked = True
    proj.track("transcript").lines[1].checked = True
    proj.export()
    assert read(made, "A001C002.en.avid.txt") == before.replace("[?] ", "")


def test_srt_with_a_review_file_opens_the_review_file(made):
    proj = project.open_any(made / "A001C002.en.srt")
    assert proj.timing == "speech" and proj.path == made / "A001C002.makesubs.json"


def test_picked_files_are_paired(made):
    (made / "A001C002.makesubs.json").unlink()
    wav = made / "ref_sound.wav"
    wav.write_bytes(b"")
    other = made / "B003C001.mov"
    other.write_bytes(b"")
    projects, problems = project.open_many([made / "A001C002.en.srt", wav])
    assert len(projects) == 1 and projects[0].clip == wav and problems == []
    # both languages of one clip open once; a clip with no subtitles is said
    projects, problems = project.open_many(
        [made / "A001C002.en.srt", made / "A001C002.transcript.srt", wav, other])
    assert len(projects) == 1
    assert problems == ["ref_sound.wav: no subtitles with that name",
                        "B003C001.mov: no subtitles with that name"]


def test_srt_from_another_tool(tmp_path):
    f = tmp_path / "Film.srt"
    f.write_bytes("﻿1\r\n00:00:01.5 --> 00:00:02,250\r\nFirst line\r\nsecond line\r\n\r\n"
                  "2\r\n00:01:00,000 --> 00:01:01,000\r\n[?] Hard to hear\r\n".encode("utf-8"))
    proj = project.open_any(f)
    t = proj.tracks[0]
    assert (t.tag, proj.stem) == ("", "Film")
    assert [(ln.start, ln.end, ln.text, ln.flagged) for ln in t.lines] == [
        (1.5, 2.25, "First line second line", False), (60.0, 61.0, "Hard to hear", True)]
    t.lines[0].text = "First line, second line"
    assert [p.name for p in proj.export()] == ["Film.srt"]
    assert "First line, second line" in f.read_text("utf-8")
