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


def _script_part(script: ScriptRef, target: str) -> str:
    slang = language_name(script.lang) if script.lang else "the language it is written in"
    lang = language_name(target)
    if script.lang == target:
        use = (f"If a transcript line says what a script line says (\"same\"), write the subtitle "
               f"with the script's own wording, only the part of it spoken in that transcript "
               "line: one script line is often spread over several transcript lines.")
    else:
        use = (f"The script is in {slang}, not {lang}: use it to understand what is meant and "
               "how names and terms are written, then translate into natural " + lang + ".")
    return (
        "The actors are performing a translation of this screenplay, so a scripted line is "
        f"usually spoken in another language than the script's. Script excerpt ({slang}):\n"
        f"<script>\n{script.text()}\n</script>\n"
        "For each transcript line decide how it relates to the script:\n"
        "- \"same\": it says what a script line says (allowing for the translation into the "
        "spoken language, small wording differences and recognition errors).\n"
        "- \"changed\": it is clearly based on a script line, but the actor changed the meaning, "
        "or added or left out something that matters.\n"
        "- \"none\": it is not in the script (improvised, between takes, crew talk).\n"
        f"{use} For \"changed\" and \"none\", translate what was actually said, not the script. "
        "Never add script text that was not spoken.")


def _prompt(lines: dict[str, str], target: str, context: list[str], draft: str | None,
            note: str, source_langs: list[str], script: ScriptRef | None = None) -> str:
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
    if draft and not script:  # with a script, the script shows how names are spelled
        parts.append("Reference only: a rough English machine translation of the same audio. "
                     "It may drop or invent sentences. Use it ONLY to check how names and places "
                     f"are spelled; do not copy its wording.\n<reference>\n{draft}\n</reference>")
    if script:
        parts.append(_script_part(script, target))
    if context:
        parts.append(f"Previous subtitles, already in {lang} (context only):\n" + "\n".join(context))
    rules = (f"Translate every numbered line into natural, concise {lang} suitable for subtitles. "
             f"Every output line must be written in {lang}. Keep names, places and numbers. Keep "
             "one output line per input line: do not merge, split, reorder or skip lines. If a "
             "line is unintelligible, translate what you can.\n")
    if script:
        rules += ("Return ONLY a JSON object with exactly the same keys, mapping each line number "
                  'to {"text": translation, "script": the matching script line such as "S4" or '
                  'null, "match": "same", "changed" or "none"}.')
    else:
        rules += ("Return ONLY a JSON object mapping each line number to its translation, with "
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
        if ref in ("NULL", "NONE") or not re.fullmatch(r"S\d+", ref):
            ref = ""
        match = str(value.get("match") or "").strip().lower()
        match = match if match in _MATCHES else ("none" if not ref else "")
        if match == "none":
            ref = ""
        return Answer(text, ref, match)
    return Answer(str(value).strip() if value is not None else "")


def _generate(model: str, prompt: str, num_ctx: int = 8192) -> dict[str, Answer]:
    payload = {"model": model, "prompt": prompt, "stream": False, "format": "json",
               "keep_alive": "5m", "options": {"temperature": 0.2, "num_ctx": num_ctx}}
    with _call("/api/generate", payload) as r:
        body = json.load(r)
    try:
        out = json.loads(body.get("response", ""))
    except json.JSONDecodeError:
        return {}
    return {str(k): parse_answer(v) for k, v in out.items()} if isinstance(out, dict) else {}


def translate_cues(cues: list[Cue], target: str, model: str = DEFAULT_MODEL,
                   draft: str | None = None, note: str = "",
                   progress: Callable[[str], None] = print,
                   script: ScriptRef | None = None) -> list[Cue]:
    """Return new cues with translated text and the same timing as the input.

    With a script, each cue also records which script line it matched and how
    (script_ref, script_match).
    """
    out = [Cue(c.start, c.end, c.text, target, c.confidence, c.flagged) for c in cues]
    todo = [i for i, c in enumerate(cues) if c.lang != target]
    done_context: list[str] = []
    num_ctx = 12288 if script else 8192  # room for the script excerpt
    for b in range(0, len(todo), BATCH):
        idx = todo[b:b + BATCH]
        progress(f"  translating to {language_name(target)}: lines {b + 1}-{b + len(idx)} of {len(todo)}")
        lines = {str(n + 1): cues[i].text for n, i in enumerate(idx)}
        langs = list(dict.fromkeys(cues[i].lang for i in idx))
        prompt = _prompt(lines, target, done_context[-CONTEXT:], draft, note, langs, script)
        result = _generate(model, prompt, num_ctx)
        bad = [k for k in lines if k not in result or not result[k].text
               or (target != "en" and cues[idx[int(k) - 1]].lang != "en"
                   and _looks_english(result[k].text))]
        if bad:  # retry once, only the empty / wrong-language lines, without the English draft
            retry = _generate(model, _prompt({k: lines[k] for k in bad}, target,
                                             done_context[-CONTEXT:], None, note, langs, script),
                              num_ctx)
            result.update({k: v for k, v in retry.items() if v.text})
        for n, i in enumerate(idx):
            ans = result.get(str(n + 1))
            if ans and ans.text:
                out[i].text = ans.text
                if script:
                    out[i].script_ref, out[i].script_match = ans.script_ref, ans.match or "none"
            else:
                out[i].flagged = True  # leave source text, mark for review
            done_context.append(out[i].text)
    return out
