"""End-to-end processing of a batch of clips. Source media is only ever read."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import asr, translate
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


@dataclass
class Job:
    info: MediaInfo
    transcript: asr.Transcript | None = None
    source_cues: list[Cue] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    error: str | None = None


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
    return tr


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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    job.outputs.append(path)


def run(files: list[Path], opts: Options, progress: Progress = print) -> list[Job]:
    translator = validate(opts)
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
    progress("Loading speech recognition model (first run downloads ~3 GB)...")
    engine = asr.create_engine(opts.backend, opts.whisper_model)
    progress(f"Using {engine.name}")
    try:
        for job in jobs:
            t0 = time.time()
            progress(f"Transcribing {job.info.path.name}")
            try:
                audio = load_audio(job.info, opts.channel)
                job.transcript = asr.transcribe(engine, audio, opts.spoken, want_draft, progress)
                cues = build_cues(job.transcript.source)
                for c in cues:
                    c.flagged = c.confidence < opts.flag_threshold
                job.source_cues = finalize(cues, job.info.fps)
                _write(job, opts, "transcript", job.source_cues)
                progress(f"  done in {time.time() - t0:.0f}s")
            except Exception as e:  # noqa: BLE001
                job.error = str(e)
                progress(f"ERROR {job.info.path.name}: {e}")
    finally:
        engine.unload()

    # Phase 2: translation (LLM in memory, Whisper unloaded).
    if translator == "llm":
        translate.ensure_model(opts.llm_model, progress)
    for job in jobs:
        if job.error or job.transcript is None:
            continue
        try:
            draft_segs = job.transcript.english or []
            for target in opts.targets:
                if translator == "llm":
                    draft = " ".join(s.text for s in draft_segs) or None
                    cues = translate.translate_cues(job.source_cues, target, opts.llm_model,
                                                    draft, opts.note, progress)
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
