"""Subtitle translation with a local LLM served by Ollama (optional quality tier)."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import languages
from .shape import Cue

DEFAULT_MODEL = "gemma3:12b"
HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not HOST.startswith("http"):
    HOST = "http://" + HOST

BATCH = 40
COMPARE_BATCH = 20   # smaller: matching against the script drifts more on long lists
CONTEXT = 6


def language_name(code: str) -> str:
    return languages.name(code)


def _call(path: str, payload: dict | None = None, timeout: float = 1800):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(HOST + path, data=data,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def available() -> bool:
    try:
        with _call("/api/tags", timeout=3):
            return True
    except (urllib.error.URLError, OSError):
        return False


def ensure_model(model: str, progress: Callable[[str], None] = print) -> None:
    with _call("/api/tags", timeout=10) as r:
        names = {m["name"] for m in json.load(r).get("models", [])}
    if model in names or f"{model}:latest" in names:
        return
    progress(f"Downloading translation model {model} (one-time, several GB)...")
    last = ""
    with _call("/api/pull", {"model": model, "stream": True}, timeout=None) as r:
        for line in r:
            msg = json.loads(line)
            if "error" in msg:
                raise RuntimeError(f"Ollama could not download {model}: {msg['error']}")
            status = msg.get("status", "")
            if msg.get("total") and msg.get("completed"):
                status += f" {100 * msg['completed'] // msg['total']}%"
            if status != last and (status.endswith("0%") or "%" not in status):
                progress("  " + status)
                last = status


def unload(model: str) -> None:
    try:
        with _call("/api/generate", {"model": model, "keep_alive": 0}, timeout=30):
            pass
    except (urllib.error.URLError, OSError):
        pass


@dataclass
class ScriptRef:
    """The part of the screenplay a clip comes from, for the translator to follow."""
    lines: list[tuple[str, str, str]]   # (ref "S1", character, text)
    lang: str                           # language the script is written in, "" if unknown

    def text(self) -> str:
        return "\n".join(f"{ref} {who}: {said}" if who else f"{ref} {said}"
                         for ref, who, said in self.lines)


def _compare_prompt(lines: dict[str, str], script: ScriptRef, target: str) -> str:
    """Second pass: match subtitles of what was said against the script.

    The subtitles are translated first without the script, so what the actors really said
    cannot be pulled towards the script; this pass only decides how each line relates to it.
    """
    slang = language_name(script.lang) if script.lang else "its own language"
    reword = script.lang == target
    parts = [
        "You are checking film subtitles against the screenplay. The actors performed a "
        "translation of the screenplay, and often changed lines, skipped lines or improvised.",
        f"Screenplay excerpt ({slang}):\n<script>\n{script.text()}\n</script>",
        "The numbered lines are subtitles of what the actors ACTUALLY said, in order. For each "
        "one decide how it relates to the script:\n"
        "- \"same\": it means what a script line says, or part of one. The subtitles are a "
        "translation of a translation and come from speech recognition, so the words are "
        "almost never identical: judge the meaning, not the wording.\n"
        "- \"changed\": it is based on a script line, but the meaning differs: another number, "
        "time, day, month, name, place or person, or something added or left out that "
        "matters.\n"
        "- \"none\": it is not in the script (improvised, between takes, crew talk).\n"
        "Examples: \"Shut the door.\" for \"Close the door, please.\" is \"same\"; \"I stayed "
        "at Omar's.\" for \"I slept at Omar's house.\" is \"same\"; \"Meet me at seven.\" for "
        "\"Meet me at six.\" is \"changed\"; \"Meet me at six, and bring Sara.\" for \"Meet me "
        "at six.\" is \"changed\". Script lines are usually spoken in order, but any of them "
        "may be left out; never match a subtitle to a script line it does not say.",
    ]
    if reword:
        parts.append(
            "For \"same\", \"text\" is the script's own wording for the part spoken in this "
            "subtitle (a script line is often split over several subtitles: give only the part "
            "this subtitle covers, never the rest of the line). For \"changed\" and \"none\", "
            "\"text\" is the subtitle exactly as given.")
    else:
        parts.append("\"text\" is the subtitle exactly as given.")
    parts.append(
        "Return ONLY a JSON object with exactly the same keys, mapping each line number to "
        '{"script": the matching script line such as "S4" or null, "match": "same", "changed" '
        'or "none", "text": the subtitle text}.\n\n'
        + json.dumps(lines, ensure_ascii=False, indent=0))
    return "\n\n".join(parts)


_NUMBER_WORDS = {w: str(n) for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_NUMBER_WORDS.update({"thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
                      "seventy": "70", "eighty": "80", "ninety": "90", "hundred": "100",
                      "thousand": "1000", "noon": "12:00", "midday": "12:00",
                      "midnight": "0:00", "half": "1/2"})
_CALENDAR = set(("january february march april may june july august september october "
                 "november december monday tuesday wednesday thursday friday saturday "
                 "sunday").split())


def _facts(text: str) -> set[str]:
    """Numbers, times, months and weekdays in an English line, normalised."""
    out = set()
    for w in re.findall(r"\d+(?::\d+)?|[a-z]+", text.lower()):
        if w[0].isdigit():
            out.add(w)
        elif w in _NUMBER_WORDS:
            out.add(_NUMBER_WORDS[w])
        elif w in _CALENDAR and w != "may":  # "may" is far more often the verb
            out.add(w)
    return out


def _prompt(lines: dict[str, str], target: str, context: list[str], draft: str | None,
            note: str, source_langs: list[str]) -> str:
    spoken = " and ".join(language_name(c) for c in source_langs) or "various languages"
    lang = language_name(target)
    parts = [
        "You are a professional subtitle translator working on film dailies.",
        f"The numbered lines are an automatic transcript of on-set speech in {spoken} "
        "(possibly a regional dialect, possibly mixed languages). The transcript can contain "
        "recognition errors; use context to infer what was actually said, but never invent "
        "content that is not there.",
    ]
    if note:
        parts.append(f"Production notes (names, places, terms): {note}")
    if draft:
        parts.append("Reference only: a rough English machine translation of the same audio. "
                     "It may drop or invent sentences. Use it ONLY to check how names and places "
                     f"are spelled; do not copy its wording.\n<reference>\n{draft}\n</reference>")
    if context:
        parts.append(f"Previous subtitles, already in {lang} (context only):\n" + "\n".join(context))
    rules = (f"Translate every numbered line into natural, concise {lang} suitable for subtitles. "
             f"Every output line must be written in {lang}. Keep names, places and numbers. Keep "
             "one output line per input line: do not merge, split, reorder or skip lines. If a "
             "line is unintelligible, translate what you can.\n"
             "Return ONLY a JSON object mapping each line number to its translation, with "
             "exactly the same keys.")
    parts.append(rules + "\n\n" + json.dumps(lines, ensure_ascii=False, indent=0))
    return "\n\n".join(parts)


_EN_WORDS = {  # common English function words that are not also common Danish/Nordic words
    "the", "and", "is", "are", "was", "were", "of", "to", "in", "it", "that", "you", "we",
    "they", "he", "she", "his", "her", "have", "has", "with", "this", "from", "when", "what",
    "my", "a", "an", "be", "not", "than", "more", "one", "there", "but", "so", "can", "will",
    "very", "all", "some", "other", "like", "at", "on", "by", "or", "as", "if", "me", "your",
    "our", "their", "people", "about", "because", "would", "which", "who"}


def _looks_english(text: str) -> bool:
    words = [w.strip(".,!?;:'\"").lower() for w in text.split()]
    words = [w for w in words if w]
    return len(words) >= 3 and sum(w in _EN_WORDS for w in words) / len(words) >= 0.25


@dataclass
class Answer:
    text: str
    script_ref: str = ""
    match: str = ""


_MATCHES = ("same", "changed", "none")


def parse_answer(value) -> Answer:
    """One line of the model's JSON: a plain string, or {"text", "script", "match"}."""
    if isinstance(value, dict):
        text = str(value.get("text") or "").strip()
        ref = value.get("script")
        ref = f"S{ref}" if isinstance(ref, int) else str(ref or "").strip().upper()
        m = re.match(r"S\s*(\d+)\b", ref)  # models sometimes quote the whole line: "S4 NIDAL: ..."
        ref = f"S{m.group(1)}" if m else ""
        match = str(value.get("match") or "").strip().lower()
        match = match if match in _MATCHES else ("none" if not ref else "")
        if match == "none":
            ref = ""
        return Answer(text, ref, match)
    return Answer(str(value).strip() if value is not None else "")


def _generate_raw(model: str, prompt: str, num_ctx: int = 8192) -> dict:
    # think=False: models that reason before answering (Qwen3 and the like) are much slower
    # with it and can break the JSON; models without it reject the setting, so retry without.
    payload = {"model": model, "prompt": prompt, "stream": False, "format": "json",
               "think": False, "keep_alive": "5m",
               "options": {"temperature": 0.2, "num_ctx": num_ctx}}
    try:
        r = _call("/api/generate", payload)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        payload.pop("think")
        r = _call("/api/generate", payload)
    with r:
        body = json.load(r)
    try:
        out = json.loads(body.get("response", ""))
    except json.JSONDecodeError:
        return {}
    return {str(k): v for k, v in out.items()} if isinstance(out, dict) else {}


def _generate(model: str, prompt: str, num_ctx: int = 8192) -> dict[str, Answer]:
    return {k: parse_answer(v) for k, v in _generate_raw(model, prompt, num_ctx).items()}


def _batches(n: int, size: int | None = None) -> list[range]:
    """Split n lines into even batches of at most size (42 lines: 21 + 21, not 40 + 2)."""
    if n <= 0:
        return []
    count = -(-n // (size or BATCH))
    step = -(-n // count)
    return [range(b, min(b + step, n)) for b in range(0, n, step)]


def _draft_for(draft, start: float, end: float) -> str | None:
    """The part of the rough English draft spoken between start and end (seconds).

    Given the whole clip's draft, the model tends to translate a short batch from the
    draft's first sentences instead of from its own lines.
    """
    if draft is None or isinstance(draft, str):
        return draft or None
    return " ".join(s.text.strip() for s in draft if s.end > start - 2 and s.start < end + 2) or None


def translate_cues(cues: list[Cue], target: str, model: str = DEFAULT_MODEL,
                   draft=None, note: str = "",
                   progress: Callable[[str], None] = print,
                   script: ScriptRef | None = None) -> list[Cue]:
    """Return new cues with translated text and the same timing as the input.

    draft is Whisper's rough English: timed segments (each batch then sees only its own part)
    or plain text. With a script, each cue also records which script line it matched and how
    (script_ref, script_match). That is a second pass over the finished translation, so the
    script can only replace a line's wording when the line says what the script says.
    """
    out = [Cue(c.start, c.end, c.text, target, c.confidence, c.flagged) for c in cues]
    todo = [i for i, c in enumerate(cues) if c.lang != target]
    done_context: list[str] = []
    for rng in _batches(len(todo)):
        idx = [todo[k] for k in rng]
        progress(f"  translating to {language_name(target)}: lines {rng.start + 1}-{rng.stop} of {len(todo)}")
        lines = {str(n + 1): cues[i].text for n, i in enumerate(idx)}
        langs = list(dict.fromkeys(cues[i].lang for i in idx))
        part = _draft_for(draft, cues[idx[0]].start, cues[idx[-1]].end)
        prompt = _prompt(lines, target, done_context[-CONTEXT:], part, note, langs)
        result = _generate(model, prompt)
        bad = [k for k in lines if k not in result or not result[k].text
               or (target != "en" and cues[idx[int(k) - 1]].lang != "en"
                   and _looks_english(result[k].text))]
        if bad:  # retry once, only the empty / wrong-language lines, without the English draft
            retry = _generate(model, _prompt({k: lines[k] for k in bad}, target,
                                             done_context[-CONTEXT:], None, note, langs))
            result.update({k: v for k, v in retry.items() if v.text})
        for n, i in enumerate(idx):
            ans = result.get(str(n + 1))
            if ans and ans.text:
                out[i].text = ans.text
            else:
                out[i].flagged = True  # leave source text, mark for review
            done_context.append(out[i].text)
    if script:
        compare_script(out, script, target, model, progress)
    return out


def compare_script(cues: list[Cue], script: ScriptRef, target: str, model: str = DEFAULT_MODEL,
                   progress: Callable[[str], None] = print) -> None:
    """Set script_ref/script_match on translated cues and use the script's wording for lines
    said as scripted.

    When the script is in the subtitle language, the code pairs each subtitle with a script
    line (align: shared words, script order) and the model only judges each pair; asked to
    find the lines itself, it loses its place on a long scene. Otherwise the model finds them,
    and the subtitles keep their translated wording.
    """
    for c in cues:
        c.script_match, c.script_ref = "none", ""
    if script.lang != target:
        _find_in_script(cues, script, target, model, progress)
        return
    refs = [ref for ref, _, _ in script.lines]
    texts = [said for _, _, said in script.lines]
    paired = [(c, k) for c, k in zip(cues, align([c.text for c in cues], texts))
              if k is not None and not c.flagged]
    for rng in _batches(len(paired), COMPARE_BATCH):
        batch = paired[rng.start:rng.stop]
        progress(f"  comparing with the script: {rng.start + 1}-{rng.stop} of {len(paired)} "
                 "lines that look scripted")
        pairs = {str(n + 1): {"said": c.text, "script": texts[k]} for n, (c, k) in enumerate(batch)}
        verdicts = _generate_raw(model, _judge_prompt(pairs), 8192)
        for n, (c, k) in enumerate(batch):
            v = verdicts.get(str(n + 1))
            v = (v.get("match") if isinstance(v, dict) else v) or ""
            v = str(v).strip().lower()
            if v in ("same", "changed"):
                c.script_match, c.script_ref = v, refs[k]

    # Use the script's wording only where the subtitle demonstrably says it: the part of the
    # script line it shares words with (a line said over several subtitles gives each its own
    # part), most of its words in common, and the same numbers, times and dates.
    for c in cues:
        if c.script_match != "same":
            continue
        parts = _sentences(texts[refs.index(c.script_ref)])
        hit = [j for j, part in enumerate(parts) if _similarity(c.text, part)]
        new = " ".join(parts[hit[0]:hit[-1] + 1]) if hit else ""
        if not new or _similarity(c.text, new) < 0.5 or _facts(c.text) != _facts(new):
            c.script_match = "changed"  # not clearly what was said: keep the translation
        else:
            c.text = new


def _judge_prompt(pairs: dict[str, dict]) -> str:
    return "\n\n".join([
        "You are checking film subtitles against the screenplay. The actors performed a "
        "translation of the screenplay, and often changed or improvised lines.",
        "Each numbered item pairs a subtitle of what an actor ACTUALLY said with the script line "
        "it was matched to. The subtitles are a translation of a translation and come from "
        "speech recognition, so the words are almost never identical: judge the meaning.\n"
        "- \"same\": the subtitle says what the script line says, or part of it.\n"
        "- \"changed\": it is the script line, but the actor changed the meaning: another "
        "number, time, day, month, name, place or person, or something added or left out "
        "that matters.\n"
        "- \"none\": the subtitle is not this script line at all.\n"
        "Examples: \"Shut the door.\" / \"Close the door, please.\" is \"same\"; \"I stayed "
        "at Omar's.\" / \"I slept at Omar's house.\" is \"same\"; \"Meet me at seven.\" / "
        "\"Meet me at six.\" is \"changed\"; \"Where is the car?\" / \"Meet me at six.\" is "
        "\"none\".",
        "Return ONLY a JSON object with the same keys, mapping each number to \"same\", "
        "\"changed\" or \"none\".\n\n" + json.dumps(pairs, ensure_ascii=False, indent=0),
    ])


def _find_in_script(cues: list[Cue], script: ScriptRef, target: str, model: str,
                    progress: Callable[[str], None]) -> None:
    """For a script in another language than the subtitles: the model finds the lines."""
    refs = {ref for ref, _, _ in script.lines}
    for rng in _batches(len(cues), COMPARE_BATCH):
        batch = cues[rng.start:rng.stop]
        progress(f"  comparing with the script: lines {rng.start + 1}-{rng.stop} of {len(cues)}")
        lines = {str(n + 1): c.text for n, c in enumerate(batch)}
        result = _generate(model, _compare_prompt(lines, script, target), 12288)
        for n, c in enumerate(batch):
            ans = result.get(str(n + 1))
            if ans and ans.match in ("same", "changed") and ans.script_ref in refs \
                    and not c.flagged:
                c.script_match, c.script_ref = ans.match, ans.script_ref


def align(said: list[str], scripted: list[str]) -> list[int | None]:
    """Pair each subtitle with the script line it most likely says, keeping the script's order.

    Actors mostly follow the script but skip, improvise and repeat lines, so this is a
    sequence alignment on shared words: subtitles may stay unpaired, script lines may be
    skipped, and several subtitles in a row may share one script line (a line said over
    several subtitles). Returns the script line index per subtitle, or None.
    """
    n, m = len(said), len(scripted)
    words_a = [_words(t) for t in said]
    words_b = [_words(t) for t in scripted]
    one_sentence = [len(_sentences(t)) <= 1 for t in scripted]

    def gain(i, k):
        a, b = words_a[i], words_b[k]
        shared = len(a & b)
        if not a or not b:  # no content words ("Us?"): only the very same line pairs
            return 1.0 if _similarity(said[i], scripted[k]) else 0.0
        if shared >= 2 or (shared == 1 and min(len(a), len(b)) <= 2 and max(len(a), len(b)) <= 5):
            return shared / min(len(a), len(b)) + 0.05 * shared
        return 0.0

    low = float("-inf")
    # score[i][j]: best total for the first i subtitles when the last paired script line is
    # j - 1 (j = 0: none paired yet); back[i][j]: (previous j, line paired with subtitle i-1)
    score = [[low] * (m + 1) for _ in range(n + 1)]
    back: list[list] = [[None] * (m + 1) for _ in range(n + 1)]
    score[0][0] = 0.0
    for i in range(n):
        for j in range(m + 1):
            if score[i][j] == low:
                continue
            if score[i][j] > score[i + 1][j]:  # subtitle i unpaired
                score[i + 1][j], back[i + 1][j] = score[i][j], (j, None)
            for k in range(max(j - 1, 0), m):  # the same line as before, or a later one
                g = gain(i, k)
                if not g:
                    continue
                if k == j - 1 and one_sentence[k]:
                    g *= 0.5  # a one-sentence line said twice is less likely than the next line
                if score[i][j] + g > score[i + 1][k + 1]:
                    score[i + 1][k + 1], back[i + 1][k + 1] = score[i][j] + g, (j, k)
    out: list[int | None] = [None] * n
    j = max(range(m + 1), key=lambda j: score[n][j])
    for i in range(n, 0, -1):
        j, out[i - 1] = back[i][j]
    return out


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s]


_COMMON = set("a an the and or but to of in on at for with from by as is are was were be been "
              "being am have has had do does did i you he she it we they me him her us them my "
              "your his our their this that these those so not no yes oh just very too also "
              "what how why when where who which there here then now can could will would "
              "should shall may might must get got go going one all some any like don didn doesn "
              "isn aren wasn weren hasn hadn won wouldn couldn shouldn".split()
              + "s t re ve ll d m".split())  # contractions split off: you're, don't, I'll


def _words(text: str) -> set[str]:
    """Content words, roughly stemmed ("driving" and "drive" count as one)."""
    return {w[:4] for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _COMMON}


def _similarity(said: str, scripted: str) -> float:
    """Share of the shorter line's content words found in the other (0 to 1)."""
    a, b = _words(said), _words(scripted)
    if not a or not b:  # "Us?" and the like: compare the lines themselves
        norm = lambda t: " ".join(re.findall(r"[a-z0-9]+", t.lower()))
        return float(norm(said) == norm(scripted) != "")
    return len(a & b) / min(len(a), len(b))
