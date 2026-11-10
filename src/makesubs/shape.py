"""Turn recognised words into subtitle cues an editor can use as-is."""
from __future__ import annotations

import math
import textwrap
from dataclasses import dataclass, replace

from .asr import Segment, Word
from .timecode import FrameRate, frames_to_seconds, seconds_to_frames

MAX_LINE = 42          # characters per line (Netflix-style guideline)
MAX_LINES = 2
MAX_DURATION = 7.0     # seconds on screen
MIN_DURATION = 1.0
LINGER = 0.7           # keep text up after speech ends (Whisper word ends also run early)
READING_CPS = 17       # characters per second a viewer can read (Netflix adult guideline)
CLOSE_GAP = 0.5        # a shorter gap to the next cue is closed, so text doesn't flash off/on
MAX_WORD_GAP = 1.0     # a pause longer than this always starts a new cue
MIN_GAP_FRAMES = 2

_SENTENCE_END = (".", "?", "!", "؟", "…")
_CLAUSE_END = (",", "،", ";", "؛", ":")


@dataclass
class Cue:
    start: float
    end: float
    text: str
    lang: str
    confidence: float
    flagged: bool = False
    script_ref: str = ""     # script line it matched, e.g. "S4" (translation with a script)
    script_match: str = ""   # "same", "changed" or "none"; "" when no script was used


def _text(words: list[Word]) -> str:
    return "".join(w.text for w in words).strip()


def _runs(segments: list[Segment]) -> list[tuple[str, list[Word]]]:
    """Group words into runs broken by pauses, language changes and sentence ends."""
    runs: list[tuple[str, list[Word]]] = []
    for seg in segments:
        for w in seg.words:
            if runs:
                lang, cur = runs[-1]
                prev = cur[-1]
                if (lang == seg.lang and w.start - prev.end <= MAX_WORD_GAP
                        and not (prev.text.strip().endswith(_SENTENCE_END)
                                 and len(_text(cur)) >= 15)):
                    cur.append(w)
                    continue
            runs.append((seg.lang, [w]))
    return runs


def _split_balanced(words: list[Word], max_chars: int) -> list[list[Word]]:
    text_len = len(_text(words))
    duration = words[-1].end - words[0].start
    if len(words) == 1 or (text_len <= max_chars and duration <= MAX_DURATION):
        return [words]
    k = max(math.ceil(text_len / max_chars), math.ceil(duration / MAX_DURATION), 2)
    # character position after each word
    ends, pos = [], 0
    for w in words:
        pos += len(w.text)
        ends.append(pos)
    bonus = 0.15 * pos / k
    cuts, last = [], 0
    for j in range(1, k):
        target = pos * j / k
        best = min(range(last + 1, len(words)),
                   key=lambda i: abs(ends[i - 1] - target)
                   - (bonus if words[i - 1].text.strip().endswith(_SENTENCE_END + _CLAUSE_END) else 0),
                   default=None)
        if best is None:
            break
        cuts.append(best)
        last = best
    parts = [words[a:b] for a, b in zip([0] + cuts, cuts + [len(words)]) if words[a:b]]
    if len(parts) == 1:
        return parts
    return [p for part in parts for p in _split_balanced(part, max_chars)]


def build_cues(segments: list[Segment], max_chars: int = MAX_LINE * MAX_LINES) -> list[Cue]:
    cues: list[Cue] = []
    for seg in segments:
        if not seg.words and seg.text:
            cues.append(Cue(seg.start, seg.end, seg.text, seg.lang, 0.0))
    for lang, words in _runs(segments):
        for part in _split_balanced(words, max_chars):
            text = _text(part)
            if text:
                conf = sum(w.prob for w in part) / len(part)
                cues.append(Cue(part[0].start, part[-1].end, text, lang, conf))
    return sorted(cues, key=lambda c: c.start)


def wrap(text: str, max_line: int = MAX_LINE) -> str:
    """Balanced line breaks: two lines normally, more only if the text can't fit otherwise
    (translations run longer than the source; cutting text would be worse)."""
    text = " ".join(text.split())
    if len(text) <= max_line:
        return text
    n = math.ceil(len(text) / max_line)
    width = max(math.ceil(len(text) / n), max(len(w) for w in text.split()))
    while True:
        lines = textwrap.wrap(text, width)
        if len(lines) <= n:
            return "\n".join(lines)
        width += 1


def reading_time(text: str) -> float:
    """Seconds a viewer needs to read the text, never less than MIN_DURATION."""
    return max(MIN_DURATION, len(" ".join(text.split())) / READING_CPS)


def finalize(cues: list[Cue], fps: FrameRate | None) -> list[Cue]:
    """Turn speech timing into on-screen timing.

    Expects cue ends at the end of speech (not already finalized): each cue stays up LINGER
    after speech ends and at least long enough to read its text, and a short gap to the next
    cue is closed. A cue never runs into the next one, so speech start times are kept.
    Boundaries are frame-accurate when the frame rate is known.
    """
    out = [replace(c) for c in sorted(cues, key=lambda c: c.start)]
    frame = frames_to_seconds(1, fps) if fps else 0.04
    gap = MIN_GAP_FRAMES * frame
    for i, c in enumerate(out):
        nxt = out[i + 1].start if i + 1 < len(out) else None
        want = max(c.end + LINGER, c.start + reading_time(c.text))
        if nxt is not None and nxt - want < CLOSE_GAP:
            want = nxt - gap
        c.end = min(want, nxt - gap) if nxt is not None else want
        c.end = max(c.end, c.start + frame)  # never zero-length
        if fps:
            sf = seconds_to_frames(c.start, fps, "floor")
            ef = max(seconds_to_frames(c.end, fps, "ceil"), sf + 1)
            if nxt is not None:
                ef = min(ef, seconds_to_frames(nxt, fps, "floor") - MIN_GAP_FRAMES)
                ef = max(ef, sf + 1)
            c.start, c.end = frames_to_seconds(sf, fps), frames_to_seconds(ef, fps)
    return out
