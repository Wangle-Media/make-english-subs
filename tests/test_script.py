"""Following a screenplay: reading .fdx/.pdf, finding the scene, script-aware translation."""
import json
from pathlib import Path

import pytest

from makesubs import pipeline, translate
from makesubs import script as screenplay
from makesubs.asr import Segment, Transcript, Word
from makesubs.pipeline import Options
from makesubs.shape import Cue

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
    assert translate.parse_answer("plain").text == "plain"


def _answers(prompt: str, make) -> dict:
    lines = json.loads(prompt.rsplit("\n\n", 1)[1])
    return {k: make(k, v) for k, v in lines.items()}


def test_translate_cues_with_script(monkeypatch):
    ref = translate.ScriptRef([("S1", "NIDAL", "Is the bread ready?"),
                               ("S2", "YASMIN", "Not yet.")], "en")
    seen = []

    def fake(model, prompt, num_ctx=8192):
        seen.append((prompt, num_ctx))
        return _answers(prompt, lambda k, v: translate.parse_answer(
            {"1": {"text": "Is the bread ready?", "script": "S1", "match": "same"},
             "2": {"text": "Let's go.", "script": None, "match": "none"}}[k]))

    monkeypatch.setattr(translate, "_generate", fake)
    cues = [Cue(0, 1, "هل الخبز جاهز؟", "ar", 0.9), Cue(2, 3, "يلا نروح", "ar", 0.9)]
    out = translate.translate_cues(cues, "en", draft="rough draft", progress=lambda s: None,
                                   script=ref)
    assert [(c.text, c.script_ref, c.script_match) for c in out] == [
        ("Is the bread ready?", "S1", "same"), ("Let's go.", "", "none")]
    prompt, num_ctx = seen[0]
    assert "S1 NIDAL: Is the bread ready?" in prompt and "script's own wording" in prompt
    assert "rough draft" not in prompt  # the script replaces the draft as name reference
    assert num_ctx > 8192


def test_translate_prompt_when_script_is_in_another_language():
    ref = translate.ScriptRef([("S1", "NIDAL", "Is the bread ready?")], "en")
    p = translate._prompt({"1": "x"}, "da", [], None, "", ["ar"], ref)
    assert "The script is in English, not Danish" in p and "script's own wording" not in p


def test_translate_without_script_is_unchanged(monkeypatch):
    monkeypatch.setattr(translate, "_generate", lambda m, p, n=8192: _answers(
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
    canned = {"1": {"text": "Papers. Everyone out of the car.", "script": "S1", "match": "same"},
              "2": {"text": "We're going to my cousin Karim's wedding in Nablus.",
                    "script": "S2", "match": "changed"},
              "3": {"text": "Come on, hurry.", "script": None, "match": "none"}}

    def fake(model, prompt, num_ctx=8192):
        prompts.append(prompt)
        return _answers(prompt, lambda k, v: translate.parse_answer(canned[k]))

    monkeypatch.setattr(translate, "_generate", fake)
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
    prompt = fake_engines[0]
    assert "SOLDIER: Papers. Everyone out of the car." in prompt
    assert "Characters in the script:" in prompt
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
    assert "KARIM: You made it!" in fake_engines[-1]

    monkeypatch.setattr(pipeline.screenplay, "MIN_COVERAGE", 0.99)
    opts = Options(targets=["en"], spoken=["ar"], script=DATA / "screenplay.pdf",
                   out_dir=tmp_path, overwrite=True)
    job = pipeline.run([clip], opts, lambda s: None)[0]
    assert job.script_match is None and "<script>" not in fake_engines[-1]
    review = (tmp_path / f"{clip.stem}.review.txt").read_text(encoding="utf-8")
    assert "no scene matched with confidence (best guess scene 2" in review
