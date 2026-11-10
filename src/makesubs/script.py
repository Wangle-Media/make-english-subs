"""Read a screenplay (Final Draft .fdx or PDF) and find the scene a clip comes from.

The script is a reference for the translator: where an actor says a scripted line, the
subtitle can use the script's wording. Only dialogue is kept, grouped by scene.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

SUFFIXES = (".fdx", ".pdf")


@dataclass
class Line:
    character: str
    text: str


@dataclass
class Scene:
    index: int        # 0-based position in the script
    number: str       # scene number as printed, "" if the script has none
    heading: str
    lines: list[Line] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"scene {self.number}" if self.number else f"scene #{self.index + 1}"


@dataclass
class Script:
    path: Path
    scenes: list[Scene]
    lang: str = ""    # two-letter code, "" if unknown

    @property
    def characters(self) -> list[str]:
        counts = Counter(ln.character for s in self.scenes for ln in s.lines if ln.character)
        return [c for c, _n in counts.most_common()]

    @property
    def line_count(self) -> int:
        return sum(len(s.lines) for s in self.scenes)

    def summary(self) -> str:
        from .languages import name
        lang = name(self.lang) if self.lang else "language unknown"
        return (f"Script {self.path.name}: {len(self.scenes)} scenes, {self.line_count} lines of "
                f"dialogue, {len(self.characters)} characters, {lang}")

    def find(self, wanted: str) -> Scene | None:
        """A scene by its printed number, or by position when the script has no numbers."""
        w = wanted.strip().upper().rstrip(".")
        for s in self.scenes:
            if s.number and s.number.upper() == w:
                return s
        if w.isdigit() and not any(s.number for s in self.scenes) and 0 < int(w) <= len(self.scenes):
            return self.scenes[int(w) - 1]
        return None


def load(path: Path) -> Script:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".fdx":
        scenes = _read_fdx(path)
    elif suffix == ".pdf":
        scenes = _read_pdf(path)
    else:
        raise RuntimeError(f"{path.name}: scripts must be Final Draft (.fdx) or PDF")
    scenes = [s for s in scenes if s.lines]
    for i, s in enumerate(scenes):
        s.index = i
    if not scenes:
        raise RuntimeError(f"{path.name}: found no scenes with dialogue. Is it a screenplay? "
                           "A scanned PDF (pictures of pages) can't be read.")
    text = " ".join(ln.text for s in scenes for ln in s.lines)
    return Script(path, scenes, detect_language(text))


# ---------------------------------------------------------------- reading

_EXTENSION = re.compile(r"\s*\((V\.?\s?O|O\.?\s?S|O\.?\s?C|CONT'?D|CONT|FORTS?|OFF|ON SCREEN|"
                        r"PRE-?LAP|FILTERED|PHONE|TELEFON|INTO PHONE|SUBTITLED?)[.\s]*\)", re.I)


def _character(name: str) -> str:
    name = _EXTENSION.sub("", name).replace("^", "")
    return " ".join(name.split()).upper()


def _read_fdx(path: Path) -> list[Scene]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise RuntimeError(f"{path.name}: not a readable Final Draft file ({e})") from e
    content = root.find("Content")
    if content is None:
        raise RuntimeError(f"{path.name}: not a Final Draft script (no <Content>)")
    scenes: list[Scene] = []
    speaker = ""
    for p in content.iter("Paragraph"):  # document order, including dual dialogue
        kind = p.get("Type")
        text = " ".join("".join(t.text or "" for t in p.findall("Text")).split())
        if kind == "Scene Heading":
            scenes.append(Scene(len(scenes), (p.get("Number") or "").strip(), text.upper()))
            speaker = ""
        elif kind == "Character":
            speaker = _character(text)
        elif kind == "Dialogue":
            if text and speaker:
                if not scenes:
                    scenes.append(Scene(0, "", ""))
                scenes[-1].lines.append(Line(speaker, text))
        elif kind and kind != "Parenthetical":
            speaker = ""  # action, transition, shot: the speech is over
    return scenes


_HEADING = re.compile(
    r"^(?:(?P<num>[A-Z]{0,2}\d+[A-Z]{0,3})\.?\s+)?"
    r"(?P<head>(?:INT|EXT|I/E|E/I|INT\.?\s?/\s?EXT|EXT\.?\s?/\s?INT|IND|UD|UDV|INDE|UDE)[.\s/-].*?)"
    r"(?:\s{2,}[A-Z]{0,2}\d+[A-Z]{0,3}\.?)?\s*$")
_NUMBERED = re.compile(  # "12   KITCHEN - NIGHT   12" without INT/EXT
    r"^(?P<num>[A-Z]{0,2}\d+[A-Z]{0,3})\.?\s{2,}(?P<head>\S.*?)(?:\s{2,}(?P=num)\.?)?\s*$")
_PAGE_NOISE = re.compile(
    r"^\(?(MORE|CONTINUED|CONT'?D|FORTSAT|FORTS\.?)\)?:?$|^\d+\.?$|^CONTINUED:", re.I)


def _is_upper(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and not any(c.islower() for c in letters)


def _read_pdf(path: Path) -> list[Scene]:
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        pages = [p.extract_text(extraction_mode="layout") for p in reader.pages]
    except Exception as e:  # noqa: BLE001 - pypdf raises many kinds
        raise RuntimeError(f"{path.name}: could not read the PDF ({e})") from e
    return parse_layout([ln.rstrip() for page in pages for ln in page.splitlines() + [""]])


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def parse_layout(rows: list[str]) -> list[Scene]:
    """Classify screenplay lines (as laid out on the page) by how far they are indented.

    Standard layout, measured from the action margin: dialogue about 1 inch in, parentheticals
    about 1.5, character names about 2.2, transitions (CUT TO:) right-aligned. pypdf's layout
    mode keeps that as roughly 10 characters per inch.
    """
    heading_at = {}
    for i, row in enumerate(rows):
        s = row.strip()
        m = _HEADING.match(s) or (_NUMBERED.match(s) if _is_upper(s) else None)
        if m and _is_upper(m.group("head")) and len(m.group("head")) <= 70:
            heading_at[i] = m
    # The action margin: where scene headings and long action lines start.
    starts = [_indent(rows[i]) + rows[i].strip().find(m.group("head")) for i, m in heading_at.items()]
    starts += [_indent(r) for r in rows if len(r.strip()) >= 45]
    if not starts:
        return []
    margin = Counter(starts).most_common(1)[0][0]

    scenes: list[Scene] = []
    speaker = ""
    speech: Line | None = None  # the speech being read, continued line by line
    in_paren = False
    for i, row in enumerate(rows):
        s = row.strip()
        if i in heading_at:
            m = heading_at[i]
            scenes.append(Scene(len(scenes), m.group("num") or "", " ".join(m.group("head").split())))
            speaker, speech, in_paren = "", None, False
            continue
        if not s:
            speaker, speech, in_paren = "", None, False
            continue
        if _PAGE_NOISE.match(s):
            continue
        rel = _indent(row) - margin
        if speaker and (in_paren or (s.startswith("(") and rel >= 12)):
            in_paren = not s.endswith(")")
            continue
        if 17 <= rel <= 40 and _is_upper(s) and not s.endswith(":") and len(s) <= 45:
            speaker, speech = _character(s), None
            continue
        if speaker and 4 <= rel < 17:
            if speech is None:
                if not scenes:
                    scenes.append(Scene(0, "", ""))
                speech = Line(speaker, s)
                scenes[-1].lines.append(speech)
            else:
                speech.text = re.sub(r"(\w)-$", r"\1", speech.text) + ("" if speech.text.endswith("-") else " ") + s
            continue
        speaker, speech = "", None  # action, transition or anything else ends the speech
    return scenes


# ---------------------------------------------------------------- language

_STOPWORDS = {
    "en": "the and you that is was for are with have this not what but they your it's don't".split(),
    "da": "og det er jeg ikke du har at en til på med vi der som jo hvad skal kan".split(),
    "sv": "och det är jag inte du har att en till på med vi som vad ska kan".split(),
    "no": "og det er jeg ikke du har at en til på med vi som hva skal kan".split(),
    "de": "und der die das ist ich nicht du ein zu mit wir was aber sie es".split(),
    "fr": "et le la les est je ne pas tu un une de avec nous que vous".split(),
    "es": "y el la los es yo no tú un una de con nosotros que por".split(),
    "nl": "en de het is ik niet je een te met wij wat maar zij dat".split(),
}


def detect_language(text: str) -> str:
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", text.lower())
    if len(words) < 30:
        return ""
    counts = Counter(words)
    score = {lang: sum(counts[w] for w in ws) / len(words) for lang, ws in _STOPWORDS.items()}
    best = max(score, key=score.get)
    return best if score[best] >= 0.08 else ""


# ---------------------------------------------------------------- matching

_EN_STOP = set("""a about after again all also am an and any are as at be because been before
being but by can could did do does doing don't down for from get go going got had has have he
her here hers him his how i i'm if in into is it it's its just know let like me more my no not
now of off oh ok okay on one only or our out over please really right said say see she so some
than that that's the their them then there they this to too up us very want was we well were
what when where which who why will with would yeah yes you you're your yours""".split())


def _key_words(text: str) -> dict[str, str]:
    """Key words of a text, as {comparable form: word as written}."""
    out: dict[str, str] = {}
    for raw in re.findall(r"[^\W_]+(?:'[^\W\d_]+)?", text.lower()):
        w = raw[:-2] if raw.endswith("'s") else raw
        if w in _EN_STOP or (len(w) < 3 and not w.isdigit()):
            continue
        shown = w
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):  # plurals match singulars
            w = w[:-1]
        out.setdefault(w, shown)
    return out


def _tokens(text: str) -> list[str]:
    return list(_key_words(text))


@dataclass
class Match:
    scenes: list[Scene]        # best scene first, then any neighbour the clip may run into
    coverage: float            # share of the clip's key words found in the best scene
    words: list[str]           # key words that matched
    by_hand: bool = False

    @property
    def label(self) -> str:
        return " + ".join(s.label for s in self.scenes)


MIN_COVERAGE = 0.2   # below this the best scene is only a guess and the script is not used
MIN_WORDS = 3
NEIGHBOUR = 0.6      # include an adjacent scene that scores at least this share of the best


class SceneFinder:
    """Find the scene a clip comes from by comparing its (rough English) text with the script.

    Words are weighted by how rare they are across scenes, so names, places and unusual words
    decide the match, not words every scene has.
    """

    def __init__(self, script: Script):
        self.script = script
        self.scene_words = [set(_tokens(" ".join(f"{ln.character} {ln.text}" for ln in s.lines)))
                            for s in script.scenes]
        n = len(script.scenes)
        df = Counter(w for ws in self.scene_words for w in ws)
        self.idf = {w: math.log((n + 1) / d) for w, d in df.items()}
        self.unseen = math.log(n + 1)

    def locate(self, text: str) -> tuple[Match | None, Match | None]:
        """Return (match, best_guess). match is None when nothing matches confidently."""
        shown = _key_words(text)
        words = set(shown)
        if not words:
            return None, None
        total = sum(self.idf.get(w, self.unseen) for w in words)
        scores = [sum(self.idf[w] for w in words & sw) / total for sw in self.scene_words]
        best = max(range(len(scores)), key=scores.__getitem__)
        picked = [self.script.scenes[best]]
        for j in (best - 1, best + 1):
            if 0 <= j < len(scores) and scores[j] >= NEIGHBOUR * scores[best] > 0:
                picked.append(self.script.scenes[j])
        hit = sorted(words & self.scene_words[best], key=lambda w: -self.idf[w])
        m = Match(picked, scores[best], [shown[w] for w in hit])
        ok = scores[best] >= MIN_COVERAGE and len(hit) >= MIN_WORDS
        return (m if ok else None), m


def excerpt(scenes: list[Scene]) -> list[tuple[str, Line]]:
    """Script lines numbered S1, S2, ... across the given scenes, in script order."""
    out, n = [], 0
    for s in sorted(scenes, key=lambda s: s.index):
        for ln in s.lines:
            n += 1
            out.append((f"S{n}", ln))
    return out
