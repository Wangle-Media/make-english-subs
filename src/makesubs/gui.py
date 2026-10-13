"""Drag-and-drop desktop window for editors."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QPlainTextEdit, QPushButton, QVBoxLayout,
    QWidget,
)

from . import __version__, languages, translate
from .pipeline import Options, run


class LanguagePicker(QPushButton):
    """Button showing the chosen languages; click for a searchable list with tick boxes."""
    changed = Signal()

    def __init__(self, names: dict[str, str], allow_auto: bool = False):
        super().__init__()
        self.setStyleSheet("text-align: left; padding: 4px 8px")
        self.popup = QFrame(self, Qt.Popup)
        self.popup.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(self.popup)
        self.search = QLineEdit(placeholderText="Search languages…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)
        self.auto = None
        if allow_auto:
            self.auto = QCheckBox("Don't know — auto-detect (less accurate)")
            self.auto.toggled.connect(self._auto_toggled)
            lay.addWidget(self.auto)
        self.list = QListWidget()
        common = [c for c in languages.COMMON if c in names]
        rest = sorted((c for c in names if c not in common), key=names.get)
        for code in common + rest:
            item = QListWidgetItem(names[code])
            item.setData(Qt.UserRole, code)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.list.addItem(item)
            if code == common[-1]:
                line = QListWidgetItem("All languages")
                line.setFlags(Qt.NoItemFlags)
                self.list.addItem(line)
        self.list.itemChanged.connect(self._item_changed)
        self.list.itemClicked.connect(self._toggle_on_text_click)
        lay.addWidget(self.list)
        self.clicked.connect(self._open)
        self._refresh()

    def _items(self):
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.UserRole):
                yield item

    def codes(self) -> list[str]:
        if self.auto and self.auto.isChecked():
            return ["auto"]
        return [i.data(Qt.UserRole) for i in self._items() if i.checkState() == Qt.Checked]

    def set_codes(self, codes: list[str]) -> None:
        self.list.blockSignals(True)
        for item in self._items():
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in codes else Qt.Unchecked)
        self.list.blockSignals(False)
        if self.auto:
            self.auto.blockSignals(True)
            self.auto.setChecked(codes == ["auto"])
            self.auto.blockSignals(False)
        self._refresh()

    def _open(self):
        self.search.clear()
        self.popup.resize(max(self.width(), 300), 340)
        self.popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        self.popup.show()
        self.search.setFocus()

    def _filter(self, text: str):
        t = text.strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            code = item.data(Qt.UserRole)
            item.setHidden(bool(t) and (not code or not (
                item.text().lower().startswith(t) or code == t)))

    _just_changed = False  # the tick box itself was clicked

    def _toggle_on_text_click(self, item):
        # Clicking the name, not just the small box, ticks it too.
        if item.data(Qt.UserRole) and not self._just_changed:
            item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)
        self._just_changed = False

    def _item_changed(self, item):
        self._just_changed = True
        if self.auto and item.checkState() == Qt.Checked:
            self.auto.blockSignals(True)
            self.auto.setChecked(False)
            self.auto.blockSignals(False)
        self._refresh()

    def _auto_toggled(self, on: bool):
        if on:
            self.list.blockSignals(True)
            for item in self._items():
                item.setCheckState(Qt.Unchecked)
            self.list.blockSignals(False)
        self._refresh()

    def _refresh(self):
        codes = self.codes()
        if codes == ["auto"]:
            text = "Auto-detect (any language)"
        elif not codes:
            text = "Choose…"
        else:
            names = [languages.name(c) for c in codes]
            text = ", ".join(names[:4]) + (f" +{len(names) - 4} more" if len(names) > 4 else "")
        self.setText(text + "   ▾")
        self.setToolTip(", ".join(f"{languages.name(c)} ({c})" for c in codes if c != "auto"))
        self.changed.emit()


class DropList(QListWidget):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setMinimumHeight(140)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        self.add_paths(Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile())

    def add_paths(self, paths):
        have = set(self.paths())
        for p in paths:
            files = sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else [p]
            for f in files:
                if str(f) not in have and not f.name.startswith(".") \
                        and f.suffix.lower() not in (".srt", ".txt", ".xml", ".ale"):
                    item = QListWidgetItem(f"{f.name}    —  {f.parent}")
                    item.setData(Qt.UserRole, str(f))
                    item.setToolTip(str(f))
                    self.addItem(item)
                    have.add(str(f))

    def paths(self) -> list[str]:
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]


class Worker(QObject):
    log = Signal(str)
    finished = Signal(bool)
    folder = Signal(str)  # where the subtitle files ended up

    def __init__(self, files, opts):
        super().__init__()
        self.files, self.opts = files, opts

    def run(self):
        try:
            jobs = run([Path(f) for f in self.files], self.opts, self.log.emit)
            self.log.emit("")
            for j in jobs:
                if j.error:
                    self.log.emit(f"FAILED  {j.info.path.name}: {j.error}")
                    continue
                subs = [p for p in j.outputs if not p.name.endswith(".review.txt")]
                where = subs[0].parent if subs else j.info.path.parent
                self.log.emit(f"OK      {j.info.path.name}: {len(subs)} subtitle files in {where}")
                flagged = sum(c.flagged for c in j.source_cues)
                if flagged:
                    self.log.emit(f"        {flagged} line{'s' if flagged > 1 else ''} marked [?] "
                                  f"to check, see {j.info.path.stem}.review.txt")
                self.folder.emit(str(Path(where).resolve()))
            self.finished.emit(bool(jobs) and not any(j.error for j in jobs))
        except Exception as e:  # noqa: BLE001 - show anything to the editor
            self.log.emit(f"Error: {e}")
            self.finished.emit(False)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"MakeSubs {__version__}")
        self.settings = QSettings("Wangle Media", "MakeSubs")
        self.out_dir: str | None = None
        self.thread: QThread | None = None

        root = QWidget()
        lay = QVBoxLayout(root)

        lay.addWidget(QLabel("<b>1. Drop clips (or folders) here</b> — they are only read, never changed."))
        self.files = DropList()
        lay.addWidget(self.files)
        row = QHBoxLayout()
        add, clear = QPushButton("Add files…"), QPushButton("Clear list")
        add.clicked.connect(self.pick_files)
        clear.clicked.connect(self.files.clear)
        row.addWidget(add)
        row.addWidget(clear)
        row.addStretch()
        lay.addLayout(row)

        box = QGroupBox("2. Settings")
        form = QFormLayout(box)

        self.spoken = LanguagePicker(languages.WHISPER, allow_auto=True)
        form.addRow("Spoken in clips:", self.spoken)
        self.spoken_hint = QLabel()
        self.spoken_hint.setWordWrap(True)
        form.addRow("", self.spoken_hint)
        self.spoken.changed.connect(self.update_hints)

        self.targets = LanguagePicker(languages.TARGETS)
        form.addRow("Subtitles in:", self.targets)
        self.targets_hint = QLabel()
        self.targets_hint.setWordWrap(True)
        form.addRow("", self.targets_hint)
        self.targets.changed.connect(self.update_hints)
        self.llm_ok = False

        self.llm_status = QLabel()
        recheck = QPushButton("Check again")
        recheck.clicked.connect(self.check_llm)
        r = QHBoxLayout()
        r.addWidget(self.llm_status, 1)
        r.addWidget(recheck)
        form.addRow("Translation:", r)

        self.channel = QComboBox()
        self.channel.addItem("Mix all channels", "mix")
        for n in range(1, 17):
            self.channel.addItem(f"Channel {n} only", str(n))
        form.addRow("Audio:", self.channel)

        fmts = QHBoxLayout()
        self.f_srt, self.f_avid = QCheckBox("Premiere / Resolve (.srt)"), QCheckBox("Avid SubCap (.avid.txt)")
        fmts.addWidget(self.f_srt)
        fmts.addWidget(self.f_avid)
        form.addRow("For NLE:", fmts)

        self.note = QLineEdit()
        self.note.setPlaceholderText("Names, places, terms — helps the translator spell them right")
        form.addRow("Notes:", self.note)

        out = QHBoxLayout()
        self.out_label = QLabel()
        choose, reset = QPushButton("Choose…"), QPushButton("Next to clips")
        choose.clicked.connect(self.pick_out)
        reset.clicked.connect(lambda: self.set_out(None))
        out.addWidget(self.out_label, 1)
        out.addWidget(choose)
        out.addWidget(reset)
        form.addRow("Save to:", out)

        self.overwrite = QCheckBox("Replace existing subtitle files")
        form.addRow("", self.overwrite)
        lay.addWidget(box)

        row = QHBoxLayout()
        self.go = QPushButton("3. Make subtitles")
        self.go.setMinimumHeight(36)
        self.go.clicked.connect(self.start)
        self.show_files = QPushButton("Show subtitle files")
        self.show_files.setMinimumHeight(36)
        self.show_files.setEnabled(False)
        self.show_files.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.result_dir)))
        self.result_dir = ""
        row.addWidget(self.go, 3)
        row.addWidget(self.show_files, 1)
        lay.addLayout(row)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setFont(QFont("Monospace", 9))
        self.log.setMinimumHeight(180)
        lay.addWidget(self.log)

        self.setCentralWidget(root)
        self.resize(760, 820)
        self.load_settings()
        self.check_llm()

    # ---------------------------------------------------------------- settings

    def load_settings(self):
        s = self.settings
        self.spoken.set_codes(_split(s.value("spoken", "ar,en")))
        targets = s.value("targets", None)
        if targets is None:  # settings from 0.2.0
            targets = ",".join([c for c, k, d in (("en", "t_en", True), ("da", "t_da", False))
                                if s.value(k, d, bool)] + _split(s.value("t_other", "")))
        self.targets.set_codes(_split(targets))
        self.channel.setCurrentIndex(max(0, self.channel.findData(s.value("channel", "mix"))))
        self.f_srt.setChecked(s.value("f_srt", True, bool))
        self.f_avid.setChecked(s.value("f_avid", False, bool))
        self.note.setText(s.value("note", ""))
        self.set_out(s.value("out_dir", None) or None)

    def save_settings(self):
        s = self.settings
        s.setValue("spoken", ",".join(self.spoken.codes()))
        s.setValue("targets", ",".join(self.targets.codes()))
        s.setValue("channel", self.channel.currentData())
        s.setValue("f_srt", self.f_srt.isChecked())
        s.setValue("f_avid", self.f_avid.isChecked())
        s.setValue("note", self.note.text())
        s.setValue("out_dir", self.out_dir or "")

    def update_hints(self):
        spoken = self.spoken.codes()
        if spoken == ["auto"]:
            self.spoken_hint.setText("Auto-detect is less accurate. Tick the language(s) people "
                                     "actually speak if you know them.")
            self.spoken_hint.setStyleSheet("color: #b36b00")
        elif len(spoken) > 1:
            self.spoken_hint.setText("Tick only languages that are really spoken. "
                                     "If it's just one, one tick gives the best result.")
            self.spoken_hint.setStyleSheet("color: gray")
        else:
            self.spoken_hint.setText("Tip: setting the spoken language gives better results "
                                     "than auto-detect.")
            self.spoken_hint.setStyleSheet("color: gray")
        others = [languages.name(c) for c in self.targets.codes() if c != "en"]
        if others and not self.llm_ok:
            listed = " and ".join(filter(None, [", ".join(others[:-1]), others[-1]]))
            verb = "need" if len(others) > 1 else "needs"
            self.targets_hint.setText(f"{listed} {verb} the high-quality translator "
                                      "(Ollama), which isn't running.")
            self.targets_hint.setStyleSheet("color: #b36b00")
            self.targets_hint.show()
        else:
            self.targets_hint.hide()

    # ---------------------------------------------------------------- actions

    def check_llm(self):
        self.llm_ok = translate.available()
        self.update_hints()
        if self.llm_ok:
            self.llm_status.setText(f"Local LLM ready ({translate.DEFAULT_MODEL}) — any language")
            self.llm_status.setStyleSheet("color: green")
        else:
            self.llm_status.setText("Basic: English only (Ollama not running — see README for better quality)")
            self.llm_status.setStyleSheet("color: #b36b00")

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Choose clips")
        self.files.add_paths(Path(f) for f in files)

    def pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "Save subtitles to")
        if d:
            self.set_out(d)

    def set_out(self, d):
        self.out_dir = d
        self.out_label.setText(d or "Next to each clip")

    def start(self):
        files = self.files.paths()
        targets = self.targets.codes()
        formats = [f for f, box in (("srt", self.f_srt), ("avid", self.f_avid)) if box.isChecked()]
        if not files:
            return self.say("Add some clips first.")
        if not self.spoken.codes():
            return self.say("Choose the spoken language(s), or auto-detect.")
        if not targets:
            return self.say("Choose at least one subtitle language.")
        if not formats:
            return self.say("Choose at least one NLE format.")
        self.save_settings()
        opts = Options(targets=targets, spoken=self.spoken.codes(),
                       channel=self.channel.currentData(), formats=formats,
                       note=self.note.text().strip(),
                       out_dir=Path(self.out_dir) if self.out_dir else None,
                       overwrite=self.overwrite.isChecked())
        self.log.clear()
        self.result_dir = ""
        self.show_files.setEnabled(False)
        self.go.setEnabled(False)
        self.go.setText("Working… (first run downloads models)")
        self.thread = QThread()
        self.worker = Worker(files, opts)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.say)
        self.worker.folder.connect(self.set_result_dir)
        self.worker.finished.connect(self.done)
        self.worker.finished.connect(self.thread.quit)
        self.thread.start()

    def set_result_dir(self, d: str):
        if not self.result_dir:
            self.result_dir = d
            self.show_files.setEnabled(True)

    def done(self, ok: bool):
        self.go.setEnabled(True)
        self.go.setText("3. Make subtitles")
        self.say("\nAll done. Click 'Show subtitle files' to open the folder." if ok
                 else "\nFinished with problems, see above.")

    def say(self, text: str):
        self.log.appendPlainText(text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def closeEvent(self, e):
        if self.thread and self.thread.isRunning():
            self.say("Still working — please wait for it to finish.")
            e.ignore()
            return
        self.save_settings()
        e.accept()


def _split(codes) -> list[str]:
    # QSettings may hand back "ar,en" as a string or as a list, depending on quoting.
    if isinstance(codes, str):
        codes = codes.split(",")
    return [c.strip().lower() for c in codes or [] if c.strip()]


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MakeSubs")
    w = Window()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
