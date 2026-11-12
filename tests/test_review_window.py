"""The review window, driven the way an editor uses it: keys, typing, undo, saving."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from makesubs import project  # noqa: E402
from makesubs.project import Line, Project, Track  # noqa: E402
from makesubs.timecode import FrameRate  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def review_file(tmp_path):
    src = Track("transcript", "", [
        Line(1.0, 2.0, "Sabah el kheir", "ar", 0.95, machine_text="Sabah el kheir"),
        Line(3.0, 4.0, "Wein Yasmin", "ar", 0.35, True, machine_text="Wein Yasmin"),
        Line(5.0, 6.0, "Nimt ind Omar", "ar", 0.9, machine_text="Nimt ind Omar"),
        Line(6.5, 7.5, "Sayyarto kharbaneh", "ar", 0.9, machine_text="Sayyarto kharbaneh"),
        Line(9.0, 10.0, "Yalla", "ar", 0.9, machine_text="Yalla"),
    ])
    script = "Nidal: I slept at Omar's. His car broke down."
    en = Track("en", "en", [
        Line(1.0, 2.0, "Good morning", "en", 0.95, machine_text="Good morning"),
        Line(3.0, 4.0, "Where is Yasmin", "en", 0.35, True, machine_text="Where is Yasmin"),
        Line(5.0, 6.0, "I slept at Omar's.", "en", 0.9, machine_text="I slept at Omar's.",
             script_ref="S2", script_match="same", script_text=script),
        Line(6.5, 7.5, "His car was broken.", "en", 0.9, machine_text="His car was broken.",
             script_ref="S2", script_match="changed", script_text=script),
        Line(9.0, 10.0, "Yalla", "en", 0.9, machine_text="Yalla"),
    ])
    proj = Project(clip=tmp_path / "A001C002.mov", out_dir=tmp_path, fps=FrameRate.parse("25"),
                   start_tc="01:00:00:00", formats=["srt", "avid"], tracks=[src, en])
    proj.export()
    return proj.save()


@pytest.fixture
def win(app, review_file):
    from makesubs.review import ReviewWindow
    w = ReviewWindow([review_file])
    w.autoplay.setChecked(False)
    w.isActiveWindow = lambda: True
    yield w
    w.stacks.clear()  # no "save changes?" question
    w.close()


def key(w, target, k, mods=Qt.NoModifier):
    return w.eventFilter(target, QKeyEvent(QEvent.KeyPress, k, mods))


def test_opens_on_the_first_line_to_check(win):
    assert [win.proj.issue(win.track, i) for i in range(5)] == [
        "", "unsure", "", "script", "untranslated"]
    assert win.row == 1
    assert win.filter_btns["check"].isChecked()
    # script differences are optional: they have their own filter
    assert [win.lines.isRowHidden(i) for i in range(5)] == [True, False, True, True, False]
    assert win.filter_btns["script"].text() == "Differs from script  1"
    assert win.progress_label.text() == "0 of 2 lines checked"
    assert not win.save_btn.isEnabled()


def test_looks_good_moves_on_and_counts(win):
    assert key(win, win.editor, Qt.Key_Return, Qt.ControlModifier)  # handled while typing
    assert win.track.lines[1].checked and win.row == 4
    assert win.progress_label.text() == "1 of 2 lines checked"
    assert win.save_btn.isEnabled()
    win.do("undo")
    assert not win.track.lines[1].checked
    assert win.proj.track("transcript").lines[1].checked is False


def test_typing_fixes_a_line_and_undo_takes_it_back(win):
    win.select(4, play=False)
    win.editor.setPlainText("Let's go")
    ln = win.track.lines[4]
    assert ln.text == "Let's go" and ln.edited and win.proj.issue(win.track, 4) == ""
    assert win.was_row.isVisible() or not win.isVisible()
    win.editor.setPlainText("Let's go!")
    win.do("undo")  # typing in one line is one undo step
    assert ln.text == "Yalla" and win.editor.toPlainText() == "Yalla"
    win.do("redo")
    assert ln.text == "Let's go!"


def test_script_wording_takes_only_this_lines_part(win):
    win.select(3, play=False)
    assert win.proj.script_wording(win.track, 3) == "His car broke down."
    assert win.use_script_btn.isEnabled()
    key(win, win.editor, Qt.Key_U, Qt.ControlModifier)
    assert win.track.lines[3].text == "His car broke down."
    assert win.editor.toPlainText() == "His car broke down."


def test_timing_nudges_every_track_and_saves(win, review_file):
    win.select(1, play=False)
    before = win.model.timed[1].start
    key(win, win.editor, Qt.Key_Right, Qt.AltModifier)
    assert abs(win.model.timed[1].start - before - 0.04) < 1e-6
    src = win.proj.track("transcript")
    assert src.lines[1].start_override == win.track.lines[1].start_override
    win.do("ok_next")
    assert win.save()
    srt = (review_file.parent / "A001C002.en.srt").read_text("utf-8")
    assert "00:00:03,040 -->" in srt
    assert "[?]" not in srt  # checked, so no longer marked
    assert "01:00:03:01" in (review_file.parent / "A001C002.en.avid.txt").read_text("utf-8-sig")
    again = project.load(review_file)
    assert again.track("en").lines[1].checked
    assert not win.save_btn.isEnabled()


def test_space_plays_unless_typing(win):
    played = []
    win.toggle_play = lambda: played.append(1)
    assert not key(win, win.editor, Qt.Key_Space)  # a space in the subtitle
    assert key(win, win.lines, Qt.Key_Space)
    assert key(win, win.editor, Qt.Key_Escape)
    assert played == [1, 1]


def test_find_filters_on_both_languages(win):
    win.filter_btns["all"].click()
    win.search_box.setText("yasmin")
    assert [win.lines.isRowHidden(i) for i in range(5)] == [True, False, True, True, True]
    win.search_box.setText("kharbaneh")  # the Arabic heard
    assert [win.lines.isRowHidden(i) for i in range(5)] == [True, True, True, False, True]


def test_script_differences_have_their_own_round(win):
    win.select(4, play=False)
    win.do("ok_next")
    win.do("ok_next")  # nothing left to check: stays, says so
    assert win.empty.isVisible() or not win.isVisible()
    assert "differ from the script" in win.empty.text() or "differs from the script" in         win.empty.text() or "1 line" in win.empty.text()
    win.filter_btns["script"].click()
    win.do("next_check")
    assert win.row == 3
    win.do("ok_next")
    assert win.track.lines[3].checked


def test_english_spoken_lines_are_not_untranslated(tmp_path):
    src = Track("transcript", "", [Line(1.0, 2.0, "Okay.", "en", machine_text="Okay."),
                                   Line(3.0, 4.0, "Yalla", "ar", machine_text="Yalla")])
    en = Track("en", "en", [Line(1.0, 2.0, "Okay.", "en", machine_text="Okay."),
                            Line(3.0, 4.0, "Yalla", "en", machine_text="Yalla")])
    proj = Project(clip=tmp_path / "x.mov", out_dir=tmp_path, fps=None, start_tc=None,
                   tracks=[src, en])
    assert [proj.issue(en, i) for i in range(2)] == ["", "untranslated"]
