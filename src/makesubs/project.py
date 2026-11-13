"""The review file: everything needed to check, correct and re-save a clip's subtitles.

The pipeline saves one `<clip>.makesubs.json` next to the subtitle files. The review window
opens it, and saving there writes the subtitle files again from it. Lines are kept at their
speech timing, and the on-screen timing is worked out the same way the pipeline does it, so
a line's timing only differs from the pipeline's where someone changed it by hand.

Subtitles without a review file (an `.srt`, from an earlier version or another tool) open too:
their timing is already on-screen timing and is kept as it is, and only the files someone
changed are written again.
"""
from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from fractions import Fraction
from pathlib import Path

from . import languages
from .shape import Cue, finalize
from .translate import _sentences, _similarity
from .timecode import FrameRate, frames_to_tc, seconds_to_frames, tc_to_frames
from .writers import FLAG, write_srt, write_subcap

SUFFIX = ".makesubs.json"
FORMAT = 1
MUST = ("untranslated", "unsure")   # issues to look at; "script" differences are optional
# What can be played next to the subtitles, best first: picture, then sound only.
VIDEO = (".mov", ".mp4", ".mxf", ".m4v", ".mkv", ".avi", ".mts", ".mpg", ".wmv", ".webm")
AUDIO = (".wav", ".bwf", ".aif", ".aiff", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus")
MEDIA = VIDEO + AUDIO


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
    name: str = ""               # the subtitle files' name; stays when the clip is relinked
    timing: str = "speech"       # "speech": made here; "screen": opened from finished subtitles

    # ---- lookups ---------------------------------------------------------------------------
    @property
    def stem(self) -> str:
        return self.name or self.clip.stem

    @property
    def title(self) -> str:
        """What the window calls it: the clip, and the subtitles' name if that differs."""
        if not self.clip.suffix:
            return self.stem   # no clip or sound yet
        if self.clip.stem == self.stem:
            return self.clip.name
        return f"{self.stem}  ({self.clip.name})"

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
        if self.timing == "screen":
            out = cues   # finished subtitles: their timing is already on-screen timing
        else:
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
        return self.out_dir / (f"{self.stem}.{tag}{suffix}" if tag else f"{self.stem}{suffix}")

    @staticmethod
    def changed(track: Track) -> bool:
        """Anything in the track that would come out differently from what was opened."""
        return any(ln.edited or ln.retimed or (ln.flagged and ln.checked) for ln in track.lines)

    def export(self) -> list[Path]:
        """Write every subtitle file again. Checked lines lose their [?].

        Subtitles opened from an .srt are only written where something changed, and an Avid
        file only where there was one, so files from other tools are left as they are."""
        written = []
        for t in self.tracks:
            if self.timing == "screen" and not self.changed(t):
                continue
            cues = [c for c in self.timed(t) if c.text.strip()]
            for fmt in self.formats:
                path = self.output_path(t.tag, fmt)
                if self.timing == "screen" and fmt != "srt" and not path.exists():
                    continue
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
            "name": self.stem, "timing": self.timing,
            "tracks": [{"tag": t.tag, "lang": t.lang,
                        "lines": [asdict(ln) for ln in t.lines]} for t in self.tracks],
        }

    def save(self, path: Path | None = None) -> Path:
        path = Path(path or self.path or self.out_dir / f"{self.stem}{SUFFIX}")
        text = json.dumps(self.to_json(), ensure_ascii=False, indent=1)
        _atomic(path, lambda p: p.write_text(text, encoding="utf-8"))
        self.path = path
        return path

    def attach(self, media: Path) -> None:
        """Play `media` with these subtitles: the clip, or a copy of its sound. The subtitle
        files keep their name. A clip with a frame rate fills in what the subtitles lacked."""
        self.clip = Path(media)
        if self.fps is None:
            info = _probe(self.clip)
            if info and info.fps:
                self.fps = info.fps
                self.start_tc = self.start_tc or info.start_tc
                if self.timing == "screen":
                    self._avid_from_files()

    def _avid_from_files(self) -> None:
        """Opened subtitles with Avid files next to them: write those again too, from the
        timecode they start at (needs the frame rate, from the clip)."""
        if not self.fps or "avid" in self.formats:
            return
        for t in self.tracks:
            path = self.output_path(t.tag, "avid")
            first = _first_subcap_tc(path)
            cues = [ln for ln in t.lines if ln.text.strip()]
            if first and cues:
                try:
                    base = tc_to_frames(first, self.fps) - seconds_to_frames(cues[0].start,
                                                                             self.fps)
                except ValueError:
                    continue
                self.avid_start = frames_to_tc(max(0, base), self.fps)
                self.formats = self.formats + ["avid"]
                return


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


def find_media(folder: Path, stem: str, name: str = "") -> Path | None:
    """The clip in `folder`: a file called `name`, else one called `stem` with any picture or
    sound extension (A001C003.wav sent along with A001C003's subtitles)."""
    folder = Path(folder)
    if name and (folder / name).is_file():
        return folder / name
    found = [Path(f) for f in glob.glob(os.path.join(glob.escape(str(folder)),
                                                     glob.escape(stem) + ".*"))]
    found = [f for f in found if f.suffix.lower() in MEDIA and f.stem == stem]
    return min(found, key=lambda f: MEDIA.index(f.suffix.lower()), default=None)


def load(path: Path) -> Project:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if "makesubs_review" not in data:
        raise ValueError(f"{path.name} is not a MakeSubs review file")
    known = {f.name for f in fields(Line)}
    fps = data.get("fps")
    clip = Path(data["clip"])
    name = data.get("name") or (path.name[:-len(SUFFIX)] if path.name.endswith(SUFFIX)
                                else clip.stem)
    if not clip.exists():   # folder moved, opened on another machine, or sent with its sound
        clip = find_media(path.parent, name, clip.name) or clip
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
        path=path, name=name, timing=data.get("timing", "speech"))


def find_for(clip: Path, out_dir: Path | None = None) -> Path | None:
    """The review file for a clip, if it has one."""
    for folder in filter(None, [out_dir, clip.parent]):
        p = Path(folder) / f"{clip.stem}{SUFFIX}"
        if p.exists():
            return p
    return None


def _probe(media: Path):
    try:
        from .probe import probe
        return probe(media)
    except Exception:  # noqa: BLE001  (sound only, or a file ffprobe can't read)
        return None


def _first_subcap_tc(path: Path) -> str | None:
    if not path.is_file():
        return None
    m = re.search(r"^(\d\d:\d\d:\d\d[:;]\d\d) \d", path.read_text("utf-8-sig", "replace"),
                  re.M)
    return m.group(1) if m else None


_SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*"
                       r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def read_srt(path: Path) -> list[tuple[float, float, str]]:
    """(start, end, text) per subtitle; line breaks inside a subtitle become spaces."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    def secs(h, m, s, frac):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000

    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        rows = block.strip("\n").split("\n")
        k = next((j for j, r in enumerate(rows) if _SRT_TIME.search(r)), None)
        if k is None:
            continue
        g = _SRT_TIME.search(rows[k]).groups()
        start, end = secs(*g[:4]), secs(*g[4:])
        out.append((start, end, " ".join(" ".join(rows[k + 1:]).split())))
    return out


def _split_srt_name(path: Path) -> tuple[str, str]:
    """"A001C003.en.srt" -> ("A001C003", "en"); "Film.srt" -> ("Film", "")."""
    base = Path(path).name[:-len(Path(path).suffix)]
    head, dot, tag = base.rpartition(".")
    if dot and head and (tag == "transcript" or tag.lower() in languages.TARGETS):
        return head, tag
    return base, ""


def from_srt(srt: Path, media: Path | None = None) -> Project:
    """Open finished subtitles for review, with the clip or its sound when there is one.

    The other subtitle files of the same clip next to it come along (A001C003.en.srt brings
    A001C003.transcript.srt, shown as what was heard). Lines marked [?] are the unsure ones."""
    srt = Path(srt)
    stem, _ = _split_srt_name(srt)
    folder = srt.parent
    files = {srt}
    files |= {Path(f) for f in glob.glob(os.path.join(glob.escape(str(folder)),
                                                      glob.escape(stem) + ".*.srt"))}
    tracks = []
    for f in files:
        f_stem, tag = _split_srt_name(f)
        if f_stem != stem or f.suffix.lower() != ".srt":
            continue
        lang = "" if tag == "transcript" else tag.lower()
        lines = []
        for start, end, text in read_srt(f):
            flagged = text.startswith(FLAG.strip())
            if flagged:
                text = text[len(FLAG.strip()):].strip()
            lines.append(Line(start, end, text, lang, 1.0, flagged, text))
        tracks.append(Track(tag, lang, lines))
    tracks.sort(key=lambda t: (t.tag != "transcript", t.tag != srt_tag(srt), t.tag))
    proj = Project(clip=folder / stem, out_dir=folder, fps=None, start_tc=None,
                   formats=["srt"], tracks=tracks, name=stem, timing="screen")
    media = media or find_media(folder, stem)
    if media:
        proj.attach(media)
    return proj


def srt_tag(path: Path) -> str:
    return _split_srt_name(path)[1]


def open_any(path: Path, media: Path | None = None) -> Project:
    """A review file, or an .srt (whose review file is used instead when it has one)."""
    path = Path(path)
    if path.suffix.lower() == ".srt":
        stem, _ = _split_srt_name(path)
        review = path.parent / f"{stem}{SUFFIX}"
        proj = load(review) if review.is_file() else from_srt(path, media)
    else:
        proj = load(path)
    if media and not proj.clip.exists():
        proj.attach(media)
    return proj


def open_many(paths: list[Path]) -> tuple[list[Project], list[str]]:
    """Projects for what someone picked or dropped: review files and .srt files, with any
    clips or sound files among them matched to the subtitles by name (or, one of each, to
    each other). Returns the projects and the problems, one sentence each."""
    paths = [Path(p) for p in paths]
    media = [p for p in paths if p.suffix.lower() in MEDIA]
    subs = [p for p in paths if p.name.endswith(SUFFIX) or p.suffix.lower() == ".srt"]
    problems = [f"{p.name}: not subtitles, a review file or a clip"
                for p in paths if p not in media and p not in subs]
    seen, groups = set(), []
    for p in subs:
        stem = p.name[:-len(SUFFIX)] if p.name.endswith(SUFFIX) else _split_srt_name(p)[0]
        key = (str(p.parent).lower(), stem)
        if key not in seen:   # one clip's .srt files and its review file open once
            seen.add(key)
            groups.append((p, stem))
    used = set()
    projects = []
    for p, stem in groups:
        clip = next((m for m in media if m.stem == stem), None)
        if clip is None and len(groups) == 1 and len(media) == 1:
            clip = media[0]
        try:
            projects.append(open_any(p, clip))
            used.add(clip)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{p.name}: {e}")
    problems += [f"{m.name}: no subtitles with that name" for m in media if m not in used]
    return projects, problems


def lines_from(cues: list[Cue], script_lines: dict[str, str] | None = None) -> list[Line]:
    script_lines = script_lines or {}
    # float()/bool(): recognition hands back numpy numbers, which JSON can't store
    return [Line(float(c.start), float(c.end), c.text, c.lang, round(float(c.confidence), 3),
                 bool(c.flagged), c.text, c.script_ref, c.script_match,
                 script_lines.get(c.script_ref, "") if c.script_match else "")
            for c in cues]
