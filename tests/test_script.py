"""Following a screenplay: reading .fdx/.pdf, finding the scene, script-aware translation."""
import json
from pathlib import Path

import pytest

from makesubs import pipeline, translate
from makesubs import script as screenplay
from makesubs.asr import Segment, Transcript, Word
from makesubs.pipeline import Options
from makesubs.shape import Cue

_REAL_MEANING = translate.meaning_matrix


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    """Tests match on words unless they set meanings themselves (no Ollama needed)."""
    monkeypatch.setattr(translate, "meaning_matrix", lambda said, scripted: None)

DATA = Path(__file__).parent / "data"

# tests/data/screenplay.pdf was made from screenplay.fountain with screenplain (standard
# screenplay layout); the same three scenes, as Final Draft XML:
FDX = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<FinalDraft DocumentType="Script" Template="No" Version="5">
<Content>
<Paragraph Number="1" Type="Scene Heading"><Text>INT. KITCHEN - NIGHT</Text></Paragraph>
<Paragraph Type="Action"><Text>Yasmin stirs a pot.</Text></Paragraph>
<Paragraph Type="Character"><Text>NIDAL (V.O.)</Text></Paragraph>
<Paragraph Type="Dialogue"><Text>Is the bread ready? We leave for </Text><Text Style="Italic">Nablus</Text><Text> before the checkpoint closes.</Text></Paragraph>
<Paragraph Type="Character"><Text>YASMIN</Text></Paragraph>
<Paragraph Type="Parenthetical"><Text>(not looking up)</Text></Paragraph>
<Paragraph Type="Dialogue"><Text>Not yet. Your mother wanted the olives packed first.</Text></Paragraph>
<Paragraph Type="Transition"><Text>CUT TO:</Text></Paragraph>
<Paragraph Number="2A" Type="Scene Heading"><Text>EXT. CHECKPOINT - DAY</Text></Paragraph>
<Paragraph Type="Character"><Text>SOLDIER</Text></Paragraph>
<Paragraph Type="Dialogue"><Text>Papers. Everyone out of the car.</Text></Paragraph>
<Paragraph><DualDialogue>
<Paragraph Type="Character"><Text>NIDAL ^</Text></Paragraph>
<Paragraph Type="Dialogue"><Text>My cousin Karim is getting married.</Text></Paragraph>
<Paragraph Type="Character"><Text>YASMIN</Text></Paragraph>
<Paragraph Type="Dialogue"><Text>Only olives and a cake.</Text></Paragraph>
</DualDialogue></Paragraph>
<Paragraph Type="Action"><Text>The soldier waves them on.</Text></Paragraph>
</Content>
</FinalDraft>
"""


@pytest.fixture
def fdx(tmp_path):
    p = tmp_path / "film.fdx"
    p.write_text(FDX, encoding="utf-8")
    return p


def test_read_fdx(fdx):
    sc = screenplay.load(fdx)
    assert [s.number for s in sc.scenes] == ["1", "2A"]
    assert sc.scenes[1].heading == "EXT. CHECKPOINT - DAY"
    first = sc.scenes[0].lines
    assert [ln.character for ln in first] == ["NIDAL", "YASMIN"]  # (V.O.) dropped
    assert first[0].text == "Is the bread ready? We leave for Nablus before the checkpoint closes."
    assert [(ln.character, ln.text) for ln in sc.scenes[1].lines] == [
        ("SOLDIER", "Papers. Everyone out of the car."),
        ("NIDAL", "My cousin Karim is getting married."),  # dual dialogue, ^ dropped
        ("YASMIN", "Only olives and a cake.")]
    assert sc.find("2a") is sc.scenes[1] and sc.find("3") is None


def test_read_pdf():
    sc = screenplay.load(DATA / "screenplay.pdf")
    assert [s.number for s in sc.scenes] == ["1", "2", "3"]
    assert [s.heading for s in sc.scenes] == [
        "INT. KITCHEN - NIGHT", "EXT. CHECKPOINT - DAY", "INT. UNCLE'S HOUSE - NIGHT"]
    assert sc.line_count == 9
    yasmin = sc.scenes[0].lines[1]
    # parenthetical "(not looking up)" left out, the three printed lines joined into one speech
    assert (yasmin.character, yasmin.text) == (
        "YASMIN", "Not yet. Your mother wanted the olives packed first, and the jars are still wet.")
    assert sc.scenes[0].lines[0].character == "NIDAL"  # "NIDAL (V.O.)"
    assert all("CUT TO" not in ln.text for s in sc.scenes for ln in s.lines)
    assert sc.lang == "en"
    assert set(sc.characters) == {"NIDAL", "YASMIN", "SOLDIER", "KARIM"}


def test_layout_without_scene_numbers_and_across_pages():
    rows = [
        "INT. KITCHEN - NIGHT", "",
        "Yasmin stirs a pot while the radio crackles with news from the city.", "",
        "                      NIDAL", "          We leave before the check-",
        "          point closes.", "                    (MORE)", "", "2.", "",
        "                      NIDAL (CONT'D)", "          Tell my mother.", "",
        "                                                   CUT TO:", "",
        "EXT. ROAD - DAY", "",
        "                      YASMIN", "          Faster.",
    ]
    scenes = screenplay.parse_layout(rows)
    assert [s.number for s in scenes] == ["", ""]
    assert [(ln.character, ln.text) for ln in scenes[0].lines] == [
        ("NIDAL", "We leave before the checkpoint closes."), ("NIDAL", "Tell my mother.")]
    assert scenes[1].lines[0].text == "Faster."


def test_unreadable_script(tmp_path):
    bad = tmp_path / "notes.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(RuntimeError, match="could not read"):
        screenplay.load(bad)
    with pytest.raises(RuntimeError, match=".fdx"):
        screenplay.load(tmp_path / "script.docx")


def test_detect_language():
    assert screenplay.detect_language("You know that I was not there, and what they said is "
                                      "not true. " * 4) == "en"
    assert screenplay.detect_language("Jeg ved ikke hvad du har gjort, og det er ikke min skyld "
                                      "at vi skal gå nu. " * 4) == "da"
    assert screenplay.detect_language("too short") == ""


def test_scene_finder_picks_the_scene_from_a_rough_translation():
    sc = screenplay.load(DATA / "screenplay.pdf")
    finder = screenplay.SceneFinder(sc)
    m, _ = finder.locate("Papers please. Everyone get out. We go to the wedding of my uncle "
                         "in Nablus, my cousin Karim marries.")
    assert m and m.scenes[0].number == "2" and "karim" in m.words and "nablus" in m.words
    m, guess = finder.locate("I love you so much, it's okay.")
    assert m is None and guess is not None and guess.coverage < screenplay.MIN_COVERAGE
    assert finder.locate("") == (None, None)


def test_parse_answer():
    a = translate.parse_answer({"text": " Hello ", "script": "s4", "match": "Same"})
    assert (a.text, a.script_ref, a.match) == ("Hello", "S4", "same")
    a = translate.parse_answer({"text": "Hi", "script": 2, "match": "changed"})
    assert (a.script_ref, a.match) == ("S2", "changed")
    a = translate.parse_answer({"text": "Hi", "script": None, "match": "none"})
    assert (a.script_ref, a.match) == ("", "none")
    a = translate.parse_answer({"text": "Hi", "script": "S3", "match": "none"})
    assert a.script_ref == ""  # "not in the script" can't point at a script line
    a = translate.parse_answer({"text": "Hi", "script": "S12 NIDAL: Hi there.", "match": "same"})
    assert (a.script_ref, a.match) == ("S12", "same")  # the model quoted the whole line
    assert translate.parse_answer("plain").text == "plain"


def _answers(prompt: str, make) -> dict:
    lines = json.loads(prompt.rsplit("\n\n", 1)[1])
    return {k: make(k, v) for k, v in lines.items()}


def _fake_model(monkeypatch, said: dict, verdict, seen: list | None = None):
    """Translation prompts get said[number]; comparison prompts get verdict(subtitle)."""
    def fake(model, prompt, num_ctx=8192, temperature=0.2):
        if seen is not None:
            seen.append(prompt)
        if "\n\nLine: " in prompt:  # a single line, retried on its own
            line = prompt.rsplit("Line: ", 1)[1]
            return {"text": said.get(line, line)}
        items = json.loads(prompt.rsplit("\n\n", 1)[1])
        if "Each numbered item pairs" in prompt:
            return {k: verdict(v["said"]) for k, v in items.items()}
        return {k: said[k] for k in items}
    monkeypatch.setattr(translate, "_generate_raw", fake)


def test_translate_cues_with_script(monkeypatch):
    ref = translate.ScriptRef([("S1", "NIDAL", "Is the bread ready?"),
                               ("S2", "YASMIN", "Lars sold that boat in March.")], "en")
    seen = []
    said = {"1": "Is the bread done?", "2": "Lars sold the boat in April.", "3": "Let's go."}
    # the model calls both "same", but the month differs in the second: what was said wins
    _fake_model(monkeypatch, said, lambda t: "same", seen)
    cues = [Cue(0, 1, "هل الخبز جاهز؟", "ar", 0.9), Cue(2, 3, "لارس باع القارب في أبريل", "ar", 0.9),
            Cue(4, 5, "يلا نروح", "ar", 0.9)]
    out = translate.translate_cues(cues, "en", draft="rough draft", progress=lambda s: None,
                                   script=ref)
    assert [(c.text, c.script_ref, c.script_match) for c in out] == [
        ("Is the bread ready?", "S1", "same"),
        ("Lars sold the boat in April.", "S2", "changed"),
        ("Let's go.", "", "none")]
    first, second = seen
    # translation never sees the script, so it cannot pull what was said towards it
    assert "Is the bread ready" not in first and "rough draft" in first
    # the comparison only sees the pairs the code lined up ("Let's go." matches nothing)
    assert '"script": "Is the bread ready?"' in second and "Let's go" not in second


def test_align_keeps_the_script_order():
    script = ["Hummus, vine leaves, tabbouleh.", "My favourite.", "You've been sleeping a lot.",
              "It's not about us, Elias, it's about everything.",
              "Whatever is going on is much bigger than you and me.",
              "You haven't been yourself lately."]
    said = ["Action!", "Hummus, vine leaves and tabbouleh.", "You're sleeping a lot.",
            "I feel they're moving.",                       # crew talk: shares nothing useful
            "It's not just about us, it's about everyone.",
            "What's happening here is bigger than me and you, Elias.",
            "You haven't been yourself lately, you know."]
    assert translate.align(said, script) == [None, 0, 2, None, 3, 4, 5]
    # repeated takes of a line after the scene moved on are not pulled back
    assert translate.align(["My favourite.", "You've been sleeping a lot."], script) == [1, 2]


def test_facts():
    assert translate._facts("The ferry leaves in ten minutes.") == {"10"}
    assert translate._facts("The ferry sails in 10 minutes.") == {"10"}
    assert translate._facts("We drive at noon on Monday.") == {"12:00", "monday"}
    assert translate._facts("You may go.") == set()


def test_compare_when_script_is_in_another_language(monkeypatch):
    ref = translate.ScriptRef([("S1", "NIDAL", "Er brødet klar?")], "da")
    p = translate._compare_prompt({"1": "x"}, ref, "en")
    assert "Danish" in p and "script's own wording" not in p
    monkeypatch.setattr(translate, "_generate", lambda m, p, n=8192, t=0.2: _answers(
        p, lambda k, v: translate.parse_answer(
            {"text": "Er brødet klar?", "script": "S1", "match": "same"})))
    cues = [Cue(0, 1, "Is the bread ready?", "en", 0.9)]
    translate.compare_script(cues, ref, "en", progress=lambda s: None)
    # matched, but the subtitle stays in the subtitle language
    assert (cues[0].text, cues[0].script_ref, cues[0].script_match) == (
        "Is the bread ready?", "S1", "same")


def test_compare_ignores_lines_that_match_nothing(monkeypatch):
    ref = translate.ScriptRef([("S1", "NIDAL", "Is the bread ready?")], "en")
    _fake_model(monkeypatch, {}, lambda t: "same")  # would say "same" to anything
    cues = [Cue(0, 1, "Hello.", "en", 0.9)]
    translate.compare_script(cues, ref, "en", progress=lambda s: None)
    assert (cues[0].text, cues[0].script_ref, cues[0].script_match) == ("Hello.", "", "none")


def test_translate_without_script_is_unchanged(monkeypatch):
    monkeypatch.setattr(translate, "_generate", lambda m, p, n=8192, t=0.2: _answers(
        p, lambda k, v: translate.parse_answer("Hello")))
    out = translate.translate_cues([Cue(0, 1, "مرحبا", "ar", 0.9)], "en", progress=lambda s: None)
    assert out[0].text == "Hello" and out[0].script_match == ""


def test_excerpt_is_capped(monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_EXCERPT", 120)
    sc = screenplay.load(DATA / "screenplay.pdf")
    m = screenplay.Match(list(sc.scenes[:2]), 1.0, [], by_hand=True)
    ref, kept = pipeline._script_ref(m, "en")
    assert m.scenes == [sc.scenes[0]]  # the second scene didn't fit
    assert sum(len(t) for _r, _c, t in ref.lines) <= 120 and kept and len(kept) < 3


def test_validate_script_needs_the_llm(monkeypatch, fdx):
    monkeypatch.setattr(translate, "available", lambda: False)
    with pytest.raises(RuntimeError, match="script needs"):
        pipeline.validate(Options(script=fdx))
    with pytest.raises(RuntimeError, match="only work together with a script"):
        pipeline.validate(Options(scenes=["1"], translator="whisper"))
    with pytest.raises(RuntimeError, match="has no scene 9"):
        pipeline.load_script(Options(script=fdx, scenes=["1", "9"]))


def _words(text, start):
    return [Word(start + i * 0.4, start + i * 0.4 + 0.3, " " + w, 0.9)
            for i, w in enumerate(text.split())]


def _segment(text, start, lang):
    words = _words(text, start)
    return Segment(words[0].start, words[-1].end, text, lang, words)


@pytest.fixture
def fake_engines(monkeypatch):
    """Whisper and Ollama replaced by canned answers; the clip itself is really read."""
    class Engine:
        name = "fake"

        def unload(self):
            pass

    source = [_segment("الأوراق لو سمحت انزلوا كلكم من السيارة", 0.2, "ar"),
              _segment("رايحين على عرس ابن عمي كريم في نابلس", 4.0, "ar"),
              _segment("يلا بسرعة", 9.5, "ar")]
    english = [_segment("Papers please, everyone get out of the car.", 0.2, "en"),
               _segment("We are going to my cousin Karim's wedding in Nablus.", 4.0, "en"),
               _segment("Come on, quickly.", 9.5, "en")]
    monkeypatch.setattr(pipeline.asr, "create_engine", lambda *a: Engine())
    monkeypatch.setattr(pipeline.asr, "transcribe",
                        lambda *a: Transcript(source, english, ["ar"]))
    monkeypatch.setattr(pipeline, "load_audio", lambda *a: None)
    monkeypatch.setattr(translate, "available", lambda: True)
    monkeypatch.setattr(translate, "ensure_model", lambda *a: None)
    monkeypatch.setattr(translate, "unload", lambda *a: None)
    prompts = []
    said = {"1": "Papers, please. All of you out of the car.",
            "2": "We're going to my cousin Karim's wedding in Nablus.",
            "3": "Come on, hurry."}
    verdicts = {said["1"]: "same", said["2"]: "changed"}
    _fake_model(monkeypatch, said, lambda t: verdicts.get(t, "none"), prompts)
    return prompts


def test_run_follows_the_script(clip, fake_engines, tmp_path):
    opts = Options(targets=["en"], spoken=["ar"], script=DATA / "screenplay.pdf",
                   out_dir=tmp_path)
    log = []
    jobs = pipeline.run([clip], opts, log.append)
    job = jobs[0]
    assert not job.error, job.error
    assert job.script_match and job.script_match.scenes[0].number == "2"
    assert any("following the script: scene 2" in s for s in log)
    translation, comparison = fake_engines
    assert "Characters in the script:" in translation and "Papers. Everyone" not in translation
    assert '"script": "Papers. Everyone out of the car."' in comparison
    srt = (tmp_path / f"{clip.stem}.en.srt").read_text(encoding="utf-8")
    assert "Papers. Everyone out of the car." in srt
    review = (tmp_path / f"{clip.stem}.review.txt").read_text(encoding="utf-8")
    assert "screenplay.pdf, scene 2 EXT. CHECKPOINT - DAY" in review
    assert "3 lines compared: 1 as scripted, 1 changed, 1 not in the script" in review
    assert "changed  We're going to my cousin Karim's wedding in Nablus." in review
    assert "script:  NIDAL: We are going to my uncle's wedding in Nablus." in review
    assert "Come on, hurry." in review


def test_run_scene_by_hand_and_no_match(clip, fake_engines, tmp_path, monkeypatch):
    opts = Options(targets=["en"], spoken=["ar"], script=DATA / "screenplay.pdf",
                   scenes=["3"], out_dir=tmp_path, overwrite=True)
    job = pipeline.run([clip], opts, lambda s: None)[0]
    assert job.script_match.by_hand and job.script_match.scenes[0].number == "3"
    review = (tmp_path / f"{clip.stem}.review.txt").read_text(encoding="utf-8")
    assert "screenplay.pdf, scene 3" in review and "set by hand" in review

    monkeypatch.setattr(pipeline.screenplay, "MIN_COVERAGE", 0.99)
    opts = Options(targets=["en"], spoken=["ar"], script=DATA / "screenplay.pdf",
                   out_dir=tmp_path, overwrite=True)
    job = pipeline.run([clip], opts, lambda s: None)[0]
    assert job.script_match is None and "Each numbered item pairs" not in fake_engines[-1]
    review = (tmp_path / f"{clip.stem}.review.txt").read_text(encoding="utf-8")
    assert "no scene matched with confidence (best guess scene 2" in review


def _line(y, *pieces, w=7.0):
    """One line of a page as PDF text pieces: (column, text) at w points per character."""
    return [(y, col * w, text) for col, text in pieces]


def test_pdf_pieces_word_export():
    """What a Word-exported screenplay looks like: scene numbers on both sides, revision
    asterisks, names on their own line, words split where the revision colour changes and
    word spaces drawn as gaps. Lines are 12 pt apart, a blank line 24."""
    w = 7.0
    page = [
        _line(700, (8, "12"), (15, "12"), (15 + 2, "EXT. TERRACE, ALIA’S"), (38, "TOWNHOUSE - DAY")),
        _line(676, (15, "Alia enters the terrace.")),
        _line(652, (35, "ALIA")),
        _line(640, (25, "Why so tense"), (37, "?")),          # punctuation touches
        _line(616, (35, "ELIAS (CONT’D)")),
        _line(604, (25, "I unclogged the"), (41, "carbu"), (46, "retor.")),  # colour split
        _line(592, (25, "Maybe you didn’t tighten it"), (25 + 28, "enough.")),  # gap = space
    ]
    page[0] = [(700, 8 * w, "12"), (700, 15 * w, "12"), (700, 18 * w, "EXT. TERRACE, ALIA’S"),
               (700, 39 * w, "TOWNHOUSE - DAY")]
    assert screenplay._char_width([page]) == w
    rows = screenplay._clean(screenplay._page_rows(page, w))
    scenes = screenplay.parse_layout(rows)
    assert [(s.number, s.heading) for s in scenes] == [("12", "EXT. TERRACE, ALIA’S TOWNHOUSE - DAY")]
    assert [(ln.character, ln.text) for ln in scenes[0].lines] == [
        ("ALIA", "Why so tense?"),
        ("ELIAS", "I unclogged the carburetor. Maybe you didn’t tighten it enough.")]


def test_heading_with_glued_scene_numbers():
    rows = ["        4a 4aINT. DARK SPACE - DAY", "", "                    ALIA",
            "          Hello?", "", "        GREEN"]
    assert [(s.number, s.heading) for s in screenplay.parse_layout(rows)] == [
        ("4a", "INT. DARK SPACE - DAY")]


def test_title_page_is_not_dialogue():
    pad = " " * 10  # action margin; dialogue 10 further in, character names 22
    rows = [pad + " " * 22 + "GREEN", pad + " " * 10 + "With locked scene numbers", "",
            "1" + " " * 9 + "INT. KITCHEN - NIGHT", "", pad + " " * 22 + "ALIA",
            pad + " " * 10 + "Hello?"]
    scenes = screenplay.parse_layout(rows)
    assert [s.number for s in scenes] == ["1"] and scenes[0].lines[0].character == "ALIA"


def test_script_wording_for_a_line_split_over_subtitles(monkeypatch):
    ref = translate.ScriptRef([("S1", "ANNA", "Don't lie to me, Mikkel. Not today."),
                               ("S2", "ANNA", "It's Dad's birthday. We're driving there at noon."),
                               ("S3", "OFFICER", "Passports, please."),
                               ("S4", "OFFICER", "Everyone shows a passport. Even for the day.")],
                              "en")
    said = ["Don't you lie to me, Mikkel.", "Not today.", "It's Dad's birthday.",
            "Mum is waiting for us there.", "Thanks.",  # "Thanks": misheard "Passports, please"
            "Everyone must show their passport.", "Also on a day trip."]
    _fake_model(monkeypatch, {}, lambda t: "same")
    cues = [Cue(i, i + 1, t, "en", 0.9) for i, t in enumerate(said)]
    translate.compare_script(cues, ref, "en", progress=lambda s: None)
    assert [(c.text, c.script_match) for c in cues] == [
        ("Don't lie to me, Mikkel.", "same"), ("Not today.", "same"),
        ("It's Dad's birthday.", "same"),          # never "... at noon": not said here
        ("Mum is waiting for us there.", "none"),
        ("Thanks.", "none"),   # shares no word with S3: never swapped for script text
        ("Everyone shows a passport.", "same"),    # each subtitle gets its own sentence
        ("Even for the day.", "same")]


def test_batches_are_even():
    assert [len(r) for r in translate._batches(42, 40)] == [21, 21]
    assert [len(r) for r in translate._batches(40, 40)] == [40]
    assert [len(r) for r in translate._batches(81, 40)] == [27, 27, 27]
    assert [len(r) for r in translate._batches(42)] == [11, 11, 11, 9]  # default: 12 at most
    assert translate._batches(0) == []


def test_each_batch_sees_only_its_own_part_of_the_draft(monkeypatch):
    """With the whole clip's draft, the model translated a short last batch from the draft's
    first sentences ("Action!") instead of from its own lines."""
    monkeypatch.setattr(translate, "BATCH", 2)
    seen = []
    monkeypatch.setattr(translate, "_generate", lambda m, p, n=8192, temperature=0.2: seen.append(p)
                        or _answers(p, lambda k, v: translate.parse_answer(f"line {len(seen)}.{k}")))
    draft = [Segment(0, 2, " Action!", "en", []), Segment(10, 12, " Hummus and vine leaves.", "en", []),
             Segment(30, 32, " Something is off.", "en", [])]
    cues = [Cue(0, 2, "أكشن", "ar", 0.9), Cue(10, 12, "حمص", "ar", 0.9), Cue(30, 32, "في اشي", "ar", 0.9)]
    translate.translate_cues(cues, "en", draft=draft, progress=lambda s: None)
    assert "Action!" in seen[0] and "Something is off" not in seen[0]
    assert "Something is off." in seen[-1] and "Action!" not in seen[-1]


def test_one_sentence_said_over_two_subtitles_is_not_repeated(monkeypatch):
    ref = translate.ScriptRef([("S1", "ELIAS", "I also made baklava."),
                               ("S2", "ALIA", "When did you have time?")], "en")
    said = ["I also made...", "Baklava, made it.", "When did you find time?"]
    _fake_model(monkeypatch, {}, lambda t: "same")
    cues = [Cue(i, i + 1, t, "en", 0.9) for i, t in enumerate(said)]
    translate.compare_script(cues, ref, "en", progress=lambda s: None)
    # both say part of S1, which can't be split: they keep their own words, not S1 twice
    assert [c.text for c in cues] == ["I also made...", "Baklava, made it.", "When did you have time?"]
    assert [c.script_ref for c in cues] == ["S1", "S1", "S2"]


def test_scene_is_found_from_the_translation_when_whisper_cannot_translate(
        clip, fake_engines, tmp_path, monkeypatch):
    """Dialect-tuned Whisper models often hand back Arabic for the English draft."""
    source = [_segment("الأوراق لو سمحت انزلوا كلكم من السيارة", 0.2, "ar"),
              _segment("رايحين على عرس ابن عمي كريم في نابلس", 4.0, "ar"),
              _segment("يلا بسرعة", 9.5, "ar")]
    monkeypatch.setattr(pipeline.asr, "transcribe",
                        lambda *a: Transcript(source, source, ["ar"]))  # "draft" is Arabic too
    opts = Options(targets=["en"], spoken=["ar"], script=DATA / "screenplay.pdf",
                   out_dir=tmp_path, overwrite=True)
    job = pipeline.run([clip], opts, lambda s: None)[0]
    assert not job.error, job.error
    assert job.script_match and job.script_match.scenes[0].number == "2"
    assert "<reference>" not in fake_engines[0]  # no Arabic passed off as an English reference


def test_fountain_reads_like_the_pdf():
    fountain = screenplay.load(DATA / "screenplay.fountain")
    pdf = screenplay.load(DATA / "screenplay.pdf")
    lines = lambda sc: [(x.heading, l.character, l.text) for x in sc.scenes for l in x.lines]
    assert lines(fountain) == lines(pdf) and len(fountain.scenes) == 3


def test_flat_text_export(tmp_path):
    """Like a plain-text export of a Word or Final Draft script: no indentation."""
    f = tmp_path / "draft.txt"
    f.write_text(
        "IN MEMORY OF TIMES TO COME\nManuscript - Someone\n\n"
        "==========\n\n"
        "1  EXT. CEMETERY - DAY (PAST)\n\n"
        "MIMI (10) is walking towards a cemetery.\n\n"
        "CREDITS BEGIN\n\n"
        "12  EXT. TERRACE, ALIA'S TOWNHOUSE - DAY\n\n"
        "ALIA\nWhy so tense?\n\n"
        "Elias gestures towards the generator.\n\n"
        "ELIAS (CONT'D)\n(quietly)\nThis.\nIt won't start.\n\n"
        "12A  INT. KITCHEN - DAY #12A#\n\n"
        "@McGregor\nHello.\n", encoding="utf-8")
    sc = screenplay.load(f)
    assert [(x.number, x.heading) for x in sc.scenes] == [
        ("12", "EXT. TERRACE, ALIA'S TOWNHOUSE - DAY"), ("12A", "INT. KITCHEN - DAY")]
    assert [(l.character, l.text) for x in sc.scenes for l in x.lines] == [
        ("ALIA", "Why so tense?"), ("ELIAS", "This. It won't start."), ("MCGREGOR", "Hello.")]
    assert sc.all_scenes == 3  # the cemetery scene has no dialogue; "CREDITS BEGIN" isn't a name


def test_text_drawn_twice_is_read_once():
    """Some PDFs draw a (CONT'D) header twice at the same spot (or text twice to fake bold)."""
    class Page:
        def extract_text(self, visitor_text):
            for _ in range(2):
                visitor_text("ALIA (CONT’D)", [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 252.0, 774.0], None, 12)
            visitor_text("Whatever is going on.", [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 180.0, 762.0], None, 12)
    lines = screenplay._page_pieces(Page())
    assert [[t for _, _, t in line] for line in lines] == [["ALIA (CONT’D)"], ["Whatever is going on."]]


def test_a_fragment_keeps_its_translation(monkeypatch):
    """'And I also made...' unmatched, then just 'Baklava.': not the whole script line."""
    ref = translate.ScriptRef([("S1", "ELIAS", "I also made baklava.")], "en")
    _fake_model(monkeypatch, {}, lambda t: "same")
    cues = [Cue(0, 1, "Baklava.", "en", 0.9)]
    translate.compare_script(cues, ref, "en", progress=lambda s: None)
    assert (cues[0].text, cues[0].script_ref, cues[0].script_match) == ("Baklava.", "S1", "same")


def test_a_line_answered_out_of_step_is_translated_again(monkeypatch):
    """The model lost count: line 2's answer is line 3's (its anchor is line 3's first word)."""
    source = ["كل شي هون حلو", "ما باعرف الياس", "واكلك زاكي"]
    calls = []

    def fake(model, prompt, num_ctx=8192, temperature=0.2):
        if "\n\nLine: " in prompt:  # the retry, alone
            calls.append(prompt.rsplit("Line: ", 1)[1])
            return {"text": "I don't know, Elias."}
        items = json.loads(prompt.rsplit("\n\n", 1)[1])
        calls.append(items)
        if len(items) == 3:  # the batch: line 2 answered with line 3's translation
            return {"1": {"first": "كل", "text": "Everything here is nice."},
                    "2": {"first": "واكلك", "text": "And your food is delicious."},
                    "3": {"first": "واكلك", "text": "And your food is delicious."}}

    monkeypatch.setattr(translate, "_generate_raw", fake)
    cues = [Cue(i, i + 1, t, "ar", 0.9) for i, t in enumerate(source)]
    out = translate.translate_cues(cues, "en", progress=lambda s: None)
    assert [c.text for c in out] == ["Everything here is nice.", "I don't know, Elias.",
                                     "And your food is delicious."]
    assert calls[1:] == ["ما باعرف الياس"]  # only the wrong line again, on its own


def test_arabic_labelled_english_is_still_translated(monkeypatch):
    seen = []

    def fake(model, prompt, num_ctx=8192, temperature=0.2):
        items = json.loads(prompt.rsplit("\n\n", 1)[1])
        seen.append(items)
        return {k: {"first": v.split()[0], "text": "I made baklava too."} for k, v in items.items()}

    monkeypatch.setattr(translate, "_generate_raw", fake)
    cues = [Cue(0, 1, "Action!", "en", 0.9), Cue(1, 2, "وكمان عملت بقلاوه", "en", 0.9)]
    out = translate.translate_cues(cues, "en", progress=lambda s: None)
    assert [c.text for c in out] == ["Action!", "I made baklava too."]
    assert seen == [{"1": "وكمان عملت بقلاوه"}]


def test_an_answer_left_in_arabic_is_not_used(monkeypatch):
    monkeypatch.setattr(translate, "_generate_raw", lambda m, p, n=8192, t=0.2: {
        "1": {"first": "انا", "text": "انا ظليت وقت"}, "text": "انا ظليت وقت"})  # always copied
    out = translate.translate_cues([Cue(0, 1, "انا ظليت وقت", "ar", 0.9)], "en",
                                   progress=lambda s: None)
    assert out[0].flagged and out[0].text == "انا ظليت وقت"  # kept and marked for review


def test_a_server_error_costs_one_request_not_the_clip(monkeypatch):
    import io
    import urllib.error
    attempts = []

    def failing(path, payload=None, timeout=1800):
        attempts.append(payload)
        raise urllib.error.HTTPError("http://x", 500, "Internal Server Error", {}, io.BytesIO())

    monkeypatch.setattr(translate, "_call", failing)
    assert translate._generate_raw("m", "prompt") == {}
    assert len(attempts) == 3  # tried again, then gave up on this request only


def test_a_line_given_its_neighbours_translation_is_redone(monkeypatch):
    source = ["الموضوع مش احنا", "اللي عم بيصير هون اكبر مني ومنك", "انا باعرف", "انا باعرف"]
    singles = []

    def fake(model, prompt, num_ctx=8192, temperature=0.2):
        if "\n\nLine: " in prompt:
            singles.append(prompt.rsplit("Line: ", 1)[1])
            return {"text": "What's happening here is bigger than you and me."}
        return {"1": {"first": "الموضوع", "text": "It's not about us."},
                "2": {"first": "اللي", "text": "It's not about us."},     # line 1's text again
                "3": {"first": "انا", "text": "I know."},
                "4": {"first": "انا", "text": "I know."}}                 # said twice: fine

    monkeypatch.setattr(translate, "_generate_raw", fake)
    out = translate.translate_cues([Cue(i, i + 1, t, "ar", 0.9) for i, t in enumerate(source)],
                                   "en", progress=lambda s: None)
    assert [c.text for c in out] == ["It's not about us.",
                                     "What's happening here is bigger than you and me.",
                                     "I know.", "I know."]
    assert singles == ["اللي عم بيصير هون اكبر مني ومنك"]


def test_align_by_meaning_finds_rewordings():
    script = ["We just harvested, but the trees are blossoming again. Out of sync with the seasons.",
              "We pick them four times a year, Alia."]
    said = ["Flowering at the wrong time.", "We go there four times a year."]
    # the words alone pair only the second; the meaning pairs both
    assert translate.align(said, script) == [None, 1]
    meaning = [[0.71, 0.20], [0.25, 0.68]]
    assert translate.align(said, script, meaning) == [0, 1]
    weak = [[0.40, 0.20], [0.25, 0.68]]  # below the floor: no pair
    assert translate.align(said, script, weak) == [None, 1]


def test_matching_falls_back_to_words_without_embeddings(monkeypatch):
    import urllib.error

    def no_embed(path, payload=None, timeout=1800):
        raise urllib.error.URLError("no embedding model")

    monkeypatch.setattr(translate, "_call", no_embed)
    assert _REAL_MEANING(["a"], ["b"]) is None


def test_a_close_pair_the_model_rejects_is_kept_as_changed(monkeypatch):
    """The model tends to say 'none' when a subtitle is only part of a long script line."""
    ref = translate.ScriptRef([("S1", "ELIAS", "You tell me you doubt your feelings for me. And "
                                "you expect me to calmly accept that.")], "en")
    said = ["And you expect me to accept this quietly.", "Can we go again?"]
    _fake_model(monkeypatch, {}, lambda t: "none")
    monkeypatch.setattr(translate, "meaning_matrix", lambda s, l: [[0.73], [0.20]])
    cues = [Cue(i, i + 1, t, "en", 0.9) for i, t in enumerate(said)]
    translate.compare_script(cues, ref, "en", progress=lambda s: None)
    assert [(c.text, c.script_match, c.script_ref) for c in cues] == [
        ("And you expect me to accept this quietly.", "changed", "S1"),  # wording kept
        ("Can we go again?", "none", "")]
