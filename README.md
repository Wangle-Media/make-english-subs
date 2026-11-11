# MakeSubs

Subtitle sidecar files for on-set dailies, made entirely on your own machine.

Drop in clips, get `.srt` (Premiere Pro, DaVinci Resolve) and/or Avid SubCap files next to
them: a transcript in the spoken language plus translations (English by default, Danish and
other languages optional). **Source clips are only read, never modified**: no remuxing,
no embedded tracks, so timecode, reel names and all other metadata stay exactly as they were.

- Speech recognition: Whisper large-v3 (NVIDIA GPU via faster-whisper, Apple Silicon via MLX),
  with an optional dialect model for Levantine Arabic (Palestinian, Lebanese, Syrian, Jordanian)
- Translation: a local LLM through [Ollama](https://ollama.com) (optional, recommended), or
  Whisper's built-in English translation
- Optionally follows the screenplay (PDF, Final Draft, Fountain or plain text): where an actor
  says a scripted line, the subtitle uses the script's wording; changed and improvised lines
  are translated as spoken and listed for review
- Frame-accurate cues using the clip's frame rate (including 29.97/59.94 drop-frame) and
  source timecode
- Lines the recogniser was unsure about are marked `[?]` and listed in a review file

## Install (editors)

You need about 20 GB of free disk space (26 GB with the Arabic dialect model), an internet
connection for the first setup, and either a Windows PC with an NVIDIA graphics card or a Mac
with Apple Silicon (M1 or later) and at least 16 GB of memory. Other machines work too, just
much slower.

**Windows**: open PowerShell and paste:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Wangle-Media/make-english-subs/main/install/install-windows.ps1 | iex"
```

**Mac**: open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/Wangle-Media/make-english-subs/main/install/install-mac.sh | bash
```

The installer asks two questions:

1. Whether to add the **high-quality translator** (Ollama plus about 9 GB of models). Say yes
   unless disk space is tight: it gives clearly better English, and it is required for Danish
   and other target languages and for following a script.
2. Whether to download the **Levantine Arabic dialect model** (about 6 GB). Say yes if the
   clips are in Palestinian, Lebanese, Syrian or Jordanian Arabic: it hears the dialect much
   better (see [Arabic speech](#2-settings)). The window then uses it for Arabic by default.
   On a Mac it runs on the processor instead of the graphics chip, which adds a few minutes
   per clip.

You can run the installer again at any time to update, or to add what you skipped.

The installer also runs a short test of the speech recognition. If it says
**WARNING: no usable NVIDIA GPU** on a PC that has an NVIDIA card, update the graphics driver
from [nvidia.com](https://www.nvidia.com/Download/index.aspx) and run the installer again;
otherwise MakeSubs runs on the CPU, which is very slow. If the installer stops with an error,
copy the text in the window and send it to whoever set up MakeSubs for you.

When it finishes you have a **MakeSubs** shortcut on the desktop (Windows) or in
`~/Applications` (Mac).

### Upgrading

Running the installer again replaces the installed MakeSubs with the newest version. Your
settings, the downloaded models and Ollama are kept. **In the middle of a production, try a new
version first, next to the one you use, and replace it at a natural break.**

**Which version you have**: the window's title bar, or `makesubs --version`.

**Try a new version without installing it** (Mac Terminal or Windows PowerShell):

```bash
uvx --python 3.12 --from "makesubs @ https://github.com/Wangle-Media/make-english-subs/archive/refs/heads/main.zip" makesubs-gui
```

This opens the new version's window from a temporary environment. Your installed MakeSubs,
its shortcut, the models and Ollama aren't touched; close the window and nothing has changed.
The first start downloads a few hundred MB of components. While you try it:

- Set **Save to** to a test folder, so its files don't mix with, or replace, the ones you've
  delivered. Both versions share the same settings, so set **Save to** back afterwards,
  before you use your installed MakeSubs again.
- New features can download models the first time you use them (following a script: 0.7 GB;
  the Arabic dialect model: about 6 GB).
- A new version can time and word subtitles differently from earlier days' files.

**Upgrade**: when the new version works for you, run the installer again (the same command as
for installing). Re-running the installer later updates MakeSubs again.

**Go back to an earlier version**: every version is kept as a branch called `release-` and
the version number (`release-0.2.0`, `release-0.3.0`, ...). Run the installer with that branch:

```bash
# Mac
curl -fsSL https://raw.githubusercontent.com/Wangle-Media/make-english-subs/main/install/install-mac.sh | MAKESUBS_REF=release-0.2.0 bash
```

```powershell
# Windows (PowerShell)
$env:MAKESUBS_REF = "release-0.2.0"; powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Wangle-Media/make-english-subs/main/install/install-windows.ps1 | iex"; Remove-Item Env:MAKESUBS_REF
```

### Uninstall

**Windows** (PowerShell) / **Mac** (Terminal):

```
uv tool uninstall makesubs
```

Then delete the shortcut, and to free the disk space used by the speech models, the folder
`.cache\huggingface` in your user folder (Windows; also `C:\ProgramData\MakeSubs` if it
exists) or `~/.cache/huggingface` (Mac). If you installed the translator: remove its models
with `ollama rm gemma3:12b mxbai-embed-large`, and remove **Ollama** like any other app if you
don't need it for anything else.

## Using it

Open **MakeSubs** from the desktop shortcut (Windows) or `~/Applications` (Mac). The window
has three numbered steps, from top to bottom.

![The MakeSubs window after a finished run](docs/window.png)

### 1. Add clips

Drag clips, or whole folders of clips, onto the list at the top. You can also click
**Add files…**. The clips are only read, never changed. **Clear list** empties the list
(it doesn't touch any files).

### 2. Settings

**Spoken in clips**: click the button and tick the language(s) people actually speak.
Type in the search box to find a language quickly; clicking its name ticks it.

![Choosing languages](docs/language-picker.png)

**Setting the language gives better results than auto-detect.** If only one language is
spoken, tick just that one. Tick *Don't know — auto-detect* only when you really don't know.

**Arabic speech** (shown when Arabic is ticked): *Standard model*, or the *Levantine Arabic
dialect model* for Palestinian, Lebanese, Syrian and Jordanian Arabic. The dialect model
([WhisperLevantine](https://huggingface.co/HebArabNlpProject/WhisperLevantine), Whisper large-v3
fine-tuned on about 1,200 hours of Levantine Arabic) hears the dialect much better: on
Palestinian Arabic dailies it got noticeably more lines right, and marked about half as many
lines `[?]`. It can't write English or translate, so the standard model still detects the languages,
writes the English, and makes the rough English draft; the dialect model then transcribes
only the Arabic, in a second pass. The first run with it downloads about 6 GB, unless the
installer already did; then it is chosen by default. On a Mac it runs on the processor (the
standard model uses the graphics chip), which adds a few minutes per clip.

**Subtitles in**: tick English, Danish or any other language, the same way.

**Translation** shows which translator will be used:

- Green *Local LLM ready*: the high-quality translator is running. Any subtitle language works.
- Orange *Basic: English only*: only English subtitles, in lower quality. If you installed the
  high-quality translator, start the **Ollama** app and click **Check again**.

**Audio**: *Mix all channels* is fine for camera scratch audio. If one channel is clearly
better (e.g. a boom on channel 1), pick it.

**For NLE**: *Premiere / Resolve (.srt)*, *Avid SubCap*, or both.

**Notes**: names of characters, places and special terms, e.g.
`Characters: Nidal, Yasmin. Places: Jenin, Nablus.` This helps the translator spell them right.

**Script** (optional, needs the high-quality translator): click **Choose…** and pick the
screenplay (a PDF, a Final Draft `.fdx`, a Fountain `.fountain`, or a plain-text export), or
drop it on the clip list. Where an actor
says a scripted line, the subtitle then uses the script's wording; lines the actor changed or
improvised are translated as spoken, and listed in `review.txt` next to the script line. See
[Following the script](#following-the-script). **None** switches it off.

Right after you choose it, the window shows what was read: the number of scenes and lines of
dialogue, and the characters. If the names look wrong (merged words, names that aren't
characters), the PDF was misread; tell us, and try the `.fdx` if there is one. The first run
with a script downloads a small extra model (0.7 GB) that matches the subtitles to the script.

**Scene**: leave it on *Find the scene for each clip automatically* and MakeSubs finds the
scene itself. If all the clips you're adding are from one scene (or a few), click it and tick
the scene(s): the list shows every scene with its heading and characters, can be searched
(number, place or character), and shows a scene's first lines when you hover over it.

![Picking the scene](docs/scene-picker.png)

**Save to**: *Next to each clip* by default. Click **Choose…** to put all subtitle files in one
folder instead, and **Next to clips** to go back.

**Replace existing subtitle files**: leave this off to skip clips that already have subtitles,
so a second run doesn't overwrite files you've already checked. Tick it to make them again.

MakeSubs remembers these settings for next time.

### 3. Make subtitles

Click **Make subtitles**. The box underneath shows what's happening. The first run downloads
the models (several GB) and takes longer; after that, expect roughly a minute per few minutes
of footage on a recent NVIDIA card or M-series Mac, plus about 30–90 s per clip and language
for high-quality translation, and a little more with a script or the dialect model. Wait for
it to finish before closing the window.

When it's done, the box ends with a summary per clip:

```
OK      A001C003.mov: 4 subtitle files in D:\Dailies\Day01
        1 line marked [?] to check, see A001C003.review.txt
        script scene 2: 7 as scripted, 3 changed, 4 not in the script; see A001C003.review.txt
OK      B002C011.mxf: 4 subtitle files in D:\Dailies\Day01

All done. Click 'Show subtitle files' to open the folder.
```

Click **Show subtitle files** to open that folder. Then import the files into your NLE (see
[Importing into the NLE](#importing-into-the-nle)), and have the lines marked `[?]` checked.

### If something goes wrong

| Message in the box | What to do |
|---|---|
| `SKIP …: subtitles already exist` | That clip was done before. Tick *Replace existing subtitle files* to make them again. |
| `SKIP …: no audio` | The file has no sound (or isn't a video). Use a version of the clip with audio. |
| `… needs the high-quality translator (Ollama)` (orange, under *Subtitles in*) | Start the Ollama app and click **Check again**, or untick the non-English languages. |
| `FAILED …` | Something went wrong with that clip; the reason is on the same line. The other clips still finish. |
| *Can't use this script: …* (red, under *Script*) | The file isn't there any more, or isn't a screenplay MakeSubs can read (a scanned PDF, for example). Try the `.fdx` or another export. |
| `no script scene found with confidence` | The clip is short, mostly improvised, or the audio is poor. Pick the scene under **Scene** to use the script anyway. |
| A box saying *MakeSubs: unexpected error* | Send the file it names (`makesubs.log`) to whoever set up MakeSubs for you. |

The log file is in `%LOCALAPPDATA%\Wangle Media\MakeSubs` (Windows) or
`~/Library/Application Support/Wangle Media/MakeSubs` (Mac).

### What you get

For `A001C003.mov`, next to the clip (or in the folder you chose):

| File | Contents |
|---|---|
| `A001C003.transcript.srt` | What was said, in the original language(s) |
| `A001C003.en.srt`, `A001C003.da.srt`, ... | Translations |
| `*.avid.txt` | The same, as Avid SubCap files (if selected) |
| `A001C003.review.txt` | Languages detected, and every line that needs checking, with source timecode; with a script, also the lines that differ from it |

Existing subtitle files are never replaced unless you tick *Replace existing subtitle files*.

### Following the script

The script is read for its dialogue, scene by scene. For each clip, in three steps:

1. Every line is translated from what was said, without the script, so the script can't pull
   the translation towards what the actor was supposed to say. MakeSubs then compares the
   English translation with the script and picks the scene with the most key words in common
   (names, places, unusual words).
2. Each subtitle is paired with the script line it most likely is, by meaning (a small
   sentence-embedding model, `mxbai-embed-large`, through Ollama) and shared words, keeping
   the script's order. Actors reword a lot, so meaning finds far more than words alone:
   "We're leaving at first light" is "We sail at dawn". Subtitles that match nothing,
   like crew talk, stay unpaired.
3. The translator checks every pair and decides whether the actor said the line **as
   scripted**, **changed** it, or said something **not in the script**.

- As scripted: the subtitle uses the script's wording, only the part of the script line said in
  that subtitle. This only happens when the translation and the script line share most of
  their words, and never when a number, time, day or month differs.
- Changed or not in the script: translated as actually said. The subtitle shows what is in
  the take, never script text that wasn't spoken.

On two test takes in Palestinian Arabic that the actors reworded a lot, this found 26 of
39 and 29 of 37 scripted lines, without a single wrong pairing; most of the rest were lines
the speech recognition misheard.

When the script is in another language than the subtitles, it is used for deciding only; the
subtitles stay as translated. Without the embedding model (Ollama couldn't download it, say),
the pairing falls back to shared words, which finds fewer lines.

`review.txt` ends with what happened:

```
Script:      film.fdx, scene 2 EXT. CHECKPOINT - DAY
             (found automatically, 73% of key words in common: cousin, papers, wedding, karim, nablus)
             3 lines compared: 1 as scripted, 1 changed, 1 not in the script

Lines that differ from the script (English subtitle, then the script line):
10:00:04:00  changed  We're going to my cousin Karim's wedding in Nablus.
             script:  NIDAL: We are going to my uncle's wedding in Nablus. My cousin Karim is getting married.
10:00:09:13  none     Come on, hurry.
```

If no scene matches well enough (short clips, mostly improvised, very noisy audio), the clip is
translated without the script and `review.txt` says so, with the best guess. Give the scene
number to use the script anyway.

Before the first run with a new script, check that MakeSubs reads it correctly (especially
a PDF):

```bash
makesubs --script film.pdf --script-info
```

```
Script film.pdf: 3 scenes, 9 lines of dialogue, 4 characters, English
Characters: NIDAL, YASMIN, SOLDIER, KARIM

scene 1        INT. KITCHEN - NIGHT                                3 lines  NIDAL, YASMIN
scene 2        EXT. CHECKPOINT - DAY                               4 lines  SOLDIER, NIDAL, YASMIN
...
```

If the scene count, the characters or the first lines look wrong, try the `.fdx` if there is
one. (The window shows the same when you choose the script.)

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

The installer also adds a `makesubs` command, which does the same as the window and is handy
for batches and scripts. Open a **new** Terminal/PowerShell window after installing so the
command is found.

```
makesubs [options] CLIP [CLIP ...]
```

With no options it uses the same defaults as the window: Arabic + English spoken, English
subtitles, `.srt`, all audio channels mixed, files written next to each clip.

### Examples

```bash
# English subtitles for one clip
makesubs A001C003.mov

# English and Danish, for Premiere/Resolve and Avid
makesubs A001C003.mov B002C011.mxf --to en,da --format srt,avid

# Only Arabic is spoken: say so, it gives better results
makesubs A001C003.mov --spoken ar

# Check timecode, frame rate and audio channels before choosing one
makesubs --info A001C003.mov

# Use only channel 1 (e.g. the boom) and write the files into another folder
makesubs A001C003.mov --channel 1 --out ~/Desktop/subs

# Help the translator spell names and places
makesubs A001C003.mov --to en,da --note "Characters: Nidal, Yasmin. Places: Jenin, Nablus."

# Follow the script (scene found per clip), or tell it the scene
makesubs A001C003.mov --script film.fdx
makesubs A001C003.mov A001C004.mov --script film.fdx --scene 23
```

A whole day's clips (MakeSubs takes files, not folders, on the command line):

```bash
# Mac
makesubs /Volumes/Dailies/Day01/*.mov --to en,da
```

```powershell
# Windows (PowerShell)
makesubs (Get-ChildItem D:\Dailies\Day01\*.mov).FullName --to en,da
```

### Example output

Real output from a 29.97 drop-frame test clip with two mono audio tracks and Syrian Arabic
speech.

`makesubs --info B002C011.mxf` shows what MakeSubs reads from the clip:

```
B002C011.mxf: 54.0s, 29.970 DF, TC 09:59:58;00, a0:1ch, a1:1ch
  reel: -; channels: 1 (stream 1, ch 1), 2 (stream 2, ch 1)
```

`makesubs B002C011.mxf --to en,da --format srt,avid` writes these files next to the clip:

```
B002C011.transcript.srt        B002C011.transcript.avid.txt
B002C011.en.srt                B002C011.en.avid.txt
B002C011.da.srt                B002C011.da.avid.txt
B002C011.review.txt
```

`B002C011.transcript.srt` contains what was said, in the spoken language. Times start at the
clip's first frame:

```
1
00:00:00,601 --> 00:00:03,971
مرحبا أنا اسمي دال وأنا من سوريا أعيش
```

`B002C011.en.srt`:

```
1
00:00:00,601 --> 00:00:03,971
Hello, my name is Nidal
and I'm from Syria. I live
```

`B002C011.da.avid.txt` is the same subtitle for Avid, on the clip's source timecode:

```
@ makesubs

<begin subtitles>

09:59:58;18 10:00:01;29
Hej, mit navn er Nidal,
og jeg er fra Syrien.
```

`review.txt` from a different clip, where one line was hard to hear:

```
Clip:        A001C003.mov
Source TC:   14:23:11:05   fps: 23.976
Languages:   en, ar
Translation: local LLM (gemma3:12b)
Subtitles:   13, of which 1 need review ([?] in the files)
...
14:24:20:02  (50%)  وانتهى الامر
```

The timecode on each review line takes you straight to the spot in the source clip.

### Options

| Option | Default | What it does |
|---|---|---|
| `-t`, `--to LANGS` | `en` | Subtitle languages, comma-separated: `en`, `en,da`, `en,da,de`. Anything other than `en` needs the high-quality translator. |
| `-s`, `--spoken LANGS` | `ar,en` | Languages spoken in the clips, or `auto`. Name the real ones: it beats `auto`, and a single language (e.g. `ar`) is most robust when nobody switches. |
| `-f`, `--format LIST` | `srt` | `srt` (Premiere/Resolve), `avid` (SubCap), or `srt,avid`. |
| `-c`, `--channel N` | `mix` | `mix` averages all audio, or a channel number as listed by `--info`. |
| `-o`, `--out FOLDER` | next to clip | Write subtitle files into this folder (created if missing). |
| `--note TEXT` | | Names, places and terms for the translator. |
| `--script FILE` | | Screenplay (`.pdf`, `.fdx`, `.fountain` or `.txt`) to follow, see [Following the script](#following-the-script). Needs the high-quality translator. |
| `--scene N` | found per clip | Script scene number(s) of the clips, e.g. `23` or `23,24`. |
| `--script-info` | | Only show what MakeSubs reads from `--script`. |
| `--avid-start TC` | clip source TC | Sequence start timecode for SubCap files, e.g. `01:00:00:00`. |
| `--overwrite` | off | Replace existing subtitle files (otherwise those clips are skipped). |
| `--info` | | Only show each clip's timecode, frame rate, reel and audio channels. |
| `--dialect levantine` | standard | Dialect speech model for the Arabic, see *Arabic speech* above. With `--setup`, also downloads it. |
| `--setup` | | Download the models now, then exit (the installer does this). |
| `--version` | | Show the installed version. |

Less common options:

| Option | Default | What it does |
|---|---|---|
| `--translator` | `auto` | `llm` (Ollama, any language), `whisper` (built-in, English only), or `auto` (`llm` when Ollama is running). |
| `--llm-model NAME` | `gemma3:12b` | Ollama model used for translation. |
| `--threshold N` | `0.6` | Confidence (0–1) below which a line is marked `[?]`. Higher marks more lines. |
| `--no-marks` | off | Leave `[?]` out of the subtitle text; flagged lines are still listed in `review.txt`. |
| `--backend` | `auto` | Force `faster-whisper` (NVIDIA/CPU) or `mlx` (Apple Silicon). |
| `--whisper-model NAME` | large-v3 | Different speech model for a quick test on a slow machine: `medium` on Windows, `mlx-community/whisper-medium-mlx` on a Mac. Expect worse Arabic. |

Language codes are the usual two-letter codes: `ar` Arabic, `en` English, `da` Danish,
`de` German, `fr` French, `sv` Swedish, `no` Norwegian, `he` Hebrew, and so on.

When it finishes, `makesubs` lists every file it wrote per clip. It exits with status 0 when
all clips succeeded and 1 if any failed, so it can be used in scripts.

## Known limitations

- **Dialect and scratch audio**: tested on Levantine Arabic (Palestinian, Syrian, Lebanese),
  including real Palestinian Arabic dailies. Clean speech is transcribed well; the dialect
  model helps a lot with Levantine Arabic, but dialect words still trip up the translation
  now and then (a dialect word left untranslated, say). Heavily noisy or distant audio
  can produce invented text; that is what the `[?]` marks are for. Validate on real
  camera-test footage before principal photography.
- **Language switches inside a sentence**: language is detected per stretch of speech (up to
  about 15 s), so a short phrase in a second language right next to the first may be written
  in the wrong language.
- **Scripts**: PDFs are read from where each piece of text sits on the page, which works for
  standard screenplay formatting from Final Draft, WriterDuet, Word and the like, but not for
  scanned pages or side-by-side (dual) dialogue (the `.fdx` handles those). Plain text is read
  as Fountain (a character name in capitals with the speech right under it) or, if it keeps
  the page layout, like a PDF. Finding the scene uses the English translation, so it works
  best when the script is in English and English is one of the subtitle languages; otherwise,
  pick the scene. About one scripted line in four isn't recognised when the actors reword a
  lot (it is then listed as not in the script, and keeps its translation), so check
  `review.txt` on important scenes.
- **Camera-original raw formats** (BRAW, R3D, ARRIRAW) aren't supported; use the transcodes.
  Avid OP-Atom MXF stores audio in separate files: point MakeSubs at the audio MXF.

## Development

```bash
uv sync
uv run pytest
uv run makesubs-gui
```

Code lives in `src/makesubs/`: `probe` (metadata), `audio` (decoding), `asr` (Whisper, voice
activity detection, language detection; dialect models in `DIALECTS`), `shape` (cue timing
and line breaks), `translate` (Ollama: translation in anchored batches, matching subtitles to
the script by meaning), `script` (screenplay reading and scene finding), `writers` (SRT and
SubCap), `pipeline`, `cli`, `gui`. The tests need no models or Ollama; `tests/data` has a small
screenplay as PDF, Final Draft and Fountain. The README screenshots are made from the real
window, offscreen, with the test screenplay.
