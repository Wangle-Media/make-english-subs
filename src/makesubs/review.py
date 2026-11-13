"""The review window: watch each line, fix what is wrong, save the subtitle files again.

Layout: the clip with the subtitle burned in and a timeline under it on the left; the lines
on the right, the selected one open for editing under them. The lines worth a look (unsure
recognition, or worded differently from the script) are listed first, and one key marks a
line as fine and moves on to the next one, playing it.
"""
from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import (
    QAbstractListModel, QEvent, QKeyCombination, QModelIndex, QObject, QPointF, QRect, QRectF,
    QSize, Qt, QThread, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPalette, QPen,
    QTextCursor, QTextOption, QUndoCommand, QUndoStack,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListView, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSplitter, QStyle,
    QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget,
)

from . import languages, project
from .project import Line, Project, Track
from .shape import MAX_LINE, READING_CPS, wrap
from .timecode import frames_to_tc, seconds_to_frames, tc_to_frames

try:  # playback is in PySide6-Addons; without it the window still works, just silent
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
except ImportError:  # pragma: no cover
    QMediaPlayer = None

MAC = sys.platform == "darwin"
MUST = project.MUST
FILTERS = (("check", "To check"), ("script", "Differs from script"), ("mine", "Changed by me"),
           ("all", "All lines"))

# Status colours: readable on light and dark backgrounds.
AMBER, BLUE, GREEN, PURPLE, RED = (QColor("#e0a100"), QColor("#3d8bfd"), QColor("#2fa866"),
                                   QColor("#9b6bdf"), QColor("#e5534b"))


@dataclass(frozen=True)
class Status:
    key: str
    label: str
    color: QColor | None
    hint: str


STATUSES = {
    "untranslated": Status("untranslated", "Not translated", RED,
                           "The translator left this line as it was spoken. Type the "
                           "translation, or fix what was heard."),
    "unsure": Status("unsure", "Unsure", AMBER,
                     "Speech recognition wasn't sure of this line: noise, overlap or off-mic. "
                     "Listen and check the subtitle says what is said."),
    "script": Status("script", "Differs from script", BLUE,
                     "The actor said this differently from the script, so it was translated "
                     "as spoken. If they did say the scripted line, use the script's wording."),
    "fixed": Status("fixed", "Fixed", GREEN, "You changed this line."),
    "checked": Status("checked", "Checked", GREEN, "You checked this line."),
    "plain": Status("plain", "", None, ""),
}


def status_of(ln: Line, issue: str) -> Status:
    if ln.checked or ln.edited:
        return STATUSES["fixed" if ln.edited or ln.retimed else "checked"]
    return STATUSES[issue or "plain"]


def is_rtl(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("\u0590" <= c <= "\u08ff" for c in letters) > len(letters) / 2


def tint(color: QColor, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def key_text(seq: str) -> str:
    return QKeySequence(seq).toString(QKeySequence.NativeText)


# --------------------------------------------------------------------------------------------
# Keys. Handled before the focused widget sees them, so they work while typing.

KEYS = [  # (sequence(s), action, description)
    (["Esc"], "play", "Play / pause"),
    (["Ctrl+R"], "play_line", "Play this line again"),
    (["Ctrl+Return", "Ctrl+Enter"], "ok_next", "Looks good: next line to check"),
    (["Ctrl+Down"], "next_check", "Next line to check"),
    (["Ctrl+Up"], "prev_check", "Previous line to check"),
    (["Alt+Down"], "next_line", "Next line"),
    (["Alt+Up"], "prev_line", "Previous line"),
    (["Ctrl+U"], "use_script", "Use the script's wording"),
    (["Alt+Left"], "start_earlier", "Start one frame earlier"),
    (["Alt+Right"], "start_later", "Start one frame later"),
    (["Alt+Shift+Left"], "end_earlier", "End one frame earlier"),
    (["Alt+Shift+Right"], "end_later", "End one frame later"),
    (["Ctrl+I"], "start_here", "Start at the playhead"),
    (["Ctrl+O"], "end_here", "End at the playhead"),
    (["Ctrl+Z"], "undo", "Undo"),
    (["Ctrl+Shift+Z", "Ctrl+Y"], "redo", "Redo"),
    (["Ctrl+S"], "save", "Save the subtitle files"),
    (["Ctrl+F"], "find", "Find text"),
    (["F1", "Ctrl+/"], "keys", "Show these keys"),
]


def _combo(seq: str) -> int:
    return QKeySequence(seq)[0].toCombined()


KEYMAP = {_combo(s): action for seqs, action, _ in KEYS for s in seqs}


# --------------------------------------------------------------------------------------------
# Undo: every change snapshots the lines it touches.

class Change(QUndoCommand):
    def __init__(self, win: "ReviewWindow", proj: Project, label: str, before, after, key=None):
        super().__init__(label)
        self.win, self.proj, self.before, self.after, self.key = win, proj, before, after, key
        self.first = True

    def id(self):  # typing into one line merges into one undo step
        return 1 if self.key else -1

    def mergeWith(self, other):
        if other.key != self.key or other.proj is not self.proj:
            return False
        self.after = other.after
        return True

    def _apply(self, snap):
        for (ti, li), ln in snap.items():  # in place: the line objects stay the same
            vars(self.proj.tracks[ti].lines[li]).update(vars(ln))
        self.win.changed(self.proj, sorted({li for _, li in snap}))

    def redo(self):
        if self.first:  # already applied when it was pushed
            self.first = False
            return
        self._apply(self.after)

    def undo(self):
        self._apply(self.before)


# --------------------------------------------------------------------------------------------
# The line list.

class LineModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.proj: Project | None = None
        self.track: Track | None = None
        self.timed = []

    def reset(self, proj, track):
        self.beginResetModel()
        self.proj, self.track = proj, track
        self.timed = proj.timed(track) if proj else []
        self.endResetModel()

    def refresh(self, rows=None):
        if not self.proj:
            return
        self.timed = self.proj.timed(self.track)
        if self.rowCount():
            a, b = (min(rows), max(rows)) if rows else (0, self.rowCount() - 1)
            self.dataChanged.emit(self.index(max(0, a - 1)),
                                  self.index(min(self.rowCount() - 1, b + 1)))
            self.layoutChanged.emit()  # heights change with the text

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or not self.track else len(self.track.lines)

    def data(self, index, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and self.track:
            return self.track.lines[index.row()].text
        return None


class LineDelegate(QStyledItemDelegate):
    PAD = 10

    def __init__(self, win: "ReviewWindow"):
        super().__init__()
        self.win = win
        self.small = QFont()
        self.small.setPointSizeF(self.small.pointSizeF() * 0.86)
        self.text_font = QFont()
        self.text_font.setPointSizeF(self.text_font.pointSizeF() * 1.08)

    def _parts(self, row):
        w = self.win
        track, proj = w.model.track, w.model.proj
        ln = track.lines[row]
        src = proj.counterpart(track, w.source_track, row) if w.source_track else None
        return ln, (src.text if src and src is not ln else ""), w.model.timed[row]

    def _heights(self, inner, source, text):
        fs, ft = QFontMetrics(self.small), QFontMetrics(self.text_font)
        flags = Qt.TextWordWrap
        hs = fs.boundingRect(QRect(0, 0, inner, 10_000), flags, source).height() if source else 0
        ht = ft.boundingRect(QRect(0, 0, inner, 10_000), flags, text or "—").height()
        return fs.height(), hs, ht

    def sizeHint(self, option, index):
        width = self.win.lines.viewport().width()
        ln, source, _ = self._parts(index.row())
        head, hs, ht = self._heights(width - 2 * self.PAD - 12, source, ln.text)
        return QSize(width, self.PAD + head + 4 + (hs + 3 if hs else 0) + ht + self.PAD + 6)

    def paint(self, p: QPainter, option, index):
        row = index.row()
        ln, source, cue = self._parts(row)
        w = self.win
        st = status_of(ln, w.proj.issue(w.model.track, row))
        pal = w.palette()
        r = QRectF(option.rect).adjusted(3, 3, -3, -3)
        selected = bool(option.state & QStyle.State_Selected)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        base = pal.color(QPalette.Base)
        bg = tint(pal.color(QPalette.Highlight), 46) if selected else base
        p.setPen(QPen(pal.color(QPalette.Highlight) if selected
                      else tint(pal.color(QPalette.Text), 28), 1.2 if selected else 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, 7, 7)
        if st.color is not None:  # status stripe
            path = QPainterPath()
            path.addRoundedRect(QRectF(r.left(), r.top(), 5, r.height()), 2.5, 2.5)
            p.fillPath(path, st.color)

        x = int(r.left()) + self.PAD + 4
        inner = int(r.width()) - 2 * self.PAD - 6
        y = int(r.top()) + self.PAD - 3
        fs = QFontMetrics(self.small)
        dim = tint(pal.color(QPalette.Text), 150)
        p.setFont(self.small)
        p.setPen(dim)
        head = f"{row + 1}    {w.tc(cue.start)}    {cue.end - cue.start:.1f} s"
        p.drawText(QRect(x, y, inner, fs.height()), Qt.AlignLeft | Qt.AlignVCenter, head)
        if st.label:
            chip_w = fs.horizontalAdvance(st.label) + 16
            chip = QRectF(x + inner - chip_w, y - 1, chip_w, fs.height() + 2)
            p.setPen(Qt.NoPen)
            p.setBrush(tint(st.color, 50))
            p.drawRoundedRect(chip, chip.height() / 2, chip.height() / 2)
            p.setPen(st.color.darker(125) if pal.color(QPalette.Base).lightness() > 128
                     else st.color.lighter(130))
            p.drawText(chip, Qt.AlignCenter, st.label)
        y += fs.height() + 4
        _, hs, ht = self._heights(inner, source, ln.text)
        if source:
            opt = QTextOption(Qt.AlignRight if is_rtl(source) else Qt.AlignLeft)
            opt.setWrapMode(QTextOption.WordWrap)
            if is_rtl(source):
                opt.setTextDirection(Qt.RightToLeft)
            p.setPen(dim)
            p.drawText(QRectF(x, y, inner, hs), source, opt)
            y += hs + 3
        p.setFont(self.text_font)
        p.setPen(pal.color(QPalette.Text))
        opt = QTextOption(Qt.AlignRight if is_rtl(ln.text) else Qt.AlignLeft)
        opt.setWrapMode(QTextOption.WordWrap)
        p.drawText(QRectF(x, y, inner, ht), ln.text or "—", opt)
        p.restore()


# --------------------------------------------------------------------------------------------
# Picture.

class VideoView(QWidget):
    """The clip, with the subtitle at the playhead drawn over it."""
    find_clip = Signal()

    def __init__(self):
        super().__init__()
        self.image = None
        self.subtitle = ""
        self.flag = False
        self.message = ""
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.find_btn = QPushButton("Find the clip or sound…", self)
        self.find_btn.hide()
        self.find_btn.clicked.connect(self.find_clip)

    def set_frame(self, frame):
        img = frame.toImage() if frame.isValid() else None
        if img is not None and not img.isNull():
            self.image = img
            self.update()

    def set_subtitle(self, text: str, flag: bool):
        if (text, flag) != (self.subtitle, self.flag):
            self.subtitle, self.flag = text, flag
            self.update()

    def set_message(self, text: str, can_find: bool = False):
        self.message = text
        self.find_btn.setVisible(can_find)
        self.update()

    def resizeEvent(self, e):
        self.find_btn.adjustSize()
        self.find_btn.move((self.width() - self.find_btn.width()) // 2, self.height() // 2 + 24)

    def picture_rect(self) -> QRectF:
        if self.image is None:
            w, h = self.width(), self.width() * 9 / 16
            if h > self.height():
                h, w = self.height(), self.height() * 16 / 9
        else:
            s = min(self.width() / self.image.width(), self.height() / self.image.height())
            w, h = self.image.width() * s, self.image.height() * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#0d0d0f"))
        r = self.picture_rect()
        if self.image is not None:
            p.drawImage(r, self.image)
        else:
            p.fillRect(r, QColor("#17181b"))
        if self.message:
            p.setPen(QColor("#c9c9cf"))
            p.drawText(r.adjusted(24, 0, -24, -20), Qt.AlignCenter | Qt.TextWordWrap, self.message)
        if self.subtitle:
            self._draw_subtitle(p, r)

    def _draw_subtitle(self, p: QPainter, r: QRectF):
        f = QFont()
        f.setPixelSize(max(12, int(r.height() / 17)))
        f.setWeight(QFont.DemiBold)
        fm = QFontMetrics(f)
        lines = wrap(self.subtitle).split("\n")
        if self.flag:
            lines[0] = "[?] " + lines[0]
        lh = fm.height()
        y = r.bottom() - r.height() * 0.07 - lh * len(lines)
        for line in lines:
            path = QPainterPath()
            x = r.center().x() - fm.horizontalAdvance(line) / 2
            path.addText(QPointF(x, y + fm.ascent()), f, line)
            p.strokePath(path, QPen(QColor(0, 0, 0, 220), max(2.0, f.pixelSize() / 7),
                                    Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.fillPath(path, QColor("#ffd54a") if self.flag else QColor("white"))
            y += lh


# --------------------------------------------------------------------------------------------
# Timeline: waveform, the lines as bars, the playhead. Drag a bar's ends to retime it.

class Timeline(QWidget):
    seek = Signal(float)
    picked = Signal(int)
    drag_started = Signal(int)
    dragged = Signal(int, object, object)   # row, start or None, end or None
    drag_finished = Signal(int)

    EDGE = 6

    def __init__(self):
        super().__init__()
        self.setFixedHeight(150)
        self.setMouseTracking(True)
        self.label = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"   # ruler text
        self.span = 12.0            # seconds shown
        self.pos_s = 0.0            # playhead
        self.cues = []
        self.statuses = []
        self.selected = -1
        self.peaks = None           # (peaks per second, array)
        self.frame = 0.04
        self.duration = 0.0
        self.drag = None            # (row, "start"|"end"|"move", grab offset)
        self.setToolTip("Click to move the playhead. Drag the ends of a line to change "
                        "when it shows. Scroll to zoom.")

    def set_data(self, cues, statuses, selected, frame, duration):
        self.cues, self.statuses, self.selected = cues, statuses, selected
        self.frame, self.duration = frame, duration
        self.update()

    def set_pos(self, s: float):
        self.pos_s = s
        self.update()

    def view(self):
        left = self.pos_s - self.span * 0.35
        return left, left + self.span

    def x_of(self, s):
        left, right = self.view()
        return (s - left) / (right - left) * self.width()

    def s_of(self, x):
        left, right = self.view()
        return left + x / max(1, self.width()) * (right - left)

    def snap(self, s):
        return round(s / self.frame) * self.frame

    def wheelEvent(self, e):
        f = 0.85 if e.angleDelta().y() > 0 else 1 / 0.85
        self.span = min(90.0, max(3.0, self.span * f))
        self.update()

    def _hit(self, x, y):
        bar_top, bar_h = 8, 40
        if not (bar_top - 4 <= y <= bar_top + bar_h + 4):
            return None
        for i, c in enumerate(self.cues):
            a, b = self.x_of(c.start), self.x_of(c.end)
            if a - self.EDGE <= x <= b + self.EDGE:
                if abs(x - a) <= self.EDGE and i == self.selected:
                    return i, "start"
                if abs(x - b) <= self.EDGE and i == self.selected:
                    return i, "end"
                if a <= x <= b:
                    return i, "pick"
        return None

    def mousePressEvent(self, e):
        x, y = e.position().x(), e.position().y()
        hit = self._hit(x, y)
        if hit and hit[1] in ("start", "end"):
            self.drag = (hit[0], hit[1])
            self.drag_started.emit(hit[0])
        elif hit:
            self.picked.emit(hit[0])
        else:
            self.seek.emit(max(0.0, self.s_of(x)))

    def mouseMoveEvent(self, e):
        x, y = e.position().x(), e.position().y()
        if self.drag:
            row, which = self.drag
            s = self.snap(max(0.0, self.s_of(x)))
            self.dragged.emit(row, s if which == "start" else None, s if which == "end" else None)
            return
        if e.buttons() & Qt.LeftButton:
            self.seek.emit(max(0.0, self.s_of(x)))
            return
        hit = self._hit(x, y)
        self.setCursor(Qt.SizeHorCursor if hit and hit[1] in ("start", "end")
                       else Qt.PointingHandCursor if hit else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if self.drag:
            row = self.drag[0]
            self.drag = None
            self.drag_finished.emit(row)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), pal.color(QPalette.Base))
        text = pal.color(QPalette.Text)
        left, right = self.view()

        # seconds ruler
        p.setPen(tint(text, 40))
        f = QFont()
        f.setPointSizeF(f.pointSizeF() * 0.8)
        p.setFont(f)
        step = 2 if self.span <= 15 else 5 if self.span <= 40 else 10
        s = int(max(0, left)) // step * step
        while s <= right:
            x = self.x_of(s)
            p.drawLine(int(x), h - 14, int(x), h - 8)
            p.setPen(tint(text, 110))
            p.drawText(int(x) + 3, h - 3, self.label(s))
            p.setPen(tint(text, 40))
            s += step

        # waveform
        wave_top, wave_h = 56.0, h - 56.0 - 22
        if self.peaks is not None:
            rate, peaks = self.peaks
            mid = wave_top + wave_h / 2
            p.setPen(tint(text, 70))
            for x in range(0, w, 2):
                a = int(max(0, self.s_of(x)) * rate)
                b = int(max(0, self.s_of(x + 2)) * rate) + 1
                if a >= len(peaks):
                    break
                v = float(peaks[a:b].max()) if b > a else 0.0
                p.drawLine(QPointF(x, mid - v * wave_h / 2), QPointF(x, mid + v * wave_h / 2))

        # end of the clip
        if self.duration:
            x = self.x_of(self.duration)
            if x < w:
                p.fillRect(QRectF(x, 0, w - x, h), tint(text, 18))

        # lines
        bar_top, bar_h = 8.0, 40.0
        for i, c in enumerate(self.cues):
            a, b = self.x_of(c.start), self.x_of(c.end)
            if b < 0 or a > w:
                continue
            st = self.statuses[i]
            color = st.color or tint(text, 120)
            sel = i == self.selected
            rect = QRectF(a, bar_top, max(2.0, b - a), bar_h)
            p.setPen(QPen(pal.color(QPalette.Highlight), 2) if sel else Qt.NoPen)
            p.setBrush(tint(color, 120 if sel else 60))
            p.drawRoundedRect(rect, 4, 4)
            p.setPen(text)
            p.drawText(rect.adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetrics(f).elidedText(c.text.replace("\n", " "), Qt.ElideRight,
                                                  int(max(0, rect.width() - 10))))
            if sel:  # handles
                p.setBrush(pal.color(QPalette.Highlight))
                p.setPen(Qt.NoPen)
                for x in (a, b):
                    p.drawRoundedRect(QRectF(x - 3, bar_top + bar_h / 2 - 9, 6, 18), 3, 3)

        # playhead
        x = self.x_of(self.pos_s)
        p.setPen(QPen(RED, 2))
        p.drawLine(QPointF(x, 0), QPointF(x, h))


# --------------------------------------------------------------------------------------------
# Background work: the waveform.

class WaveLoader(QObject):
    done = Signal(object, object)   # project, (rate, peaks)

    def __init__(self, proj: Project):
        super().__init__()
        self.proj = proj

    def run(self):
        try:
            from .audio import SAMPLE_RATE, load_audio
            from .probe import probe
            audio = load_audio(probe(self.proj.clip), self.proj.channel)
            per = SAMPLE_RATE // 100
            n = len(audio) // per
            peaks = np.abs(audio[: n * per]).reshape(n, per).max(axis=1)
            top = float(np.percentile(peaks, 99.5)) or 1.0
            self.done.emit(self.proj, (100, np.clip(peaks / top, 0, 1) ** 0.7))
        except Exception:  # noqa: BLE001 - no waveform is fine
            self.done.emit(self.proj, None)


# --------------------------------------------------------------------------------------------
# The editor for the selected line.

class SubtitleEdit(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        f = QFont()
        f.setPointSizeF(f.pointSizeF() * 1.25)
        self.setFont(f)
        self.setTabChangesFocus(True)
        self.setFixedHeight(QFontMetrics(f).lineSpacing() * 3 + 18)
        self.setPlaceholderText("No subtitle for this line")


def _chip(text: str, color: QColor | None) -> str:
    if not text or color is None:
        return ""
    return (f"<span style='background:{tint(color, 60).name(QColor.HexArgb)};"
            f"color:{color.name()};'>&nbsp;{text}&nbsp;</span>")


class ReviewWindow(QMainWindow):
    closed = Signal()

    def __init__(self, files: list[Path], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.stacks: dict[int, QUndoStack] = {}
        self.saved_index: dict[int, int] = {}
        self.projects, problems = project.open_many([Path(f) for f in files])
        if problems:
            QMessageBox.warning(self, "MakeSubs", "Can't open everything:\n\n"
                                + "\n".join(problems))
        self.proj: Project | None = None
        self.track: Track | None = None
        self.source_track: Track | None = None
        self.row = -1
        self.waves: dict[int, object] = {}
        self.threads: list[QThread] = []
        self.stop_at: float | None = None
        self.drag_before = None
        self._loading = False

        self.setWindowTitle("Review subtitles")
        self._build()
        self._player()
        QApplication.instance().installEventFilter(self)
        self.tick = QTimer(self, interval=40, timeout=self._tick)
        self.tick.start()
        if self.projects:
            self.open_clip(0)
        self.resize(1440, 900)

    # ---------------------------------------------------------------- building

    def _build(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 10, 14, 12)
        outer.setSpacing(10)

        # top bar: clip, language, progress, save
        top = QHBoxLayout()
        top.setSpacing(12)
        self.clip_box = QComboBox()
        self.clip_box.setMinimumWidth(260)
        for pr in self.projects:
            self.clip_box.addItem(pr.title)
        self.clip_box.currentIndexChanged.connect(self.open_clip)
        title = QVBoxLayout()
        title.setSpacing(0)
        self.clip_title = QLabel()
        f = self.clip_title.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setWeight(QFont.DemiBold)
        self.clip_title.setFont(f)
        self.clip_info = QLabel()
        self.clip_info.setObjectName("hint")
        title.addWidget(self.clip_title)
        title.addWidget(self.clip_info)
        top.addWidget(self.clip_box)
        top.addLayout(title, 1)
        self.lang_bar = QHBoxLayout()
        self.lang_bar.setSpacing(0)
        self.lang_group = QButtonGroup(self)
        self.lang_group.idClicked.connect(self.set_track)
        top.addLayout(self.lang_bar)
        prog = QVBoxLayout()
        prog.setSpacing(3)
        self.progress_label = QLabel()
        self.progress = QProgressBar(textVisible=False, maximumHeight=6, minimumWidth=200)
        prog.addWidget(self.progress_label)
        prog.addWidget(self.progress)
        top.addLayout(prog)
        self.save_state = QLabel()
        self.save_state.setObjectName("hint")
        top.addWidget(self.save_state)
        self.save_btn = QPushButton(f"Save subtitles   {key_text('Ctrl+S')}")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        top.addWidget(self.save_btn)
        keys = QToolButton(text="Keys")
        keys.setToolTip("Keyboard shortcuts")
        keys.clicked.connect(self.show_keys)
        top.addWidget(keys)
        outer.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)

        # left: picture, transport, timeline
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)
        self.video = VideoView()
        self.video.find_clip.connect(self.find_clip)
        lv.addWidget(self.video, 1)
        tr = QHBoxLayout()
        self.play_btn = QPushButton()
        self.play_btn.setFixedWidth(120)
        self.play_btn.clicked.connect(self.toggle_play)
        self.play_line_btn = QPushButton(f"Play line   {key_text('Ctrl+R')}")
        self.play_line_btn.clicked.connect(self.play_line)
        self.tc_label = QLabel()
        mono = QFont("Menlo" if MAC else "Consolas")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSizeF(mono.pointSizeF() * 1.25)
        self.tc_label.setFont(mono)
        self.autoplay = QCheckBox("Play each line when I move to it")
        self.autoplay.setChecked(True)
        tr.addWidget(self.play_btn)
        tr.addWidget(self.play_line_btn)
        tr.addSpacing(10)
        tr.addWidget(self.tc_label)
        tr.addStretch()
        tr.addWidget(self.autoplay)
        lv.addLayout(tr)
        self.timeline = Timeline()
        self.timeline.seek.connect(self.seek)
        self.timeline.picked.connect(lambda r: self.select(r, play=False))
        self.timeline.drag_started.connect(self._drag_start)
        self.timeline.dragged.connect(self._drag)
        self.timeline.drag_finished.connect(self._drag_end)
        self.timeline.label = lambda s: self.tc(s).rsplit(":", 1)[0] if self.proj and \
            self.proj.fps else f"{int(s) // 60}:{int(s) % 60:02d}"
        lv.addWidget(self.timeline)
        split.addWidget(left)

        # right: filters, list, editor
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.filter_group = QButtonGroup(self)
        self.filter_btns = {}
        for i, (key, label) in enumerate(FILTERS):
            b = QPushButton(label, checkable=True)
            b.setObjectName("chip")
            self.filter_group.addButton(b, i)
            self.filter_btns[key] = b
            chips.addWidget(b)
        self.filter_btns["check"].setChecked(True)
        self.filter_group.idClicked.connect(lambda _i: self.apply_filter())
        chips.addStretch()
        self.search_box = QLineEdit(placeholderText="Find…", clearButtonEnabled=True)
        self.search_box.setMaximumWidth(200)
        self.search_box.textChanged.connect(lambda _t: self.apply_filter())
        chips.addWidget(self.search_box)
        rv.addLayout(chips)

        self.model = LineModel()
        self.lines = QListView()
        self.lines.setModel(self.model)
        self.lines.setItemDelegate(LineDelegate(self))
        self.lines.setSelectionMode(QAbstractItemView.SingleSelection)
        self.lines.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.lines.setResizeMode(QListView.Adjust)
        self.lines.setFrameShape(QFrame.NoFrame)
        self.lines.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lines.selectionModel().currentChanged.connect(
            lambda cur, _prev: cur.isValid() and cur.row() != self.row
            and self.select(cur.row()))
        rv.addWidget(self.lines, 1)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setObjectName("empty")
        self.empty.hide()
        rv.addWidget(self.empty, 1)

        rv.addWidget(self._editor())
        split.addWidget(right)
        split.setStretchFactor(0, 11)
        split.setStretchFactor(1, 9)
        split.setSizes([800, 640])
        outer.addWidget(split, 1)
        self.setCentralWidget(root)
        self.statusBar().setSizeGripEnabled(False)  # there from the start: no jump later
        self._style()

    def _editor(self) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        g = QVBoxLayout(card)
        g.setContentsMargins(14, 12, 14, 12)
        g.setSpacing(6)

        head = QHBoxLayout()
        self.ed_title = QLabel()
        f = self.ed_title.font()
        f.setWeight(QFont.DemiBold)
        self.ed_title.setFont(f)
        self.ed_status = QLabel()
        head.addWidget(self.ed_title)
        head.addWidget(self.ed_status)
        head.addStretch()
        g.addLayout(head)
        self.ed_hint = QLabel(wordWrap=True)
        self.ed_hint.setObjectName("hint")
        g.addWidget(self.ed_hint)

        self.src_label = QLabel()
        self.src_label.setObjectName("caption")
        self.src_text = QLabel(wordWrap=True)
        self.src_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = self.src_text.font()
        f.setPointSizeF(f.pointSizeF() * 1.15)
        self.src_text.setFont(f)
        g.addWidget(self.src_label)
        g.addWidget(self.src_text)

        row = QHBoxLayout()
        self.text_label = QLabel()
        self.text_label.setObjectName("caption")
        self.counter = QLabel()
        self.counter.setObjectName("caption")
        row.addWidget(self.text_label)
        row.addStretch()
        row.addWidget(self.counter)
        g.addLayout(row)
        self.editor = SubtitleEdit()
        self.editor.textChanged.connect(self._typed)
        g.addWidget(self.editor)

        was = QHBoxLayout()
        self.was_label = QLabel(wordWrap=True)
        self.was_label.setObjectName("hint")
        self.revert_btn = QPushButton("Undo my changes to this line")
        self.revert_btn.setObjectName("link")
        self.revert_btn.clicked.connect(self.revert_line)
        was.addWidget(self.was_label, 1)
        was.addWidget(self.revert_btn)
        self.was_row = QWidget()
        self.was_row.setLayout(was)
        was.setContentsMargins(0, 0, 0, 0)
        g.addWidget(self.was_row)

        self.script_card = QFrame()
        self.script_card.setObjectName("script")
        sc = QHBoxLayout(self.script_card)
        sc.setContentsMargins(10, 8, 10, 8)
        sv = QVBoxLayout()
        sv.setSpacing(2)
        self.script_caption = QLabel()
        self.script_caption.setObjectName("caption")
        self.script_text = QLabel(wordWrap=True)
        self.script_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sv.addWidget(self.script_caption)
        sv.addWidget(self.script_text)
        sc.addLayout(sv, 1)
        self.use_script_btn = QPushButton(f"Use script wording   {key_text('Ctrl+U')}")
        self.use_script_btn.clicked.connect(self.use_script)
        sc.addWidget(self.use_script_btn, 0, Qt.AlignVCenter)
        g.addWidget(self.script_card)

        timing = QHBoxLayout()
        timing.setSpacing(4)
        self.start_tc = QLabel()
        self.end_tc = QLabel()
        for w in (self.start_tc, self.end_tc):
            w.setObjectName("tc")
        cap = QLabel("Shows")
        cap.setObjectName("caption")
        timing.addWidget(cap)
        timing.addSpacing(4)
        for label, tc, minus, plus, here in (
                ("from", self.start_tc, "start_earlier", "start_later", "start_here"),
                ("to", self.end_tc, "end_earlier", "end_later", "end_here")):
            c = QLabel(label)
            c.setObjectName("caption")
            timing.addWidget(c)
            for sym, act, tip in (("‹", minus, "One frame earlier"), (None, None, None),
                                  ("›", plus, "One frame later")):
                if sym is None:
                    timing.addWidget(tc)
                    continue
                b = QToolButton(text=sym)
                b.setAutoRepeat(True)
                b.setToolTip(f"{tip}   {self._keys_for(act)}")
                b.clicked.connect(lambda _c=False, a=act: self.do(a))
                timing.addWidget(b)
            b = QToolButton(text="◆")
            b.setToolTip(f"Set to the playhead   {self._keys_for(here)}")
            b.clicked.connect(lambda _c=False, a=here: self.do(a))
            timing.addWidget(b)
            timing.addSpacing(10)
        self.reset_timing_btn = QPushButton("Reset timing")
        self.reset_timing_btn.setObjectName("link")
        self.reset_timing_btn.clicked.connect(self.reset_timing)
        timing.addWidget(self.reset_timing_btn)
        timing.addStretch()
        g.addLayout(timing)

        actions = QHBoxLayout()
        self.ok_btn = QPushButton(f"✓  Looks good, next   {key_text('Ctrl+Return')}")
        self.ok_btn.setObjectName("primary")
        self.ok_btn.setMinimumHeight(34)
        self.ok_btn.clicked.connect(self.ok_next)
        self.uncheck_btn = QPushButton("Not checked")
        self.uncheck_btn.setObjectName("link")
        self.uncheck_btn.setToolTip("Mark this line as not checked yet")
        self.uncheck_btn.clicked.connect(self.uncheck)
        prev_b = QPushButton("‹ Previous")
        prev_b.setToolTip(f"Previous line to check   {key_text('Ctrl+Up')}")
        prev_b.clicked.connect(lambda: self.do("prev_check"))
        actions.addWidget(prev_b)
        actions.addWidget(self.uncheck_btn)
        actions.addStretch()
        actions.addWidget(self.ok_btn)
        g.addLayout(actions)
        return card

    def _style(self):
        dark = self.palette().color(QPalette.Window).lightness() < 128
        hl = self.palette().color(QPalette.Highlight).name()
        line = "rgba(255,255,255,0.10)" if dark else "rgba(0,0,0,0.10)"
        dim = "rgba(255,255,255,0.55)" if dark else "rgba(0,0,0,0.55)"
        card = "rgba(255,255,255,0.04)" if dark else "rgba(255,255,255,0.75)"
        self.setStyleSheet(f"""
            QFrame#card {{ background: {card}; border: 1px solid {line}; border-radius: 10px; }}
            QFrame#script {{ background: {tint(BLUE, 30).name(QColor.HexArgb)};
                             border: 1px solid {tint(BLUE, 90).name(QColor.HexArgb)};
                             border-radius: 8px; }}
            QLabel#caption {{ color: {dim}; font-size: 11px;
                              text-transform: uppercase; letter-spacing: 0.5px; }}
            QLabel#hint {{ color: {dim}; }}
            QLabel#tc {{ font-family: {'Menlo' if MAC else 'Consolas'}; padding: 0 4px; }}
            QLabel#empty {{ color: {dim}; font-size: 15px; }}
            QPushButton#primary {{ background: {hl}; color: white; border: none;
                                   border-radius: 7px; padding: 7px 16px; font-weight: 600; }}
            QPushButton#primary:disabled {{ background: {line}; color: {dim}; }}
            QPushButton#chip {{ border: 1px solid {line}; border-radius: 13px; padding: 4px 12px; }}
            QPushButton#chip:checked {{ background: {hl}; color: white; border-color: {hl}; }}
            QPushButton#link {{ border: none; background: transparent; color: {hl};
                                padding: 2px 4px; }}
            QPushButton#link:hover {{ text-decoration: underline; }}
            QPushButton#lang {{ border: 1px solid {line}; padding: 5px 14px; }}
            QPushButton#lang:checked {{ background: {hl}; color: white; border-color: {hl}; }}
            QPlainTextEdit {{ border: 1px solid {line}; border-radius: 7px; padding: 4px; }}
            QPlainTextEdit:focus {{ border: 1px solid {hl}; }}
            QListView {{ background: palette(window); }}
        """)

    def _player(self):
        self.player = None
        if QMediaPlayer is None:
            self.video.set_message("Playback needs PySide6-Addons (reinstall MakeSubs). "
                                   "You can still edit and save.")
            return
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self.video.set_frame)
        self.player.setVideoSink(self.sink)
        self.player.errorOccurred.connect(
            lambda _e, msg: self.video.set_message(f"Can't play this clip: {msg}"))
        self.player.playbackStateChanged.connect(lambda _s: self._update_play_btn())
        self.player.mediaStatusChanged.connect(self._media_status)
        self._update_play_btn()

    def _media_status(self, status):
        """A sound file has no picture: say so, rather than show an empty frame."""
        if status == QMediaPlayer.MediaStatus.LoadedMedia and not self.player.hasVideo():
            self.video.set_message(f"Sound only: {self.proj.clip.name}", True)

    # ---------------------------------------------------------------- opening

    def open_clip(self, i: int):
        if not (0 <= i < len(self.projects)):
            return
        if self.clip_box.currentIndex() != i:
            self.clip_box.setCurrentIndex(i)
            return
        self.proj = pr = self.projects[i]
        self.clip_box.setVisible(len(self.projects) > 1)
        self._header()
        if id(pr) not in self.stacks:
            self.stacks[id(pr)] = QUndoStack(self)
            self.stacks[id(pr)].indexChanged.connect(lambda _i: self._update_saved())
            self.saved_index[id(pr)] = 0
        # language buttons
        for b in self.lang_group.buttons():
            self.lang_group.removeButton(b)
            b.deleteLater()
        targets = pr.targets
        for k, t in enumerate(targets):
            b = QPushButton(languages.name(t.lang) if t.lang else "Transcript", checkable=True)
            b.setObjectName("lang")
            self.lang_group.addButton(b, k)
            self.lang_bar.addWidget(b)
            b.setVisible(len(targets) > 1)
        self.source_track = next((t for t in pr.tracks if t.tag == "transcript"), None)
        self._load_media()
        self.set_track(0)
        self._load_wave()
        self._update_saved()

    def set_track(self, k: int):
        targets = self.proj.targets
        self.track = targets[k]
        btn = self.lang_group.button(k)
        if btn:
            btn.setChecked(True)
        self.model.reset(self.proj, self.track)
        self.row = -1
        first_check = next((i for i in range(len(self.track.lines))
                            if self._open_issue(i, MUST)), None)
        if first_check is None:
            first_check = next((i for i in range(len(self.track.lines))
                                if self._open_issue(i, ("script",))), None)
            self.filter_btns["script" if first_check is not None else "all"].setChecked(True)
        else:
            self.filter_btns["check"].setChecked(True)
        self.apply_filter()
        self.select(first_check if first_check is not None else 0, play=False)
        self._update_progress()

    def _header(self):
        pr = self.proj
        self.clip_title.setText(pr.title)
        bits = [pr.scene[:1].upper() + pr.scene[1:], pr.script_name,
                f"{pr.fps} fps" if pr.fps else "", f"TC {pr.start_tc}" if pr.start_tc else ""]
        self.clip_info.setText("   ·   ".join(b for b in bits if b))

    def _load_media(self):
        pr = self.proj
        self.video.image = None
        if not self.player:
            return
        if not pr.clip.exists():
            self.player.setSource(QUrl())
            self.video.set_message(
                "No clip or sound opened with these subtitles." if not pr.clip.suffix else
                f"The clip isn't at\n{pr.clip}\n(moved or renamed?)", True)
            return
        self.video.set_message("")
        self.player.setSource(QUrl.fromLocalFile(str(pr.clip)))
        self.player.pause()  # shows the first frame

    def find_clip(self):
        f, _ = QFileDialog.getOpenFileName(
            self, f"The clip or its sound for {self.proj.stem}", str(self.proj.out_dir),
            f"Clips and sound ({' '.join('*' + e for e in project.MEDIA)});;All files (*)")
        if f:
            self.proj.attach(Path(f))   # the subtitle files keep their name
            self._header()
            self._mark_dirty()
            self._load_media()
            self._load_wave()

    def _load_wave(self):
        pr = self.proj
        if id(pr) in self.waves:
            self.timeline.peaks = self.waves[id(pr)]
            return
        self.timeline.peaks = None
        if not pr.clip.exists():
            return
        thread, loader = QThread(), WaveLoader(pr)
        loader.moveToThread(thread)
        thread.started.connect(loader.run)
        loader.done.connect(self._wave_ready)
        loader.done.connect(thread.quit)
        thread.finished.connect(lambda: self.threads.remove(thread))
        thread.loader = loader
        self.threads.append(thread)
        thread.start()

    def _wave_ready(self, pr, peaks):
        self.waves[id(pr)] = peaks
        if pr is self.proj:
            self.timeline.peaks = peaks
            self.timeline.update()

    # ---------------------------------------------------------------- helpers

    def tc(self, s: float) -> str:
        pr = self.proj
        if pr and pr.fps:
            base = tc_to_frames(pr.start_tc, pr.fps) if pr.start_tc else 0
            return frames_to_tc(base + seconds_to_frames(max(0.0, s), pr.fps), pr.fps)
        m, sec = divmod(max(0.0, s), 60)
        return f"{int(m):02d}:{sec:05.2f}"

    def _keys_for(self, action: str) -> str:
        return " / ".join(key_text(s) for seqs, a, _ in KEYS if a == action for s in seqs[:1])

    def _kinds(self) -> tuple:
        """The issues the current filter works through."""
        return ("script",) if self.filter_btns["script"].isChecked() else MUST

    def _open_issue(self, i: int, kinds=None) -> bool:
        ln = self.track.lines[i]
        return not ln.checked and not ln.edited and \
            self.proj.issue(self.track, i) in (kinds or self._kinds())

    def _was_issue(self, i: int, kinds=MUST) -> bool:
        """An issue now, or one that was dealt with (counts towards the progress)."""
        ln = self.track.lines[i]
        if self.proj.issue(self.track, i) in kinds:
            return True
        if not (ln.checked or ln.edited):
            return False
        if "script" in kinds:
            script = self.proj.script_line(self.track, i)
            return bool(script and script.script_match == "changed")
        src = self.proj.counterpart(self.track, self.source_track, i) \
            if self.source_track else None
        return ln.flagged or ln.edited and bool(src) and src.lang != self.track.lang \
            and project._norm(src.text) == project._norm(ln.machine_text)

    def position(self) -> float:
        return self.player.position() / 1000 if self.player else self.timeline.pos_s

    # ---------------------------------------------------------------- list and filter

    def apply_filter(self):
        if not self.track:
            return
        mode = next(k for k, b in self.filter_btns.items() if b.isChecked())
        needle = self.search_box.text().strip().lower()
        n = len(self.track.lines)
        must = [self._open_issue(i, MUST) for i in range(n)]
        script = [self._open_issue(i, ("script",)) for i in range(n)]
        mine = [ln.edited or ln.retimed for ln in self.track.lines]
        counts = {"check": sum(must), "script": sum(script), "mine": sum(mine), "all": n}
        visible = 0
        for i in range(n):
            # the line just dealt with stays in the list until you move on, unless it was
            # the last one: then the list empties and says so
            keep = i == self.row and counts[mode] > 0
            show = {"check": must[i] or keep and self._was_issue(i, MUST),
                    "script": script[i] or keep and self._was_issue(i, ("script",)),
                    "mine": mine[i], "all": True}[mode]
            if needle:
                src = self.proj.counterpart(self.track, self.source_track, i) \
                    if self.source_track else None
                show = show and (needle in self.track.lines[i].text.lower()
                                 or bool(src and needle in src.text.lower()))
            self.lines.setRowHidden(i, not show)
            visible += show
        for key, label in FILTERS:
            self.filter_btns[key].setText(f"{label}  {counts[key]}")
        self.filter_btns["script"].setVisible(counts["script"] > 0 or mode == "script")
        if visible:
            self.empty.hide()
            self.lines.show()
        else:
            self.lines.hide()
            more = (f"\n\n{counts['script']} line{'s' if counts['script'] > 1 else ''} differ "
                    "from the script: worth a look if there's time (Differs from script)."
                    if counts["script"] else "")
            self.empty.setText(
                "No lines match." if needle else
                "✓  Every line to check is done." + more + "\n\nSave to write the subtitle files."
                if mode == "check" else
                "✓  All the script differences are done.\n\nSave to write the subtitle files."
                if mode == "script" else "You haven't changed any lines yet.")
            self.empty.show()

    def select(self, row: int, play: bool | None = None):
        if not self.track or not (0 <= row < len(self.track.lines)):
            self._fill_editor()
            return
        prev = self.row
        self.row = row
        idx = self.model.index(row)
        if self.lines.isRowHidden(row):
            self.apply_filter()
        if self.lines.isRowHidden(row):  # not in this filter: show everything
            self.search_box.clear()
            self.filter_btns["all"].setChecked(True)
            self.apply_filter()
        if self.lines.currentIndex() != idx:
            self.lines.setCurrentIndex(idx)
        self.lines.scrollTo(idx, QAbstractItemView.EnsureVisible)
        if prev != row:
            self._fill_editor()
        cue = self.model.timed[row]
        if play is None:
            play = self.autoplay.isChecked()
        if play:
            self.play_line()
        else:
            self.stop_at = None
            if self.player:
                self.player.pause()
            self.seek(cue.start)
        self._update_timeline()

    def _fill_editor(self):
        self._loading = True
        try:
            if not self.track or not (0 <= self.row < len(self.track.lines)):
                self.editor.setPlainText("")
                self.editor.setEnabled(False)
                return
            self.editor.setEnabled(True)
            i, tr = self.row, self.track
            ln = tr.lines[i]
            if self.editor.toPlainText() != ln.text:
                self.editor.setPlainText(ln.text)
                self.editor.moveCursor(QTextCursor.End)
            self._update_editor()
        finally:
            self._loading = False

    def _update_editor(self):
        i, pr, tr = self.row, self.proj, self.track
        if not tr or not (0 <= i < len(tr.lines)):
            return
        ln = tr.lines[i]
        cue = self.model.timed[i]
        script = pr.script_line(tr, i)
        issue = pr.issue(tr, i)
        st = status_of(ln, issue)
        self.ed_title.setText(f"Line {i + 1} of {len(tr.lines)}")
        self.ed_status.setText(_chip(st.label, st.color))
        hint = STATUSES[issue].hint if issue and st.key == issue else ""
        self.ed_hint.setText(hint)
        self.ed_hint.setVisible(bool(hint))

        src = pr.counterpart(tr, self.source_track, i) if self.source_track else None
        show_src = bool(src and src is not ln)
        self.src_label.setVisible(show_src)
        self.src_text.setVisible(show_src)
        if show_src:
            rtl_lang = src.lang in project._RTL_LANGS
            lang = f" ({languages.name(src.lang)})" if src.lang and \
                rtl_lang == is_rtl(src.text) else ""
            # (subtitles opened from an .srt don't know how sure the recognition was)
            sure = f"   ·   {src.confidence:.0%} sure" if src.flagged and src.confidence < 1 \
                else ""
            self.src_label.setText(f"Heard{lang}{sure}")
            self.src_text.setText(src.text)
            rtl = is_rtl(src.text)
            self.src_text.setAlignment(Qt.AlignRight if rtl else Qt.AlignLeft)
            self.src_text.setLayoutDirection(Qt.RightToLeft if rtl else Qt.LeftToRight)
        self.text_label.setText(f"Subtitle ({languages.name(tr.lang)})" if tr.lang else "Subtitle")
        self._update_counter()

        self.was_row.setVisible(ln.edited)
        if ln.edited:
            self.was_label.setText(f"Was: {ln.machine_text}")

        has_script = bool(script and script.script_text)
        self.script_card.setVisible(has_script)
        if has_script:
            who, _, words = script.script_text.partition(": ")
            what = {"same": "Follows the script", "changed": "In the script",
                    "none": "Closest script line"}.get(script.script_match, "In the script")
            scene = f", {pr.scene}" if pr.scene else ""
            self.script_caption.setText(f"{what}{scene}   ·   {who}")
            usable = script.script_match != "same" and self._script_usable(script)
            part = pr.script_wording(tr, i) if usable else ""
            full = words or who
            if part and part != full and part in full:  # part of a longer script line
                a = full.index(part)
                self.script_text.setText(f"<span style='opacity:.6'>{full[:a]}</span>"
                                         f"<b>{part}</b>{full[a + len(part):]}")
            else:
                self.script_text.setText(full)
            self.use_script_btn.setVisible(usable)
            self.use_script_btn.setEnabled(usable and ln.text.strip() != part.strip())
        if script and script.script_match == "none" and not has_script:
            self.script_card.hide()

        self.start_tc.setText(self.tc(cue.start))
        self.end_tc.setText(f"{self.tc(cue.end)}   ({cue.end - cue.start:.2f} s)")
        self.reset_timing_btn.setVisible(ln.retimed)
        done = ln.checked or ln.edited
        self.uncheck_btn.setVisible(done)
        self.ok_btn.setText(("✓  Next   " if done else "✓  Looks good, next   ")
                            + key_text("Ctrl+Return"))

    def _script_usable(self, script: Line) -> bool:
        """The script's wording can stand in only for a subtitle in the script's language."""
        return script is self.track.lines[self.row] or script.lang == self.track.lang

    def _update_counter(self):
        text = self.editor.toPlainText()
        cue = self.model.timed[self.row] if 0 <= self.row < len(self.model.timed) else None
        n = len(" ".join(text.split()))
        widest = max((len(x) for x in wrap(text).split("\n")), default=0) if text else 0
        cps = n / max(0.1, cue.end - cue.start) if cue else 0
        fast = cps > READING_CPS + 0.5
        long = widest > MAX_LINE
        warn = "color:#e5534b" if fast or long else ""
        self.counter.setText(
            f"<span style='{'color:#e5534b' if long else ''}'>{widest}/{MAX_LINE} per line</span>"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;<span style='{warn if fast else ''}'>"
            f"{cps:.0f} characters/s{' — too fast to read' if fast else ''}</span>")

    def _update_progress(self):
        if not self.track:
            return
        issues = [i for i in range(len(self.track.lines)) if self._was_issue(i)]
        done = sum(not self._open_issue(i, MUST) for i in issues)
        total = len(issues)
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done if total else 1)
        lines = "line" if total == 1 else "lines"
        self.progress_label.setText(f"{done} of {total} {lines} checked" if total
                                    else "Nothing flagged in this clip")

    def _update_timeline(self):
        if not self.track:
            return
        statuses = [status_of(ln, self.proj.issue(self.track, i))
                    for i, ln in enumerate(self.track.lines)]
        dur = self.player.duration() / 1000 if self.player and self.player.duration() > 0 else 0
        self.timeline.set_data(self.model.timed, statuses, self.row, self.proj.frame, dur)

    def _update_play_btn(self):
        playing = self.player and self.player.playbackState() == QMediaPlayer.PlayingState
        self.play_btn.setText(("❚❚  Pause   " if playing else "▶  Play   ") + key_text("Esc"))

    # ---------------------------------------------------------------- changes

    def _snapshot(self, rows):
        snap = {}
        for ti, t in enumerate(self.proj.tracks):
            if t is self.track or self.proj.aligned(self.track, t):
                for r in rows:
                    snap[(ti, r)] = copy.copy(t.lines[r])
        return snap

    def edit(self, label: str, fn, rows=None, key=None, before=None):
        """Apply fn() to the current line(s) as one undoable step."""
        rows = rows or [self.row]
        before = before or self._snapshot(rows)
        fn()
        after = self._snapshot(rows)
        if all(vars(before[k]) == vars(after[k]) for k in before):
            return
        self.stacks[id(self.proj)].push(Change(self, self.proj, label, before, after, key))
        self.changed(self.proj, rows)

    def changed(self, proj: Project, rows):
        if proj is not self.proj:
            self.open_clip(self.projects.index(proj))
        self.model.refresh(rows)
        if self.row in rows:
            if not self._loading and self.editor.toPlainText() != self.track.lines[self.row].text:
                self._fill_editor()
            else:
                self._update_editor()
        self.apply_filter()
        self._update_progress()
        self._update_timeline()
        self._update_saved()

    def _typed(self):
        if self._loading or not (0 <= self.row < len(self.track.lines)):
            return
        text = self.editor.toPlainText()
        row = self.row

        def fn():
            for t in self._aligned_tracks(same_lang=True):
                t.lines[row].text = text
        self._loading = True
        try:
            self.edit("Edit text", fn, key=("text", id(self.track), row))
        finally:
            self._loading = False
        self._update_counter()

    def _aligned_tracks(self, same_lang=False):
        return [t for t in self.proj.tracks if t is self.track
                or (not same_lang and self.proj.aligned(self.track, t))]

    def use_script(self):
        script = self.proj.script_line(self.track, self.row)
        if not script or not script.script_text or not self._script_usable(script):
            return
        words = self.proj.script_wording(self.track, self.row)
        row = self.row

        def fn():
            self.track.lines[row].text = words
        self.edit("Use script wording", fn)
        self._fill_editor()

    def revert_line(self):
        row = self.row

        def fn():
            ln = self.track.lines[row]
            ln.text = ln.machine_text
        self.edit("Undo changes to the line", fn)
        self._fill_editor()

    def set_checked(self, value: bool):
        row = self.row

        def fn():
            for t in self._aligned_tracks():
                t.lines[row].checked = value
        self.edit("Checked" if value else "Not checked", fn)

    def uncheck(self):
        self.set_checked(False)

    def ok_next(self):
        if not (0 <= self.row < len(self.track.lines)):
            return
        if not self.track.lines[self.row].checked:
            self.set_checked(True)
        nxt = self._find(+1, issues=True)
        if nxt is None:
            self.apply_filter()
            if self.player:
                self.player.pause()
            self.statusBar().showMessage("That was the last line to check. Save when you're "
                                         "ready.", 6000)
            return
        self.select(nxt)

    def _find(self, step: int, issues: bool):
        n = len(self.track.lines)
        i = self.row + step
        while 0 <= i < n:
            if (self._open_issue(i) if issues else not self.lines.isRowHidden(i)):
                return i
            i += step
        if issues:  # wrap round to earlier ones left open
            i = 0 if step > 0 else n - 1
            while 0 <= i < n and i != self.row:
                if self._open_issue(i):
                    return i
                i += step
        return None

    # timing
    def _bounds(self, row):
        cues = self.model.timed
        frame = self.proj.frame
        prev_end = max((c.end for k, c in enumerate(cues) if k != row
                        and c.start < cues[row].start), default=0.0)
        next_start = min((c.start for k, c in enumerate(cues) if k != row
                          and c.start > cues[row].start), default=1e9)
        return prev_end, next_start - 2 * frame

    def retime(self, start=None, end=None, row=None, record=True, before=None):
        row = self.row if row is None else row
        cue = self.model.timed[row]
        frame = self.proj.frame
        lo, hi = self._bounds(row)
        if start is not None:
            start = round(min(max(start, lo, 0.0), (end or cue.end) - frame) / frame) * frame
        if end is not None:
            end = round(max(min(end, hi), (start if start is not None else cue.start) + frame)
                        / frame) * frame

        def fn():
            self.proj.set_timing(self.track, row, start, end)
        if record:
            self.edit("Change timing", fn, rows=[row], before=before)
        else:
            fn()
            self.model.refresh([row])
            self._update_editor()
            self._update_timeline()

    def reset_timing(self):
        row = self.row
        self.edit("Reset timing", lambda: self.proj.reset_timing(self.track, row))

    def _drag_start(self, row):
        if row != self.row:
            self.select(row, play=False)
        self.drag_before = self._snapshot([row])

    def _drag(self, row, start, end):
        self.retime(start, end, row=row, record=False)
        if self.player:
            self.seek(start if start is not None else end)

    def _drag_end(self, row):
        before, self.drag_before = self.drag_before, None
        if before:
            self.edit("Change timing", lambda: None, rows=[row], before=before)

    # ---------------------------------------------------------------- playback

    def seek(self, s: float):
        if self.player:
            self.player.setPosition(int(s * 1000))
        self.timeline.set_pos(s)
        self._show_position(s)

    def toggle_play(self):
        if not self.player:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.stop_at = None
            self.player.play()

    def play_line(self):
        if not self.player or not (0 <= self.row < len(self.model.timed)):
            return
        cue = self.model.timed[self.row]
        self.player.setPosition(int(max(0.0, cue.start - 0.4) * 1000))
        self.stop_at = cue.end + 0.25
        self.player.play()

    def _tick(self):
        if not self.player or not self.track:
            return
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        s = self.position()
        if self.stop_at is not None and s >= self.stop_at:
            self.player.pause()
            self.stop_at = None
        self.timeline.set_pos(s)
        self._show_position(s)

    def _show_position(self, s: float):
        self.tc_label.setText(self.tc(s))
        text, flag = "", False
        for i, c in enumerate(self.model.timed):
            if c.start <= s < c.end:
                ln = self.track.lines[i]
                text, flag = ln.text, ln.flagged and not (ln.checked or ln.edited)
                break
        self.video.set_subtitle(text, flag)

    # ---------------------------------------------------------------- keys

    def do(self, action: str):
        if action == "play":
            self.toggle_play()
        elif action == "play_line":
            self.play_line()
        elif action == "ok_next":
            self.ok_next()
        elif action in ("next_check", "prev_check"):
            i = self._find(+1 if action == "next_check" else -1, issues=True)
            if i is not None:
                self.select(i)
        elif action in ("next_line", "prev_line"):
            i = self._find(+1 if action == "next_line" else -1, issues=False)
            if i is not None:
                self.select(i)
        elif action == "use_script":
            self.use_script()
        elif action in ("start_earlier", "start_later", "end_earlier", "end_later",
                        "start_here", "end_here"):
            if not (0 <= self.row < len(self.model.timed)):
                return
            cue, f = self.model.timed[self.row], self.proj.frame
            new = {"start_earlier": ("s", cue.start - f), "start_later": ("s", cue.start + f),
                   "end_earlier": ("e", cue.end - f), "end_later": ("e", cue.end + f),
                   "start_here": ("s", self.position()), "end_here": ("e", self.position())}
            which, value = new[action]
            self.retime(start=value if which == "s" else None, end=value if which == "e" else None)
            self.seek(self.model.timed[self.row].start if which == "s"
                      else max(0.0, self.model.timed[self.row].end - f))
        elif action == "undo":
            self.stacks[id(self.proj)].undo()
        elif action == "redo":
            self.stacks[id(self.proj)].redo()
        elif action == "save":
            self.save()
        elif action == "find":
            self.search_box.setFocus()
            self.search_box.selectAll()
        elif action == "keys":
            self.show_keys()

    def eventFilter(self, obj, e):
        if e.type() == QEvent.KeyPress and self.isActiveWindow() and isinstance(obj, QWidget) \
                and obj.window() is self:
            combo = e.keyCombination()
            mods = combo.keyboardModifiers() & ~Qt.KeypadModifier
            key = QKeyCombination(mods, combo.key()).toCombined()
            action = KEYMAP.get(key)
            if action == "play" and obj is self.search_box and self.search_box.text():
                return False  # Esc clears the search box first
            if action:
                self.do(action)
                return True
            if combo.key() == Qt.Key_Space and not mods \
                    and not isinstance(obj, (QPlainTextEdit, QLineEdit)):
                self.toggle_play()
                return True
        return super().eventFilter(obj, e)

    def show_keys(self):
        rows = "".join(
            f"<tr><td style='padding:3px 18px 3px 0'><b>{' or '.join(key_text(s) for s in seqs)}"
            f"</b></td><td>{desc}</td></tr>" for seqs, _a, desc in KEYS)
        box = QMessageBox(self)
        box.setWindowTitle("Keys")
        box.setTextFormat(Qt.RichText)
        box.setText("<p>These work while you type in the subtitle.</p>"
                    f"<table>{rows}</table><p>Space also plays and pauses when you're not "
                    "typing. Drag the ends of the selected line in the timeline to retime it; "
                    "scroll there to zoom.</p>")
        box.exec()

    # ---------------------------------------------------------------- saving

    def dirty(self, pr: Project) -> bool:
        st = self.stacks.get(id(pr))
        return bool(st) and (st.index() != self.saved_index[id(pr)] or getattr(pr, "_dirty", False))

    def _mark_dirty(self):
        self.proj._dirty = True
        self._update_saved()

    def _update_saved(self):
        if getattr(self, "_closing", False):
            return
        any_dirty = any(self.dirty(pr) for pr in self.projects)
        cur = self.proj and self.dirty(self.proj)
        self.save_btn.setEnabled(any_dirty)
        self.save_state.setText("Unsaved changes" if cur else "Saved" if self.proj else "")
        self.setWindowModified(any_dirty)
        self.setWindowTitle(f"Review subtitles — {self.proj.title if self.proj else ''}[*]")
        for k, pr in enumerate(self.projects):
            self.clip_box.setItemText(k, ("● " if self.dirty(pr) else "") + pr.title)

    def save(self) -> bool:
        written = []
        for pr in self.projects:
            if not self.dirty(pr):
                continue
            try:
                written += pr.export()
                pr.save()
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "MakeSubs", f"Couldn't save {pr.title}:\n{e}")
                return False
            self.saved_index[id(pr)] = self.stacks[id(pr)].index()
            pr._dirty = False
        self._update_saved()
        if written:
            names = sorted({p.name for p in written})
            self.statusBar().showMessage(f"Saved {len(names)} file{'s' if len(names) > 1 else ''}"
                                         f": {', '.join(names[:4])}"
                                         f"{' …' if len(names) > 4 else ''}", 8000)
        return True

    def closeEvent(self, e):
        if any(self.dirty(pr) for pr in self.projects):
            box = QMessageBox(QMessageBox.Question, "Review subtitles",
                              "Save your changes to the subtitle files?",
                              QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, self)
            box.setDefaultButton(QMessageBox.Save)
            answer = box.exec()
            if answer == QMessageBox.Cancel or (answer == QMessageBox.Save and not self.save()):
                e.ignore()
                return
        QApplication.instance().removeEventFilter(self)
        self._closing = True
        self.tick.stop()
        if self.player:
            self.player.stop()
        for t in list(self.threads):
            t.quit()   # its quit signal would wait for this (blocked) thread: ask directly
            t.wait(20000)
        self.closed.emit()
        e.accept()


def open_files(parent=None) -> list[Path]:
    """Review files or .srt files, and optionally the clips or sound files to play with them."""
    media = " ".join("*" + e for e in project.MEDIA)
    files, _ = QFileDialog.getOpenFileNames(
        parent, "Open subtitles to review (and the clip or sound, if it has another name)", "",
        f"Subtitles, clips and sound (*{project.SUFFIX} *.srt {media});;"
        f"MakeSubs review files (*{project.SUFFIX});;Subtitles (*.srt);;All files (*)")
    return [Path(f) for f in files]


def main() -> int:
    """Open review files, .srt files and their clips directly."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName("Wangle Media")
    app.setApplicationName("MakeSubs")
    files = [Path(a) for a in sys.argv[1:]] or open_files()
    if not files:
        return 0
    w = ReviewWindow(files)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
