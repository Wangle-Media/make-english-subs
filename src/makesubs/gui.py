"""Drag-and-drop desktop window for editors."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QPlainTextEdit, QPushButton, QVBoxLayout,
    QWidget,
)

from . import __version__, translate
from .pipeline import Options, run

SPOKEN_PRESETS = [
    ("Arabic + English", "ar,en"),
    ("Arabic", "ar"),
    ("Danish + English", "da,en"),
    ("English", "en"),
    ("Auto-detect (any language)", "auto"),
]


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

    def __init__(self, files, opts):
        super().__init__()
        self.files, self.opts = files, opts

    def run(self):
        try:
            jobs = run([Path(f) for f in self.files], self.opts, self.log.emit)
            self.log.emit("")
            for j in jobs:
                self.log.emit(f"{'FAILED' if j.error else 'OK'}  {j.info.path.name}"
                              + (f": {j.error}" if j.error else ""))
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

        self.spoken = QComboBox()
        self.spoken.setEditable(True)
        for label, codes in SPOKEN_PRESETS:
            self.spoken.addItem(f"{label}  [{codes}]", codes)
        self.spoken.setToolTip("Language codes, comma separated (e.g. ar,en).\n"
                               "Naming the actual languages gives better results than auto-detect.")
        form.addRow("Spoken in clips:", self.spoken)
        hint = QLabel("Tip: choosing the actual spoken language(s) gives better results than auto-detect.")
        hint.setStyleSheet("color: gray")
        form.addRow("", hint)

        targets = QHBoxLayout()
        self.t_en, self.t_da = QCheckBox("English"), QCheckBox("Danish")
        self.t_other = QLineEdit()
        self.t_other.setPlaceholderText("other codes, e.g. de,fr")
        targets.addWidget(self.t_en)
        targets.addWidget(self.t_da)
        targets.addWidget(self.t_other)
        form.addRow("Subtitles in:", targets)

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

        self.go = QPushButton("3. Make subtitles")
        self.go.setMinimumHeight(36)
        self.go.clicked.connect(self.start)
        lay.addWidget(self.go)

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
        spoken = s.value("spoken", "ar,en")
        i = self.spoken.findData(spoken)
        if i >= 0:
            self.spoken.setCurrentIndex(i)
        else:
            self.spoken.setEditText(spoken)
        self.t_en.setChecked(s.value("t_en", True, bool))
        self.t_da.setChecked(s.value("t_da", False, bool))
        self.t_other.setText(s.value("t_other", ""))
        self.channel.setCurrentIndex(max(0, self.channel.findData(s.value("channel", "mix"))))
        self.f_srt.setChecked(s.value("f_srt", True, bool))
        self.f_avid.setChecked(s.value("f_avid", False, bool))
        self.note.setText(s.value("note", ""))
        self.set_out(s.value("out_dir", None) or None)

    def save_settings(self):
        s = self.settings
        s.setValue("spoken", self.spoken_codes())
        s.setValue("t_en", self.t_en.isChecked())
        s.setValue("t_da", self.t_da.isChecked())
        s.setValue("t_other", self.t_other.text())
        s.setValue("channel", self.channel.currentData())
        s.setValue("f_srt", self.f_srt.isChecked())
        s.setValue("f_avid", self.f_avid.isChecked())
        s.setValue("note", self.note.text())
        s.setValue("out_dir", self.out_dir or "")

    def spoken_codes(self) -> str:
        i = self.spoken.currentIndex()
        if i >= 0 and self.spoken.itemText(i) == self.spoken.currentText():
            return self.spoken.itemData(i)
        text = self.spoken.currentText()
        if "[" in text and "]" in text:
            text = text[text.index("[") + 1:text.index("]")]
        return ",".join(c.strip().lower() for c in text.split(",") if c.strip())

    # ---------------------------------------------------------------- actions

    def check_llm(self):
        if translate.available():
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
        targets = ([c for c, box in (("en", self.t_en), ("da", self.t_da)) if box.isChecked()]
                   + [c.strip().lower() for c in self.t_other.text().split(",") if c.strip()])
        formats = [f for f, box in (("srt", self.f_srt), ("avid", self.f_avid)) if box.isChecked()]
        if not files:
            return self.say("Add some clips first.")
        if not targets:
            return self.say("Choose at least one subtitle language.")
        if not formats:
            return self.say("Choose at least one NLE format.")
        self.save_settings()
        opts = Options(targets=targets, spoken=self.spoken_codes().split(","),
                       channel=self.channel.currentData(), formats=formats,
                       note=self.note.text().strip(),
                       out_dir=Path(self.out_dir) if self.out_dir else None,
                       overwrite=self.overwrite.isChecked())
        self.log.clear()
        self.go.setEnabled(False)
        self.go.setText("Working… (first run downloads models)")
        self.thread = QThread()
        self.worker = Worker(files, opts)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.say)
        self.worker.finished.connect(self.done)
        self.worker.finished.connect(self.thread.quit)
        self.thread.start()

    def done(self, ok: bool):
        self.go.setEnabled(True)
        self.go.setText("3. Make subtitles")
        self.say("\nAll done." if ok else "\nFinished with problems — see above.")

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


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MakeSubs")
    w = Window()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
