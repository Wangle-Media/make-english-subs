"""Command-line interface: makesubs CLIP [CLIP ...]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, translate
from .audio import channel_choices
from .pipeline import Options, run
from .probe import probe


def _codes(value: str) -> list[str]:
    return [c.strip().lower() for c in value.split(",") if c.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="makesubs",
        description="Make subtitle sidecar files for dailies. Source clips are never modified.")
    p.add_argument("clips", nargs="*", type=Path, help="video/audio files")
    p.add_argument("-t", "--to", default="en", type=_codes, metavar="LANGS",
                   help="subtitle languages to produce, e.g. en or en,da (default: en)")
    p.add_argument("-s", "--spoken", default="ar,en", type=_codes, metavar="LANGS",
                   help="languages spoken in the clips, e.g. ar,en, or 'auto' for any (default: ar,en). "
                        "Naming the actual languages gives better results than 'auto'; a single "
                        "language is the most robust if nobody switches language")
    p.add_argument("--translator", choices=["auto", "llm", "whisper"], default="auto",
                   help="llm = local Ollama model (best, any language); whisper = built-in, "
                        "English only; auto = llm if Ollama is running (default)")
    p.add_argument("--llm-model", default=translate.DEFAULT_MODEL, help="Ollama model name")
    p.add_argument("--note", default="", help="names/places/terms to help translation")
    p.add_argument("-c", "--channel", default="mix",
                   help="audio channel to use: 'mix' or a channel number (see --info)")
    p.add_argument("-f", "--format", default="srt", type=_codes,
                   help="srt (Premiere/Resolve), avid (SubCap), or srt,avid (default: srt)")
    p.add_argument("--avid-start", metavar="TC",
                   help="sequence start timecode for SubCap (default: clip source TC)")
    p.add_argument("-o", "--out", type=Path, help="output folder (default: next to each clip)")
    p.add_argument("--overwrite", action="store_true", help="replace existing subtitle files")
    p.add_argument("--threshold", type=float, default=0.6,
                   help="confidence below which a line is marked [?] (default: 0.6)")
    p.add_argument("--no-marks", action="store_true", help="don't put [?] in subtitle text")
    p.add_argument("--backend", choices=["auto", "faster-whisper", "mlx"], default="auto")
    p.add_argument("--whisper-model", help="override speech model (default large-v3)")
    p.add_argument("--info", action="store_true", help="only show clip metadata and channels")
    p.add_argument("--setup", action="store_true",
                   help="download the speech model (and translation model, if Ollama runs), then exit")
    p.add_argument("--version", action="version", version=f"makesubs {__version__}")
    return p


def setup(args) -> int:
    from .asr import create_engine
    print("Downloading/checking speech recognition model (about 3 GB the first time)...")
    engine = create_engine(args.backend, args.whisper_model)
    print(f"OK: {engine.name}")
    engine.unload()
    if translate.available():
        translate.ensure_model(args.llm_model)
        print(f"OK: translation model {args.llm_model}")
    else:
        print("Ollama is not running: only English (basic) translation is available.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.setup:
        return setup(args)
    if not args.clips:
        parser.error("give at least one clip")
    missing = [c for c in args.clips if not c.is_file()]
    if missing:
        print(f"File not found: {', '.join(map(str, missing))}", file=sys.stderr)
        return 2

    if args.info:
        for clip in args.clips:
            try:
                info = probe(clip)
            except RuntimeError as e:
                print(f"{clip.name}: {e}")
                continue
            print(info.summary())
            print(f"  reel: {info.reel or '-'}; channels: {', '.join(channel_choices(info))}")
        return 0

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    opts = Options(
        targets=args.to, spoken=args.spoken, translator=args.translator,
        llm_model=args.llm_model, note=args.note, channel=args.channel, formats=args.format,
        avid_start=args.avid_start, out_dir=args.out, overwrite=args.overwrite,
        flag_threshold=args.threshold, mark_flagged=not args.no_marks,
        backend=args.backend, whisper_model=args.whisper_model)
    try:
        jobs = run(args.clips, opts)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    failed = [j for j in jobs if j.error]
    print()
    for j in jobs:
        status = f"FAILED: {j.error}" if j.error else "ok"
        print(f"{j.info.path.name}: {status}")
        for p in j.outputs:
            print(f"   {p}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
