"""Offline tests for media/scenes.py (lint and gen): no image generation, no network, no browser.

lint is checked rule by rule on the fixture video with one thing changed in memory. gen runs against the
stand-in codex of mediatest.py and a made-up explainer folder whose end-card script is a stand-in too. The
prompt-parity test pins the text sent to the image model: the builder's output for every channel must equal
the lines pinned in tests/fixtures/media/prompts, byte for byte.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediatest import Codex, fixture_path, needs, quota_at, read_fixture, run  # noqa: E402

import scenes  # noqa: E402  (mediatest put agent-playbook/media on sys.path)

END_CARD = r'''
import os, sys
with open(os.environ["FAKE_END_CARD_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[2:]) + "\n")
if os.environ.get("FAKE_END_CARD", "ok") == "ok":
    with open(sys.argv[1], "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + b"the end card")
    print("saved", sys.argv[1])
else:
    print("saved FAILED")
'''


def fixture_scenes():
    return json.loads(read_fixture("video", "scenes.json"))


def fixture_words():
    return json.loads(read_fixture("video", "words.json"))


def no_usage_error(message):
    raise AssertionError("usage error: " + message)


def channel(name):
    return scenes.load_channel(name, scenes.CHANNELS, no_usage_error)


def lines(found):
    return ["%s: %s %s" % (where, rule, message) for rule, where, message in found]


class TestLintRules(unittest.TestCase):
    def lint(self, change=None, words=None, **kw):
        data = fixture_scenes()
        if change:
            data = change(data) or data
        return lines(scenes.lint(data, words, **kw))

    def one(self, change, rule, where, text, words=None, **kw):
        """The change must give exactly one finding, of this rule, at this place, with this text in it."""
        found = self.lint(change, words, **kw)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("%s: %s " % (where, rule)), found[0])
        self.assertIn(text, found[0])

    def test_the_fixture_is_clean(self):
        self.assertEqual(self.lint(), [])
        self.assertEqual(self.lint(words=fixture_words(), count=(5, 8), channel=channel("chalkboard"), channel_name="chalkboard"), [])

    def test_e1_a_list_of_objects(self):
        self.assertEqual(lines(scenes.lint({"scenes": []})), ["scenes.json: E1 not a JSON list of objects"])
        self.assertEqual(lines(scenes.lint("scenes")), ["scenes.json: E1 not a JSON list of objects"])
        self.one(lambda d: d.insert(2, "a string"), "E1", "scene at position 3", "not an object")

    def test_e2_ids_run_1_to_n_in_order(self):
        def gap(d):
            d[2]["id"] = 7
        self.one(gap, "E2", "scene at position 3", "id is 7, expected 3")

        def repeat(d):
            d[3]["id"] = 3
        self.one(repeat, "E2", "scene at position 4", "id is 3, expected 4")

        def text_id(d):
            d[0]["id"] = "1"
        self.one(text_id, "E2", "scene at position 1", 'id is "1", expected 1')

        def no_id(d):
            del d[1]["id"]
        self.one(no_id, "E2", "scene at position 2", "id is null, expected 2")

        def swapped(d):
            d[0], d[1] = d[1], d[0]
        found = self.lint(swapped)
        self.assertEqual([f.split(": ")[0] for f in found if " E2 " in f], ["scene at position 1", "scene at position 2"])

    def test_e3_text_is_needed_except_on_an_end_card(self):
        def empty(d):
            d[1]["text"] = "  "
        self.one(empty, "E3", "scene 2", "text is missing or empty")

        def missing(d):
            del d[3]["text"]
        self.one(missing, "E3", "scene 4", "text is missing or empty")
        self.assertNotIn("text", fixture_scenes()[4], "the fixture's end card has no text, and that is allowed")

    def test_e4_prompt_is_ascii_and_restates_its_props(self):
        def empty(d):
            d[0]["prompt"] = ""
        self.one(empty, "E4", "scene 1", "prompt is missing or empty")

        def dash(d):
            d[2]["prompt"] = "The guide waves \u2014 slowly."
        self.one(dash, "E4", "scene 3", "U+2014 EM DASH at position 17")

        def lazy(d):
            d[3]["prompt"] = "Same as scene 2, but the trophy is bigger."
        self.one(lazy, "E4", "scene 4", "same as scene")

        def end_card_too(d):
            del d[4]["prompt"]
        self.one(end_card_too, "E4", "scene 5", "prompt is missing or empty")

    def test_e5_moves(self):
        def unknown(d):
            d[1]["move"] = "zoom"
        self.one(unknown, "E5", "scene 2", 'move is "zoom" (one of push_in, pull_out, pan_left, pan_right, drift_up, still)')

        def missing(d):
            del d[1]["move"]
        self.one(missing, "E5", "scene 2", "move is null")

        def twice(d):
            d[3]["move"] = "still"
        found = self.lint(twice)
        self.assertEqual(found, ["scene 4: E5 same move as scene 3 (still)", "scene 5: E5 same move as scene 4 (still)"])

    def test_e6_label_is_at_most_four_words(self):
        def long(d):
            d[1]["label"] = "IT STILL WINS EVERY TIME"
        self.one(long, "E6", "scene 2", 'label has 5 words, at most 4: "IT STILL WINS EVERY TIME"')

        def number(d):
            d[1]["label"] = 12
        self.one(number, "E6", "scene 2", "label is not a string")

        def degree(d):
            d[1]["label"] = "90\u00b0 TURN"
        self.one(degree, "E6", "scene 2", "U+00B0 DEGREE SIGN at position 3")

    def test_e7_times_join_up(self):
        def backwards(d):
            d[2]["end"] = 3.0  # scene 3 ends before it starts; scene 4 no longer starts at its end
        self.assertEqual(self.lint(backwards), ["scene 3: E7 end 3.0 is not after start 3.2", "scene 4: E7 starts at 4.4, but scene 3 ends at 3.0"])

        def gap(d):
            d[1]["start"] = 2.5
        self.one(gap, "E7", "scene 2", "starts at 2.5, but scene 1 ends at 2.0")

        def late(d):
            d[0]["start"] = 0.4
        self.one(late, "E7", "scene 1", "the first scene starts at 0.4, not 0")

        def text_time(d):
            d[3]["start"] = "4.4"
        self.one(text_time, "E7", "scene 4", "start and end are not both numbers")

        def half_timed(d):
            del d[3]["start"], d[3]["end"]
        self.one(half_timed, "E7", "scene 4", "start and end are not both numbers")

    def test_e7_is_not_checked_before_the_list_is_timed(self):
        def zeros(d):  # the planner writes 0 and 0; retiming fills them in
            for s in d:
                s["start"], s["end"] = 0, 0
        self.assertEqual(self.lint(zeros), [])

        def none(d):
            for s in d:
                del s["start"], s["end"]
        self.assertEqual(self.lint(none), [])

    def test_e8_the_scene_texts_are_the_narration(self):
        words = fixture_words()
        self.assertEqual(self.lint(words=words), [])

        def reworded(d):
            d[2]["text"] = "Here is how."
        self.one(reworded, "E8", "scene 3", 'the scene texts leave the narration at word 11: narration "why", scene text "how" '
                                            "(narration: ... It still wins. Here is why. Flip it over. ...)", words=words)

        def dropped(d):
            d[3]["text"] = "Flip it."
        self.one(dropped, "E8", "scenes.json", "the narration has 1 more word than the scene texts, from: over.", words=words)

        def added(d):
            d[3]["text"] = "Flip it over. Right now."
        self.one(added, "E8", "scene 4", "the scene texts have 2 more words than the narration, from: right now", words=words)

        def punctuation(d):  # case and punctuation are not words
            d[0]["text"] = "YOUR phone: is face-down?"
        found = self.lint(punctuation, words)
        self.assertEqual(len(found), 1, found)
        self.assertIn('narration "face", scene text "facedown"', found[0])
        self.assertEqual(self.lint(words=[{"word": "Your"}]), ["words.json: E8 not a list of {w, start, end} objects"])
        self.assertEqual(self.lint(words="not JSON"), ["words.json: E8 not a list of {w, start, end} objects"])
        self.assertEqual(scenes.tokens("It's 50% off, $5 -- \"now\"!"), ["its", "50%", "off", "$5", "now"])

    def test_e9_the_end_card_is_last_and_the_channel_has_one(self):
        def early(d):
            d[1]["end_card"] = True
        self.one(early, "E9", "scene 2", "an end card must be the last scene")
        self.one(None, "E9", "scene 5", "is an end card, but channel notebook has none",
                 channel=channel("notebook"), channel_name="notebook")

    def test_w1_about_one_scene_in_four_is_labelled(self):
        def none(d):
            del d[1]["label"]
        self.one(none, "W1", "scenes.json", "0 of 5 scenes have a label (0%)")

        def most(d):
            d[0]["label"] = "FACE DOWN"
            d[3]["label"] = "FLIP IT"
        self.one(most, "W1", "scenes.json", "3 of 5 scenes have a label (60%)")

    def test_w2_the_scene_count(self):
        self.one(None, "W2", "scenes.json", "5 scenes, expected 55 to 65", count=(55, 65))
        self.assertEqual(self.lint(count=(5, 5)), [])

    def test_w3_unknown_keys(self):
        def extra(d):
            d[0]["prompt_orig"] = "x"
            d[0]["notes"] = "y"
            d[0]["prompt_qc2"] = "an earlier prompt, kept by reject"
        self.one(extra, "W3", "scene 1", "unknown keys: notes, prompt_orig")


class TestLintCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = os.path.join(self.tmp.name, "video")
        shutil.copytree(fixture_path("video"), self.video)

    def write(self, data, name="scenes.json"):
        with open(os.path.join(self.video, name), "w", encoding="utf-8") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))

    def lint(self, *args):
        return run("scenes.py", "lint", self.video, *args, env={"AGENT_PLAYBOOK_TEST_HIDE_MODULES": "PIL,numpy"})

    def test_the_fixture_passes_even_strictly(self):
        for args in ([], ["--strict"], ["--strict", "--channel", "chalkboard", "--count", "5:8"]):
            r = self.lint(*args)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "ok: 5 scenes, 0 errors, 0 warnings\n", ""), args)
        before = sorted(os.listdir(self.video))
        self.lint()
        self.assertEqual(sorted(os.listdir(self.video)), before, "lint writes nothing")

    def test_errors_exit_1_with_one_line_each_and_a_count(self):
        data = fixture_scenes()
        data[3]["move"] = "still"          # E5 twice (scenes 4 and 5)
        data[2]["prompt"] = "The guide waves \u2013 slowly."   # E4
        data[0]["note"] = "x"              # W3
        self.write(data)
        r = self.lint()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["scene 1: W3 unknown key: note",
                                                 "scene 3: E4 prompt is ASCII only: it has U+2013 EN DASH at position 17",
                                                 "scene 4: E5 same move as scene 3 (still)",
                                                 "scene 5: E5 same move as scene 4 (still)"])
        self.assertEqual(r.stderr, "scenes.py: 3 errors, 1 warning\n")

    def test_warnings_pass_unless_strict(self):
        r = self.lint("--channel", "chalkboard")  # chalkboard expects 50 to 65 scenes
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "scenes.json: W2 5 scenes, expected 50 to 65\nok: 5 scenes, 0 errors, 1 warning\n")
        r = self.lint("--channel", "chalkboard", "--strict")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, "scenes.py: 0 errors, 1 warning\n")
        self.assertEqual(self.lint("--channel", "chalkboard", "--count", "5:8", "--strict").returncode, 0, "--count wins over the channel's range")

    def test_words_json_is_read_when_it_is_there(self):
        words = fixture_words()
        words[10]["w"] = "how."
        self.write(words, "words.json")
        r = self.lint()
        self.assertEqual(r.returncode, 1)
        self.assertIn("scene 3: E8 the scene texts leave the narration at word 11", r.stdout)
        self.write("[{", "words.json")
        r = self.lint()
        self.assertEqual(r.returncode, 1)
        self.assertIn("words.json: E8", r.stdout)
        os.remove(os.path.join(self.video, "words.json"))
        self.assertEqual(self.lint().returncode, 0, "no narration yet: the texts are not compared")

    def test_a_file_that_is_not_json_is_e1(self):
        self.write("[{\"id\": 1,")
        r = self.lint()
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("scenes.json: E1 not JSON: "), r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        self.write({"scenes": []})
        r = self.lint()
        self.assertEqual((r.returncode, r.stdout), (1, "scenes.json: E1 not a JSON list of objects\n"))

    def test_text_the_console_cannot_show_is_not_a_traceback(self):
        data = fixture_scenes()
        data[0]["text"] = "Your phone\u2019s face \u65e5\u672c down."
        first, second, third, fourth, last = data[0]["text"].split()
        data[0]["text"] = " ".join([first, fourth, second, third, last])  # the texts leave the narration at the word cp1252 can't show
        self.write(data)
        e = {"PYTHONIOENCODING": "cp1252"}
        r = run("scenes.py", "lint", self.video, env=e)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn('scene 1: E8 the scene texts leave the narration at word 2: narration "phone", scene text "??"', r.stdout)


class TestPromptParity(unittest.TestCase):
    """What is sent to the image model changes only when a channel file and its pinned prompt lines change together."""

    def test_every_channel_and_branch_builds_the_captured_prompts(self):
        shapes = json.loads(read_fixture("prompts", "scenes.json"))
        self.assertEqual([(bool(s.get("character")), bool(s.get("label"))) for s in shapes],
                         [(False, False), (False, True), (True, False), (True, True)], "one scene per branch of the builder")
        expected = set(["scenes.json"])
        for name in scenes.channel_names(scenes.CHANNELS):
            doc = channel(name)
            jobs = [(name + ".txt", None)] + [("%s.%s.txt" % (name, b), b) for b in sorted(doc.get("branches") or {})]
            for file, branch in jobs:
                expected.add(file)
                with self.subTest(file=file):
                    built = b"".join(scenes.build_prompt(s, doc, branch).encode("ascii") + b"\n" for s in shapes)
                    with open(fixture_path("prompts", file), "rb") as fh:
                        captured = fh.read()
                    self.assertEqual(built, captured, "%s: the prompt is no longer the captured one, byte for byte" % file)
        self.assertEqual(sorted(os.listdir(fixture_path("prompts"))), sorted(expected), "every channel file has its captured prompts")
        self.assertEqual(scenes.channel_names(scenes.CHANNELS), ["blueprint", "chalkboard", "notebook", "parchment"])

    def test_the_branch_colour_and_the_hands_sentence(self):
        doc = channel("blueprint")
        scene = {"id": 1, "prompt": "A beaker on a bench.", "character": True}
        default = scenes.build_prompt(scene, doc)
        self.assertEqual(default, scenes.build_prompt(scene, doc, "technology"))
        self.assertIn("filled with a soft sunflower-yellow glow", default)
        self.assertIn("filled with a soft coral-red glow", scenes.build_prompt(scene, doc, "science"))
        self.assertNotIn("{BRANCH}", default)
        self.assertTrue(default.endswith("Stick-figure hands stay simple (Pip: small open circle hands); no extra or merged limbs."))
        self.assertTrue(scenes.build_prompt(scene, channel("chalkboard")).endswith("Every hand has exactly five fingers; no extra or merged limbs."))

    def test_channel_files_say_what_the_flows_say(self):
        want = {"blueprint": ([55, 65], ["--pad-solid"], False), "chalkboard": ([50, 65], ["--pad-solid", "--pad-band", "10:30"], True),
                "notebook": ([55, 65], [], False), "parchment": ([55, 65], ["--pad-solid"], False)}
        for name, (count, flags, card) in want.items():
            doc = channel(name)
            self.assertEqual((doc["scene_count"], doc["render_flags"], bool(doc.get("end_card"))), (count, flags, card), name)
        self.assertEqual(channel("chalkboard")["end_card"], {"script": "end_card.py", "args": ["--chalk"]})

    def test_a_broken_channel_file_is_exit_1_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in (("notjson", "{"), ("nostyle", '{"character": "x"}'),
                               ("badcard", '{"style": "s", "character": "c", "end_card": {"script": "../outside.py"}}'),
                               ("badbranch", '{"style": "s", "character": "c", "branches": {"a": "red"}, "default_branch": "b"}'),
                               ("notascii", '{"style": "caf\u00e9", "character": "c"}')):
                with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as fh:
                    fh.write(text)
                r = run("scenes.py", "lint", fixture_path("video"), "--channel", name, "--channels-dir", d)
                self.assertEqual(r.returncode, 1, name + r.stdout + r.stderr)
                self.assertIn(name + ".json", r.stderr)
                self.assertNotIn("Traceback", r.stderr)


class GenBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.codex = Codex(d)
        quota_at(self.codex.home, 5.0)
        self.video = os.path.join(d, "video")
        shutil.copytree(fixture_path("video"), self.video)
        self.explainer = os.path.join(d, "explainer")
        for name in ("chalkboard", "notebook", "blueprint"):
            os.makedirs(os.path.join(self.explainer, name, "branding"))
            with open(self.sheet(name), "wb") as fh:
                fh.write(b"the %s character sheet" % name.encode("ascii"))
        with open(os.path.join(self.explainer, "blueprint", "branding", "character-sheet-science.png"), "wb") as fh:
            fh.write(b"the science sheet")
        with open(os.path.join(self.explainer, "chalkboard", "end_card.py"), "w", encoding="utf-8") as fh:
            fh.write(END_CARD)
        self.card_log = os.path.join(d, "end_card_runs.txt")

    def sheet(self, name):
        return os.path.join(self.explainer, name, "branding", "character-sheet.png")

    def gen(self, *args, channel="chalkboard", mode=None, **env):
        e = dict(self.codex.env, FAKE_END_CARD_LOG=self.card_log, **env)
        if mode:
            e["FAKE_CODEX_MODE"] = mode
        return run("scenes.py", "gen", self.video, "--channel", channel, "--explainer-dir", self.explainer, *args, env=e)

    def images(self):
        folder = os.path.join(self.video, "scenes")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def runs_log(self):
        with open(os.path.join(self.video, "qc", "gen.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def card_runs(self):
        if not os.path.exists(self.card_log):
            return []
        with open(self.card_log, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def more_scenes(self, total):
        """Lengthen the fixture to `total` drawn scenes plus the end card."""
        data = fixture_scenes()
        card = data.pop()
        moves = ["push_in", "pan_left", "pull_out", "pan_right"]
        while len(data) < total:
            n = len(data) + 1
            data.append({"id": n, "text": "More.", "prompt": "Extra scene number %d: a kettle on a stove." % n, "character": False, "move": moves[n % 4]})
        card["id"] = len(data) + 1
        data.append(card)
        with open(os.path.join(self.video, "scenes.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return data

    def have(self, *ids):
        os.makedirs(os.path.join(self.video, "scenes"), exist_ok=True)
        for i in ids:
            with open(os.path.join(self.video, "scenes", "%03d.png" % i), "wb") as fh:
                fh.write(b"an image made earlier")


class TestGenBeforeAnythingIsSpent(GenBase):
    """Paths that need neither Pillow nor a working codex."""

    def test_dry_run_prints_every_prompt_and_starts_nothing(self):
        r = self.gen("--dry-run", AGENT_PLAYBOOK_TEST_HIDE_MODULES="PIL,numpy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout.splitlines()
        self.assertEqual(out[0], "5 of 5 scenes to generate, 2 at a time")
        doc = channel("chalkboard")
        self.assertEqual(out[1:5], ["scene %03d: %s" % (s["id"], scenes.build_prompt(s, doc)) for s in fixture_scenes()[:4]])
        self.assertTrue(out[5].startswith("scene 005: end card: "), out[5])
        self.assertTrue(out[5].endswith(os.path.join(self.video, "scenes", "005.png") + " --chalk"), out[5])
        self.assertIn(os.path.join(self.explainer, "chalkboard", "end_card.py"), out[5])
        self.assertEqual(out[6], "reference: " + self.sheet("chalkboard"))
        self.assertEqual(len(out), 7)
        self.assertEqual((self.codex.runs(), self.card_runs()), ([], []))
        self.assertEqual(sorted(os.listdir(self.video)), ["scenes.json", "words.json"], "a dry run writes nothing")
        r = run("scenes.py", "gen", self.video, "--channel", "chalkboard", "--dry-run", "--only", "2,4",
                env={"AGENT_PLAYBOOK_EXPLAINER_DIR": self.explainer})  # no codex anywhere; the folder from the environment
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([line[:10] for line in r.stdout.splitlines()], ["2 of 5 sce", "scene 002:", "scene 004:", "reference:"])

    def test_the_branch_picks_the_colour_and_its_own_sheet(self):
        with open(os.path.join(self.video, "scenes.json"), "w", encoding="utf-8") as fh:
            json.dump(fixture_scenes()[:4], fh)
        r = self.gen("--dry-run", "--branch", "science", channel="blueprint")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("A single coral-red highlighter tint", r.stdout)
        self.assertEqual(r.stdout.splitlines()[-1], "reference: " + os.path.join(self.explainer, "blueprint", "branding", "character-sheet-science.png"))
        r = self.gen("--dry-run", channel="blueprint")  # the default branch has no sheet of its own here
        self.assertIn("A single sunflower-yellow highlighter tint", r.stdout)
        self.assertEqual(r.stdout.splitlines()[-1], "reference: " + self.sheet("blueprint"))

    def test_the_explainer_folder_is_found_above_the_video(self):
        inside = os.path.join(self.explainer, "chalkboard", "videos", "01-example")
        shutil.copytree(self.video, inside)
        r = run("scenes.py", "gen", inside, "--channel", "chalkboard", "--dry-run")  # no flag, nothing in the environment
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines()[-1], "reference: " + self.sheet("chalkboard"))
        r = run("scenes.py", "gen", self.video, "--channel", "chalkboard", "--dry-run")  # this copy is outside the explainer folder
        self.assertEqual(r.returncode, 2)
        self.assertIn("name the explainer-channel folder with --explainer-dir DIR or $AGENT_PLAYBOOK_EXPLAINER_DIR", r.stderr)
        other = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(os.path.join(other, "chalkboard", "branding"))
        r = run("scenes.py", "gen", inside, "--channel", "chalkboard", "--dry-run", "--explainer-dir", other)
        self.assertEqual(r.returncode, 1, "the flag wins over the folder above, even when it has no sheet")
        self.assertIn("no character sheet: " + os.path.join(other, "chalkboard", "branding", "character-sheet.png"), r.stderr)

    def test_pillow_missing_is_exit_3_before_anything_starts(self):
        r = self.gen(AGENT_PLAYBOOK_TEST_HIDE_MODULES="PIL")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("pip install Pillow", r.stderr)
        self.assertEqual((self.codex.runs(), self.card_runs(), self.images()), ([], [], []))

    def test_what_cannot_start_is_exit_1_before_codex(self):
        os.remove(self.sheet("chalkboard"))
        r = self.gen()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no character sheet: " + self.sheet("chalkboard"), r.stderr)
        r = self.gen("--only", "2,9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no scene 9 in ", r.stderr)
        data = fixture_scenes()
        data[1]["prompt"] = "A caf\u00e9 table."
        with open(os.path.join(self.video, "scenes.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        r = self.gen(channel="notebook")
        self.assertEqual(r.returncode, 1)
        self.assertIn("scene 2: the prompt is ASCII only: it has U+00E9 LATIN SMALL LETTER E WITH ACUTE at position 6", r.stderr)
        data[1]["prompt"], data[3]["id"] = "A table.", 2
        with open(os.path.join(self.video, "scenes.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        r = self.gen()
        self.assertEqual(r.returncode, 1)
        self.assertIn("the scene at position 4 has no id of its own (id 2); run scenes.py lint", r.stderr)
        with open(os.path.join(self.video, "scenes.json"), "w", encoding="utf-8") as fh:
            fh.write("[{")
        r = self.gen()
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not JSON", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual((self.codex.runs(), self.card_runs(), self.images()), ([], [], []))


@needs("Pillow")
class TestGen(GenBase):
    def test_every_scene_gets_an_image_and_the_run_is_recorded(self):
        r = self.gen()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "5 of 5 scenes to generate, 2 at a time")
        self.assertEqual(r.stdout.splitlines()[-1], "5 scenes ok")
        self.assertEqual(self.images(), ["001.png", "001.txt", "002.png", "002.txt", "003.png", "003.txt", "004.png", "004.txt", "005.png"])
        doc = channel("chalkboard")
        with open(os.path.join(self.video, "scenes", "002.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), scenes.build_prompt(fixture_scenes()[1], doc) + "\n")
        with open(os.path.join(self.video, "scenes", "005.png"), "rb") as fh:
            self.assertEqual(fh.read(), b"\x89PNG\r\n\x1a\n" + b"the end card", "the end card is drawn by the channel's script, not by codex")
        self.assertEqual(len(self.card_runs()), 1)
        self.assertTrue(self.card_runs()[0].endswith("--chalk"))
        runs = self.codex.runs()
        self.assertEqual(len(runs), 4)
        self.assertEqual(sorted(x["prompt"] for x in runs),
                         sorted("Use your image generation tool to create exactly one image. %s Do not write any code or files yourself; "
                                "just generate the image and reply 'done'." % scenes.build_prompt(s, doc) for s in fixture_scenes()[:4]))
        for x in runs:
            self.assertEqual(x["argv"][5:], ["-i", self.sheet("chalkboard"), "-"])
        log = self.runs_log()
        self.assertEqual(len(log), 1)
        import mcommon
        self.assertEqual((log[0]["channel"], log[0]["branch"], log[0]["only"], log[0]["pace"], log[0]["quota_used_percent"]),
                         ("chalkboard", None, None, False, 5.0))
        self.assertEqual(log[0]["channel_sha256"], mcommon.sha256(os.path.join(scenes.CHANNELS, "chalkboard.json")))
        self.assertEqual((log[0]["reference"], log[0]["reference_sha256"]), (self.sheet("chalkboard"), mcommon.sha256(self.sheet("chalkboard"))))
        self.assertEqual([(s["id"], s["status"], s["tries"], s["error"]) for s in log[0]["scenes"]],
                         [(i, "ok", 1, None) for i in (1, 2, 3, 4, 5)])
        self.assertTrue(log[0]["scenes"][4]["end_card"])
        # a second run has nothing to do, and says so with exit 0
        r = self.gen()
        self.assertEqual((r.returncode, r.stdout), (0, "0 of 5 scenes to generate\n"))
        self.assertEqual(len(self.codex.runs()), 4)
        self.assertEqual(len(self.runs_log()), 1)

    def test_a_failed_scene_is_exit_1_and_named(self):
        r = self.gen("--backoff", "0", FAKE_CODEX_FAIL_ON="stack of books")  # scene 3's prompt
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "scenes.py: 1 of 5 scenes failed: 3 (%s has the reasons)\n" % os.path.join("qc", "gen.json"))
        self.assertIn("scene 003 FAILED: try 1: no image produced for this run's thread", r.stdout)
        self.assertEqual([n for n in self.images() if n.endswith(".png")], ["001.png", "002.png", "004.png", "005.png"])
        record = dict((s["id"], s) for s in self.runs_log()[0]["scenes"])
        self.assertEqual((record[3]["status"], record[3]["tries"]), ("failed", 3))
        self.assertEqual(record[3]["error"].count("no image produced for this run's thread"), 3)
        self.assertEqual([record[i]["status"] for i in (1, 2, 4, 5)], ["ok"] * 4)
        self.assertEqual(len(self.codex.runs()), 3 + 3)
        # the second run generates only the missing scene
        r = self.gen()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "1 of 5 scenes to generate, 2 at a time")
        self.assertEqual(len(self.codex.runs()), 3 + 3 + 1)
        self.assertIn("003.png", self.images())
        log = self.runs_log()
        self.assertEqual(len(log), 2, "each run adds a record")
        self.assertEqual([(s["id"], s["status"]) for s in log[1]["scenes"]], [(3, "ok")])

    def test_only_regenerates_a_scene_that_has_an_image(self):
        self.have(1, 2, 3, 4, 5)
        r = self.gen("--only", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.codex.runs()), 1)
        self.assertIn("stack of books", self.codex.runs()[0]["prompt"])
        with open(os.path.join(self.video, "scenes", "003.png"), "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")
        for i in (1, 2, 4, 5):
            with open(os.path.join(self.video, "scenes", "%03d.png" % i), "rb") as fh:
                self.assertEqual(fh.read(), b"an image made earlier", "scene %d was not asked for" % i)
        self.assertEqual(self.runs_log()[0]["only"], [3])

    def test_only_fails_when_the_image_was_not_rewritten(self):
        self.have(1, 2, 3, 4, 5)
        r = self.gen("--only", "3,4", "--backoff", "0", "--tries", "2", mode="crash")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("2 of 2 scenes failed: 3, 4", r.stderr)
        with open(os.path.join(self.video, "scenes", "003.png"), "rb") as fh:
            self.assertEqual(fh.read(), b"an image made earlier", "the old image stays, but it does not count as done")
        self.assertEqual(len(self.codex.runs()), 4)

    def test_an_image_that_is_not_a_picture_of_anything_does_not_count(self):
        r = self.gen("--only", "1", "--tries", "2", mode="black")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("1 of 1 scene failed: 1", r.stderr)
        self.assertEqual(self.images(), ["001.rejected-1.png", "001.rejected-2.png"])
        self.assertIn("one flat colour", self.runs_log()[0]["scenes"][0]["error"])

    def test_a_bulk_run_is_refused_when_the_quota_is_too_high(self):
        self.more_scenes(6)
        quota_at(self.codex.home, 85.0, minutes_ago=1, name="newer.jsonl")
        r = self.gen()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("85.0% of the weekly Codex limit is used (limit for a bulk run: 80%): generate nothing in bulk; "
                      "mark unfixed scenes pending-codex", r.stderr)
        self.assertEqual((self.codex.runs(), self.card_runs(), self.images()), ([], [], []), "something was started")
        self.assertFalse(os.path.exists(os.path.join(self.video, "qc")))
        # with 2 scenes to do it is not a bulk run: allowed up to 95 percent
        self.have(1, 2, 3, 4)
        r = self.gen()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.codex.runs()), 2)
        self.assertEqual(self.runs_log()[0]["quota_used_percent"], 85.0)

    def test_the_bulk_limit_is_a_flag_and_small_runs_stop_at_95(self):
        self.more_scenes(6)
        quota_at(self.codex.home, 85.0, minutes_ago=2, name="newer.jsonl")
        r = self.gen("--max-used", "90")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.codex.runs()), 6)
        quota_at(self.codex.home, 96.0, minutes_ago=1, name="newest.jsonl")
        r = self.gen("--only", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("96.0% of the weekly Codex limit is used (limit for a single image: 95%)", r.stderr)
        self.assertEqual(len(self.codex.runs()), 6)
        r = self.gen("--only", "1", "--no-quota")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.codex.runs()), 7)

    def test_no_quota_reading_refuses_the_run(self):
        shutil.rmtree(os.path.join(self.codex.home, "sessions"))
        r = self.gen()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no Codex sessions folder", r.stderr)
        self.assertEqual((self.codex.runs(), self.card_runs(), self.images()), ([], [], []))

    def test_an_end_card_that_did_not_render_is_exit_1(self):
        r = self.gen(FAKE_END_CARD="broken")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("1 of 5 scenes failed: 5", r.stderr)
        self.assertIn("scene 005 FAILED: the end card script wrote no PNG (saved FAILED)", r.stdout)
        self.assertEqual([n for n in self.images() if n.endswith(".png")], ["001.png", "002.png", "003.png", "004.png"])
        card = self.runs_log()[0]["scenes"][-1]
        self.assertEqual((card["id"], card["status"], card["end_card"]), (5, "failed", True))
        r = self.gen()  # only the end card is left, so codex and the quota are not needed
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "1 of 5 scenes to generate")
        self.assertIn("005.png", self.images())

    def test_an_end_card_on_a_channel_without_one_is_exit_1(self):
        r = self.gen(channel="notebook")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("1 of 5 scenes failed: 5", r.stderr)
        self.assertIn("channel notebook has no end card", r.stdout)
        os.remove(os.path.join(self.explainer, "chalkboard", "end_card.py"))
        r = self.gen("--only", "5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no end card script: ", r.stdout)

    def test_pace_backs_off_and_tries_again(self):
        r = self.gen("--only", "2", "--pace", "--backoff", "0", FAKE_CODEX_FAIL_FIRST="2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "1 of 5 scenes to generate, one at a time")
        self.assertEqual(len(self.codex.runs()), 3)
        record = self.runs_log()[0]
        self.assertTrue(record["pace"])
        self.assertEqual((record["scenes"][0]["status"], record["scenes"][0]["tries"]), ("ok", 3))
        self.assertEqual(record["scenes"][0]["error"].count("no image produced"), 2, "the failed tries are kept in the record")
        r = self.gen("--only", "2", "--pace", "--backoff", "0", mode="crash")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.codex.runs()), 3 + 6, "six tries per scene with --pace")

    def test_pace_waits_double_up_to_fifteen_minutes(self):
        import image_gen
        waits = []
        real = image_gen.codex_img.generate

        def never(prompt, out, refs, timeout, cmd):
            raise image_gen.codex_img.CodexError("usage limit reached")
        image_gen.codex_img.generate = never
        try:
            got = image_gen.make("p", os.path.join(self.tmp.name, "x.png"), tries=scenes.PACE_TRIES, sleep=waits.append,
                                 wait=lambda n: min(scenes.PACE_MAX_WAIT, scenes.PACE_BACKOFF * 2 ** (n - 1)))
        finally:
            image_gen.codex_img.generate = real
        self.assertEqual(waits, [600, 900, 900, 900, 900])
        self.assertEqual((got["accepted"], got["tries"], len(got["rejected"])), (False, 6, 6))

    def test_codex_missing_is_exit_1(self):
        r = run("scenes.py", "gen", self.video, "--channel", "chalkboard", "--explainer-dir", self.explainer,
                env={"CODEX_HOME": self.codex.home})
        self.assertEqual(r.returncode, 1)
        self.assertIn("codex not found", r.stderr)
        self.assertEqual(self.images(), [])

    def test_an_unreadable_run_log_is_kept_aside(self):
        os.makedirs(os.path.join(self.video, "qc"))
        with open(os.path.join(self.video, "qc", "gen.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        r = self.gen("--only", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(os.listdir(os.path.join(self.video, "qc"))), ["gen.json", "gen.unreadable.json"])
        self.assertEqual(len(self.runs_log()), 1)


class TestEndCardTimeout(unittest.TestCase):
    def test_a_hung_end_card_script_is_ended_with_what_it_started(self):
        # an end-card script that renders through Chrome leaves the browser running if only the script is killed
        import time
        from test_scripts import SPAWN_AND_HANG, ended, read_pid
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "chalkboard"))
            with open(os.path.join(d, "chalkboard", "end_card.py"), "w", encoding="utf-8") as fh:
                fh.write(SPAWN_AND_HANG)
            pid_file = os.path.join(d, "pid.txt")
            saved = scenes.END_CARD_TIMEOUT, os.environ.get("TREE_PID_FILE")
            scenes.END_CARD_TIMEOUT = 3
            os.environ["TREE_PID_FILE"] = pid_file
            try:
                t0 = time.time()
                why = scenes.render_end_card({"end_card": {"script": "end_card.py"}}, "chalkboard", d, os.path.join(d, "005.png"))
            finally:
                scenes.END_CARD_TIMEOUT = saved[0]
                os.environ.pop("TREE_PID_FILE")
                if saved[1] is not None:
                    os.environ["TREE_PID_FILE"] = saved[1]
            self.assertEqual(why, "the end card script did not finish in 3 s")
            self.assertLess(time.time() - t0, 30)
            self.assertTrue(ended(read_pid(pid_file)), "what the end-card script started outlived the timeout")
            self.assertFalse(os.path.exists(os.path.join(d, "005.png")))


if __name__ == "__main__":
    unittest.main()
