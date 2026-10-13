# MakeSubs

Subtitle sidecar files for on-set dailies, made entirely on your own machine.

Drop in clips, get `.srt` (Premiere Pro, DaVinci Resolve) and/or Avid SubCap files next to
them: a transcript in the spoken language plus translations (English by default, Danish and
other languages optional). **Source clips are only read, never modified**: no remuxing,
no embedded tracks, so timecode, reel names and all other metadata stay exactly as they were.

- Speech recognition: Whisper large-v3 (NVIDIA GPU via faster-whisper, Apple Silicon via MLX)
- Translation: a local LLM through [Ollama](https://ollama.com) (optional, recommended), or
  Whisper's built-in English translation
- Frame-accurate cues using the clip's frame rate (including 29.97/59.94 drop-frame) and
  source timecode
- Lines the recogniser was unsure about are marked `[?]` and listed in a review file

## Install (editors)

You need about 15 GB of free disk space, an internet connection for the first setup, and
either a Windows PC with an NVIDIA graphics card or a Mac with Apple Silicon (M1 or later).
Other machines work too, just much slower.

**Windows**: open PowerShell and paste:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Wangle-Media/make-english-subs/main/install/install-windows.ps1 | iex"
```

**Mac**: open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/Wangle-Media/make-english-subs/main/install/install-mac.sh | bash
```

The installer asks whether to add the **high-quality translator** (Ollama plus an about 8 GB
model). Say yes unless disk space is tight: it gives clearly better English, and it is required
for Danish and other target languages. You can run the installer again at any time to update
or to add the translator later.

When it finishes you have a **MakeSubs** shortcut on the desktop (Windows) or in
`~/Applications` (Mac).

## Using it

1. Drag clips (or whole folders) into the window.
2. **Spoken in clips**: choose the language(s) people actually speak.
   **Setting the language gives better results than auto-detect.** If only one language is
   spoken, choose just that one. Use *Auto-detect* only when you really don't know.
3. **Subtitles in**: English, Danish, or other language codes (`de`, `fr`, `sv`, ...).
   Anything other than English needs the high-quality translator.
4. **Audio**: *Mix all channels* is fine for camera scratch audio. If one channel is clearly
   better (e.g. a boom on channel 1), pick it.
5. **For NLE**: `.srt` for Premiere/Resolve, SubCap for Avid (or both).
6. **Notes**: names of characters, places and special terms help the translator spell them right.
7. Click **Make subtitles**. The first run downloads models and takes longer.

On a recent NVIDIA card or M-series Mac, expect roughly a minute per few minutes of
footage, plus about 30–90 s per clip and language for high-quality translation.

### What you get

For `A001C003.mov`, next to the clip (or in the folder you chose):

| File | Contents |
|---|---|
| `A001C003.transcript.srt` | What was said, in the original language(s) |
| `A001C003.en.srt`, `A001C003.da.srt`, ... | Translations |
| `*.avid.txt` | The same, as Avid SubCap files (if selected) |
| `A001C003.review.txt` | Languages detected, and every line that needs checking, with source timecode |

Existing subtitle files are never replaced unless you tick *Replace existing subtitle files*.

### Lines marked `[?]`

Machine transcription of scratch audio makes mistakes, and the dangerous ones **read
fluently**. When the recogniser's confidence is low (noise, off-mic, overlapping dialogue),
the line is marked `[?]` in every language and listed in `review.txt`. Have an Arabic speaker
check those lines before anything goes to a client or into a cut that depends on them.

### Importing into the NLE

- **Premiere Pro**: *File → Import* the `.srt`, then drag it onto the sequence above the clip.
  SRT times start at the first frame of the clip, so line it up with the clip's start.
- **DaVinci Resolve**: *File → Import → Subtitle...*, then drag the subtitle from the media
  pool onto the timeline, aligned with the clip's start.
- **Avid Media Composer**: add the SubCap effect to a subtitle track, then
  *Effect Editor → Caption Files → Import Caption Data*. SubCap timecodes must match the
  **sequence** timecode; MakeSubs uses the clip's source timecode, so this works directly
  when the clip is edited into a sequence at its own source TC. Otherwise use
  `--avid-start` (command line) to set the sequence start.

> These import steps have been checked against the file formats, not yet in each NLE with
> real project media. Please report anything that doesn't line up.

## Command line

The installer also adds `makesubs`:

```bash
makesubs A001C003.mov B002C011.mxf -t en,da -s ar,en -f srt,avid
makesubs --info A001C003.mov            # timecode, frame rate, audio channels
makesubs clips/*.mov -c 1 -o subs/      # channel 1 only, write into subs/
makesubs --help
```

## Known limitations

- **Dialect and scratch audio**: tested on Levantine Arabic (Palestinian, Syrian, Lebanese)
  samples. Clean speech is transcribed well, including dialect. Heavily noisy or distant audio
  can produce invented text; that is what the `[?]` marks are for. Validate on real camera-test
  footage before principal photography.
- **Language switches inside a sentence**: language is detected per stretch of speech (up to
  about 15 s), so a short phrase in a second language right next to the first may be written
  in the wrong language.
- **Camera-original raw formats** (BRAW, R3D, ARRIRAW) aren't supported; use the transcodes.
  Avid OP-Atom MXF stores audio in separate files: point MakeSubs at the audio MXF.

## Development

```bash
uv sync
uv run pytest
uv run makesubs-gui
```

Code lives in `src/makesubs/`: `probe` (metadata), `audio` (decoding), `asr` (Whisper, voice
activity detection, language detection), `shape` (cue timing and line breaks), `translate`
(Ollama), `writers` (SRT and SubCap), `pipeline`, `cli`, `gui`.
