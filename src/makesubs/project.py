"""The review file: everything needed to check, correct and re-save a clip's subtitles.

The pipeline saves one `<clip>.makesubs.json` next to the subtitle files. The review window
opens it, and saving there writes the subtitle files again from it. Lines are kept at their
speech timing, and the on-screen timing is worked out the same way the pipeline does it, so
a line's timing only differs from the pipeline's where someone changed it by hand.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from fractions import Fraction
from pathlib import Path

from .shape import Cue, finalize
from .translate import _sentences, _similarity
from .timecode import FrameRate
from .writers import write_srt, write_subcap

SUFFIX = ".makesubs.json"
FORMAT = 1
MUST = ("untranslated", "unsure")   # issues to look at; "script" differences are optional


@dataclass
class Line:
    start: float                 # speech timing
    end: float
    text: str
    lang: str
    confidence: float = 1.0
    flagged: bool = False        # low recognition confidence
    machine_text: str = ""       # the text as made by the pipeline, to compare and revert
    script_ref: str = ""
    script_match: str = ""       # "same", "changed", "none" or ""
    script_text: str = ""        # "CHARACTER: line" from the script, when matched
    start_override: float | None = None   # on-screen timing set by hand
    end_override: float | None = None
    checked: bool = False        # someone looked at it

    @property
    def edited(self) -> bool:
        return self.text.strip() != self.machine_text.strip()

    @property
    def needs_check(self) -> bool:
        """Lines worth a look: unsure recognition, or worded differently from the script."""
        return self.flagged or self.script_match == "changed"

    @property
    def retimed(self) -> bool:
        return self.start_override is not None or self.end_override is not None


@dataclass
class Track:
    tag: str                     # "transcript", "en", "da"... (the output file's tag)
    lang: str
    lines: list[Line] = field(default_factory=list)


@dataclass
class Project:
    clip: Path
    out_dir: Path
    fps: FrameRate | None
    start_tc: str | None
    avid_start: str | None = None
    formats: list[str] = field(default_factory=lambda: ["srt"])
    mark_flagged: bool = True
    channel: str = "mix"
    script_name: str = ""
    scene: str = ""
    tracks: list[Track] = field(default_factory=list)
    path: Path | None = None     # where it was loaded from / saved to

    # ---- lookups ---------------------------------------------------------------------------
    @property
    def stem(self) -> str:
        return self.clip.stem

    def track(self, tag: str) -> Track:
        return next(t for t in self.tracks if t.tag == tag)

    @property
    def targets(self) -> list[Track]:
        """The translated tracks (what gets reviewed); the transcript when there are none."""
        return [t for t in self.tracks if t.tag != "transcript"] or self.tracks[:1]

    def aligned(self, a: Track, b: Track) -> bool:
        """Same lines in both: translations of the same speech, line for line."""
        return len(a.lines) == len(b.lines) and all(
            abs(x.start - y.start) < 1e-3 for x, y in zip(a.lines, b.lines))

    def counterpart(self, track: Track, other: Track, i: int) -> Line | None:
        """The line in `other` for line i of `track`: same index when aligned, else the one
        overlapping it most in time."""
        if not other.lines:
            return None
        if self.aligned(track, other):
            return other.lines[i]
        ln = track.lines[i]
        best = max(other.lines, key=lambda o: min(o.end, ln.end) - max(o.start, ln.start))
        return best if min(best.end, ln.end) > max(best.start, ln.start) else None

    def script_line(self, track: Track, i: int) -> Line | None:
        """The line carrying the script comparison for line i (it is on one track only)."""
        for t in [track] + [t for t in self.tracks if t is not track]:
            ln = self.counterpart(track, t, i) if t is not track else t.lines[i]
            if ln is not None and ln.script_match:
                return ln
        return None

    def issue(self, track: Track, i: int) -> str:
        """Why line i is worth a look: "untranslated", "unsure", "script" or ""."""
        ln = track.lines[i]
        source = self.tracks[0] if self.tracks and self.tracks[0].tag == "transcript" else None
        if track.tag != "transcript" and source is not None and ln.text.strip():
            src = self.counterpart(track, source, i)
            same = (src is not None and src.lang and src.lang != track.lang
                    and _norm(src.text) == _norm(ln.text))
            if same or (track.lang not in _RTL_LANGS and _rtl_share(ln.text) > 0.3):
                return "untranslated"
        if ln.flagged:
            return "unsure"
        script = self.script_line(track, i)
        if (script or ln).script_match == "changed":
            return "script"
        return ""

    def script_wording(self, track: Track, i: int) -> str:
        """The script's words for line i. A script line said over several subtitles is shared
        out sentence by sentence, so using it doesn't repeat what the next subtitle says."""
        script = self.script_line(track, i)
        if not script or not script.script_text:
            return ""
        holder = next(t for t in self.tracks if script in t.lines)
        k = holder.lines.index(script)
        words = script.script_text.partition(": ")[2] or script.script_text
        run = [k]
        while run[0] > 0 and holder.lines[run[0] - 1].script_ref == script.script_ref:
            run.insert(0, run[0] - 1)
        while (run[-1] + 1 < len(holder.lines)
               and holder.lines[run[-1] + 1].script_ref == script.script_ref):
            run.append(run[-1] + 1)
        sentences = _sentences(words)
        if len(run) == 1 or len(sentences) == 1:
            return words
        texts = {r: holder.lines[r].machine_text or holder.lines[r].text for r in run}
        mine = [s for s in sentences
                if max(run, key=lambda r: (_similarity(texts[r], s), -abs(r - k))) == k]
        return " ".join(mine) or max(sentences, key=lambda s: _similarity(texts[k], s))

    # ---- timing ----------------------------------------------------------------------------
    @property
    def frame(self) -> float:
        return float(1 / self.fps.rate) if self.fps else 0.04

    def timed(self, track: Track) -> list[Cue]:
        """On-screen cues, worked out as the pipeline does, with hand-set timing applied."""
        cues = [Cue(ln.start if ln.start_override is None else ln.start_override,
                    ln.end, ln.text, ln.lang, ln.confidence,
                    ln.flagged and not ln.checked, ln.script_ref, ln.script_match)
                for ln in track.lines]
        order = sorted(range(len(cues)), key=lambda k: cues[k].start)
        done = finalize([cues[k] for k in order], self.fps)
        out = [None] * len(cues)
        for k, c in zip(order, done):
            out[k] = c
        for ln, c in zip(track.lines, out):
            if ln.start_override is not None:
                c.start = ln.start_override
            if ln.end_override is not None:
                c.end = ln.end_override
        return out

    def set_timing(self, track: Track, i: int, start: float | None = None,
                   end: float | None = None) -> None:
        """Change line i's on-screen timing, on every track that has the same lines."""
        for t in self.tracks:
            if t is track or self.aligned(track, t):
                ln = t.lines[i]
                if start is not None:
                    ln.start_override = start
                if end is not None:
                    ln.end_override = end

    def reset_timing(self, track: Track, i: int) -> None:
        for t in self.tracks:
            if t is track or self.aligned(track, t):
                t.lines[i].start_override = t.lines[i].end_override = None

    # ---- files -----------------------------------------------------------------------------
    def output_path(self, tag: str, fmt: str) -> Path:
        suffix = ".srt" if fmt == "srt" else ".avid.txt"
        return self.out_dir / f"{self.stem}.{tag}{suffix}"

    def export(self) -> list[Path]:
        """Write every subtitle file again. Checked lines lose their [?]."""
        written = []
        for t in self.tracks:
            cues = [c for c in self.timed(t) if c.text.strip()]
            for fmt in self.formats:
                path = self.output_path(t.tag, fmt)
                if path.resolve() == self.clip.resolve():
                    raise RuntimeError(f"refusing to write over source file {self.clip}")
                if fmt == "srt":
                    _atomic(path, lambda p: write_srt(p, cues, self.mark_flagged))
                elif self.fps:
                    start = self.avid_start or self.start_tc or "00:00:00:00"
                    _atomic(path, lambda p: write_subcap(p, cues, self.fps, start,
                                                         self.mark_flagged))
                else:
                    continue
                written.append(path)
        return written

    def to_json(self) -> dict:
        return {
            "makesubs_review": FORMAT,
            "clip": str(self.clip),
            "fps": None if not self.fps else {"rate": str(self.fps.rate),
                                              "drop_frame": self.fps.drop_frame},
            "start_tc": self.start_tc, "avid_start": self.avid_start,
            "formats": self.formats, "mark_flagged": self.mark_flagged,
            "channel": self.channel, "script": self.script_name, "scene": self.scene,
            "tracks": [{"tag": t.tag, "lang": t.lang,
                        "lines": [asdict(ln) for ln in t.lines]} for t in self.tracks],
        }

    def save(self, path: Path | None = None) -> Path:
        path = Path(path or self.path or self.out_dir / f"{self.stem}{SUFFIX}")
        text = json.dumps(self.to_json(), ensure_ascii=False, indent=1)
        _atomic(path, lambda p: p.write_text(text, encoding="utf-8"))
        self.path = path
        return path


_RTL_LANGS = {"ar", "he", "fa", "ur", "yi", "ps", "sd", "ug"}


def _norm(text: str) -> str:
    return " ".join("".join(c for c in text.lower() if c.isalnum() or c.isspace()).split())


def _rtl_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum("\u0590" <= c <= "\u08ff" for c in letters) / len(letters) if letters else 0.0


def _atomic(path: Path, write) -> None:
    """Write to a temporary file and swap it in, so a crash never leaves half a file."""
    tmp = path.with_name(path.name + ".part")
    write(tmp)
    os.replace(tmp, path)


def load(path: Path) -> Project:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if "makesubs_review" not in data:
        raise ValueError(f"{path.name} is not a MakeSubs review file")
    known = {f.name for f in fields(Line)}
    fps = data.get("fps")
    clip = Path(data["clip"])
    if not clip.exists() and (path.parent / clip.name).exists():
        clip = path.parent / clip.name   # folder moved or opened on another machine
    return Project(
        clip=clip, out_dir=path.parent,
        fps=FrameRate(Fraction(fps["rate"]), fps["drop_frame"]) if fps else None,
        start_tc=data.get("start_tc"), avid_start=data.get("avid_start"),
        formats=data.get("formats") or ["srt"], mark_flagged=data.get("mark_flagged", True),
        channel=data.get("channel", "mix"), script_name=data.get("script", ""),
        scene=data.get("scene", ""),
        tracks=[Track(t["tag"], t["lang"],
                      [Line(**{k: v for k, v in ln.items() if k in known}) for ln in t["lines"]])
                for t in data["tracks"]],
        path=path)


def find_for(clip: Path, out_dir: Path | None = None) -> Path | None:
    """The review file for a clip, if it has one."""
    for folder in filter(None, [out_dir, clip.parent]):
        p = Path(folder) / f"{clip.stem}{SUFFIX}"
        if p.exists():
            return p
    return None


def lines_from(cues: list[Cue], script_lines: dict[str, str] | None = None) -> list[Line]:
    script_lines = script_lines or {}
    # float()/bool(): recognition hands back numpy numbers, which JSON can't store
    return [Line(float(c.start), float(c.end), c.text, c.lang, round(float(c.confidence), 3),
                 bool(c.flagged), c.text, c.script_ref, c.script_match,
                 script_lines.get(c.script_ref, "") if c.script_match else "")
            for c in cues]
