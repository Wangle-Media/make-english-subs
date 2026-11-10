"""End-to-end processing of a batch of clips. Source media is only ever read."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import asr, languages, translate
from . import script as screenplay
from .audio import load_audio
from .probe import MediaInfo, probe
from .shape import Cue, build_cues, finalize
from .timecode import frames_to_tc, seconds_to_frames, tc_to_frames
from .writers import write_srt, write_subcap

Progress = Callable[[str], None]


@dataclass
class Options:
    targets: list[str] = field(default_factory=lambda: ["en"])
    spoken: list[str] = field(default_factory=lambda: ["ar", "en"])
    translator: str = "auto"          # "auto" | "llm" | "whisper"
    llm_model: str = translate.DEFAULT_MODEL
    note: str = ""                    # names/places to help the translator
    channel: str = "mix"              # "mix" or 1-based channel number
    formats: list[str] = field(default_factory=lambda: ["srt"])  # "srt", "avid"
    avid_start: str | None = None     # override sequence start TC for SubCap
    out_dir: Path | None = None       # default: next to each clip
    overwrite: bool = False
    flag_threshold: float = 0.6
    mark_flagged: bool = True
    backend: str = "auto"
    whisper_model: str | None = None
    dialect: str | None = None        # dialect speech model for one language, see asr.DIALECTS
    script: Path | None = None        # screenplay (.fdx/.pdf) for the translator to follow
    scenes: list[str] = field(default_factory=list)  # script scene numbers; [] = find per clip


MAX_EXCERPT = 9000   # characters of script given to the translator per clip


@dataclass
class Job:
    info: MediaInfo
    transcript: asr.Transcript | None = None
    speech_cues: list[Cue] = field(default_factory=list)  # timed to the speech itself
    source_cues: list[Cue] = field(default_factory=list)  # on-screen timing (finalized)
    outputs: list[Path] = field(default_factory=list)
    error: str | None = None
    script_match: screenplay.Match | None = None   # scene(s) used for this clip
    script_guess: screenplay.Match | None = None   # best scene, even if not good enough
    script_cues: list[Cue] = field(default_factory=list)  # translated cues with match info
    script_lines: list[tuple[str, screenplay.Line]] = field(default_factory=list)


def resolve_translator(opts: Options) -> str:
    if opts.translator != "auto":
        return opts.translator
    return "llm" if translate.available() else "whisper"


def validate(opts: Options) -> str:
    """Return the translator to use, or raise with an editor-friendly message."""
    tr = resolve_translator(opts)
    if tr == "whisper" and any(t != "en" for t in opts.targets):
        others = ", ".join(t for t in opts.targets if t != "en")
        raise RuntimeError(
            f"Translating to {others} needs the local translation model (Ollama), which is not "
            "running. Install/start Ollama (see README) or choose English only.")
    if tr == "llm" and not translate.available():
        raise RuntimeError("Ollama is not running. Start the Ollama app, or use --translator whisper.")
    for f in opts.formats:
        if f not in ("srt", "avid"):
            raise RuntimeError(f"unknown format {f!r}")
    if opts.script and tr != "llm":
        raise RuntimeError("Following a script needs the local translation model (Ollama), which "
                           "is not running. Start Ollama, or leave the script out.")
    if opts.scenes and not opts.script:
        raise RuntimeError("Scene numbers only work together with a script.")
    if opts.dialect:
        if opts.dialect not in asr.DIALECTS:
            raise RuntimeError(f"Unknown dialect model {opts.dialect!r}; choose from "
                               + ", ".join(asr.DIALECTS))
        lang = asr.DIALECTS[opts.dialect][0]
        if lang not in opts.spoken and opts.spoken != ["auto"]:
            raise RuntimeError(f"The {opts.dialect} model is for {languages.name(lang)}, which "
                               "isn't among the spoken languages.")
    return tr


def load_script(opts: Options) -> screenplay.Script | None:
    if not opts.script:
        return None
    if not Path(opts.script).is_file():
        raise RuntimeError(f"Script not found: {opts.script}")
    script = screenplay.load(Path(opts.script))
    missing = [n for n in opts.scenes if script.find(n) is None]
    if missing:
        raise RuntimeError(f"{script.path.name} has no scene {', '.join(missing)}. "
                           "Check the numbers with: makesubs --script-info SCRIPT")
    return script


def _script_ref(match: screenplay.Match, lang: str) -> tuple[translate.ScriptRef, list]:
    """The excerpt for the translator: the best scene, plus neighbours while it fits."""
    scenes = list(match.scenes)
    while len(scenes) > 1 and sum(len(ln.text) for s in scenes for ln in s.lines) > MAX_EXCERPT:
        scenes.pop()
    match.scenes = scenes
    numbered = screenplay.excerpt(scenes)
    size, kept = 0, []
    for ref, ln in numbered:  # a single huge scene: keep its start
        size += len(ln.text) + len(ln.character) + 6
        if size > MAX_EXCERPT:
            break
        kept.append((ref, ln))
    ref = translate.ScriptRef([(r, ln.character, ln.text) for r, ln in kept], lang)
    return ref, kept


def output_path(info: MediaInfo, opts: Options, tag: str, fmt: str) -> Path:
    folder = opts.out_dir or info.path.parent
    suffix = ".srt" if fmt == "srt" else ".avid.txt"
    path = folder / f"{info.path.stem}.{tag}{suffix}"
    if path.resolve() == info.path.resolve():  # paranoia: never overwrite media
        raise RuntimeError(f"refusing to write over source file {info.path}")
    return path


def planned_outputs(info: MediaInfo, opts: Options) -> list[Path]:
    tags = ["transcript"] + opts.targets
    return [output_path(info, opts, t, f) for t in tags for f in opts.formats]


def _is_english(text: str) -> bool:
    """Mostly Latin letters: the draft is English, not left in the spoken language."""
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) >= 0.8 * len(letters)


def _flag_overlapping(cues: list[Cue], reference: list[Cue], threshold: float) -> None:
    bad = [r for r in reference if r.flagged]
    for c in cues:
        c.flagged = c.confidence < threshold or any(
            r.start < c.end and c.start < r.end for r in bad)


def _write(job: Job, opts: Options, tag: str, cues: list[Cue]) -> None:
    info = job.info
    for fmt in opts.formats:
        path = output_path(info, opts, tag, fmt)
        if fmt == "srt":
            write_srt(path, cues, opts.mark_flagged)
        else:
            if not info.fps:
                raise RuntimeError(f"{info.path.name}: no frame rate, cannot write Avid SubCap")
            start = opts.avid_start or info.start_tc or "00:00:00:00"
            write_subcap(path, cues, info.fps, start, opts.mark_flagged)
        job.outputs.append(path)


def _tc(info: MediaInfo, seconds: float) -> str:
    if info.fps and info.start_tc:
        base = tc_to_frames(info.start_tc, info.fps)
        return frames_to_tc(base + seconds_to_frames(seconds, info.fps), info.fps)
    m, s = divmod(seconds, 60)
    return f"{int(m):02d}:{s:05.2f}"


def _write_review(job: Job, opts: Options, translator: str) -> None:
    info, cues = job.info, job.source_cues
    flagged = [c for c in cues if c.flagged]
    path = (opts.out_dir or info.path.parent) / f"{info.path.stem}.review.txt"
    lines = [
        f"Clip:        {info.path.name}",
        f"Source TC:   {info.start_tc or 'none'}   fps: {info.fps or 'unknown'}",
        f"Languages:   {', '.join(job.transcript.languages) or 'no speech'}",
        f"Translation: {'local LLM (' + opts.llm_model + ')' if translator == 'llm' else 'Whisper'}",
        f"Subtitles:   {len(cues)}, of which {len(flagged)} need review ([?] in the files)",
        "",
        "Machine transcription/translation. Lines marked [?] had low recognition confidence",
        "(noise, overlapping speech, off-mic) and may be wrong even if they read fluently.",
    ]
    if opts.spoken == ["auto"]:
        lines.append("Tip: spoken language was auto-detected. Setting the actual language(s) "
                     "usually gives better results.")
    lines.append("")
    for c in flagged:
        lines.append(f"{_tc(info, c.start)}  ({c.confidence:.0%})  {c.text}")
    if opts.script:
        lines += ([""] if lines[-1] else []) + _script_review(job, opts)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    job.outputs.append(path)


def _script_review(job: Job, opts: Options) -> list[str]:
    name = Path(opts.script).name
    m, guess = job.script_match, job.script_guess
    if not m:
        why = (f"best guess {guess.label}, only {guess.coverage:.0%} of key words in common"
               if guess and guess.words else "too little speech to compare")
        return [f"Script:      {name}: no scene matched with confidence ({why}).",
                "             Translated without the script. To use it, give the scene number "
                "(--scene, or Scene in the window)."]
    heading = "; ".join(f"{s.label} {s.heading}".strip() for s in m.scenes)
    how = "set by hand" if m.by_hand else (
        f"found automatically, {m.coverage:.0%} of key words in common: {', '.join(m.words[:8])}")
    cues = [c for c in job.script_cues if c.script_match]  # lines already in the target are not compared
    counts = {k: sum(c.script_match == k for c in cues) for k in ("same", "changed", "none")}
    out = [f"Script:      {name}, {heading}",
           f"             ({how})",
           f"             {len(cues)} lines compared: {counts['same']} as scripted, "
           f"{counts['changed']} changed, {counts['none']} not in the script"]
    differ = [c for c in cues if c.script_match in ("changed", "none")]
    if differ:
        lang = translate.language_name(cues[0].lang)
        out += ["", f"Lines that differ from the script ({lang} subtitle, then the script line):"]
        lookup = {ref: ln for ref, ln in job.script_lines}
        for c in differ:
            tc = _tc(job.info, c.start)
            out.append(f"{tc}  {c.script_match:<8} {c.text}")
            ln = lookup.get(c.script_ref)
            if ln:
                out.append(f"{'':{len(tc) + 2}}script:  {ln.character}: {ln.text}")
    return out


def run(files: list[Path], opts: Options, progress: Progress = print) -> list[Job]:
    translator = validate(opts)
    script = load_script(opts)
    if script:
        progress(script.summary())
    jobs: list[Job] = []
    for f in files:
        try:
            info = probe(Path(f))
        except Exception as e:  # noqa: BLE001 - report per file, keep going
            progress(f"SKIP {Path(f).name}: {e}")
            continue
        if not info.audio:
            progress(f"SKIP {info.path.name}: no audio")
            continue
        existing = [p for p in planned_outputs(info, opts) if p.exists()]
        if existing and not opts.overwrite:
            progress(f"SKIP {info.path.name}: subtitles already exist "
                     f"({existing[0].name}); use overwrite to replace them")
            continue
        progress(info.summary())
        jobs.append(Job(info))
    if not jobs:
        return jobs

    # Phase 1: speech recognition for every clip (Whisper in memory).
    want_draft = "en" in opts.targets or translator == "llm"
    dialect_lang, dialect_model, dialect_name = asr.DIALECTS[opts.dialect] if opts.dialect         else (None, None, None)
    later = frozenset({dialect_lang}) if dialect_lang else frozenset()
    progress("Loading speech recognition model (first run downloads ~3 GB)...")
    engine = asr.create_engine(opts.backend, opts.whisper_model)
    progress(f"Using {engine.name}")
    try:
        for job in jobs:
            t0 = time.time()
            progress(f"Transcribing {job.info.path.name}")
            try:
                audio = load_audio(job.info, opts.channel)
                job.transcript = asr.transcribe(engine, audio, opts.spoken, want_draft, progress,
                                                later)
                progress(f"  done in {time.time() - t0:.0f}s")
            except Exception as e:  # noqa: BLE001
                job.error = str(e)
                progress(f"ERROR {job.info.path.name}: {e}")
    finally:
        engine.unload()

    # Phase 1b: the dialect model transcribes the stretches in its language (one model in
    # memory at a time).
    pending = [j for j in jobs if j.transcript and not j.error
               and any(c[2] == dialect_lang for c in j.transcript.chunks)]
    if pending:
        progress(f"Loading the {dialect_name} speech model (first run downloads ~6 GB)...")
        engine = asr.create_engine("faster-whisper", dialect_model)
        progress(f"Using {engine.name}")
        try:
            for job in pending:
                t0 = time.time()
                progress(f"Transcribing the {languages.name(dialect_lang)} in {job.info.path.name}")
                try:
                    audio = load_audio(job.info, opts.channel)
                    asr.retranscribe(engine, audio, job.transcript, {dialect_lang}, progress)
                    progress(f"  done in {time.time() - t0:.0f}s")
                except Exception as e:  # noqa: BLE001
                    job.error = str(e)
                    progress(f"ERROR {job.info.path.name}: {e}")
        finally:
            engine.unload()

    for job in jobs:
        if job.error or job.transcript is None:
            continue
        cues = build_cues(job.transcript.source)
        for c in cues:
            c.flagged = c.confidence < opts.flag_threshold
        job.speech_cues = cues
        job.source_cues = finalize(cues, job.info.fps)
        _write(job, opts, "transcript", job.source_cues)

    # Phase 2: translation (LLM in memory, Whisper unloaded).
    if translator == "llm":
        translate.ensure_model(opts.llm_model, progress)
    note = opts.note
    if script:
        names = ", ".join(n.title() for n in script.characters[:40])
        note = "; ".join(filter(None, [note, f"Characters in the script: {names}"]))
    finder = screenplay.SceneFinder(script) if script and not opts.scenes else None
    for job in jobs:
        if job.error or job.transcript is None:
            continue
        try:
            draft_segs = job.transcript.english or []
            # Some Whisper models (dialect fine-tunes) can no longer translate and hand back
            # the source language: that is no help as an English reference.
            english_segs = [s for s in draft_segs if _is_english(s.text)]
            translated: dict[str, list] = {}
            if translator == "llm":
                # Translate speech-timed cues and finalize afterwards, so the reading time is
                # worked out from the translated text, which usually runs longer.
                for target in opts.targets:
                    translated[target] = translate.translate_cues(
                        job.speech_cues, target, opts.llm_model, english_segs or None, note,
                        progress)
            if script:
                if opts.scenes:
                    job.script_match = screenplay.Match(
                        [script.find(n) for n in opts.scenes], 1.0, [], by_hand=True)
                else:
                    # Find the scene from the translation (better English than Whisper's draft,
                    # which a dialect model may not give at all), else from the draft.
                    text = " ".join(c.text for c in translated.get("en", [])) or \
                        " ".join(s.text for s in english_segs)
                    job.script_match, job.script_guess = finder.locate(text)
                if job.script_match:
                    ref, job.script_lines = _script_ref(job.script_match, script.lang)
                    progress(f"  following the script: {job.script_match.label}")
                    report = script.lang if script.lang in opts.targets else opts.targets[0]
                    translate.compare_script(translated[report], ref, report, opts.llm_model,
                                             progress)
                    job.script_cues = translated[report]
                else:
                    progress("  no script scene matched with confidence; translated without it")
            for target in opts.targets:
                if translator == "llm":
                    cues = finalize(translated[target], job.info.fps)
                else:  # Whisper's own English translation, with its own timing
                    cues = build_cues(draft_segs)
                    _flag_overlapping(cues, job.source_cues, opts.flag_threshold)
                    cues = finalize(cues, job.info.fps)
                _write(job, opts, target, cues)
            _write_review(job, opts, translator)
            progress(f"Finished {job.info.path.name}")
        except Exception as e:  # noqa: BLE001
            job.error = str(e)
            progress(f"ERROR {job.info.path.name}: {e}")
    if translator == "llm":
        translate.unload(opts.llm_model)
    return jobs
