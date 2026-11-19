"""Offline tests for media/image_gen.py against a stand-in codex: no image generation, no network.

The stand-in (mediatest.FAKE_CODEX) draws a PNG with Pillow into its own thread folder, the way the real one
leaves its image, and records every start, so a test can assert that nothing was started.
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediatest import Codex, needs, quota_at, run  # noqa: E402

import image_gen  # noqa: E402  (mediatest put agent-playbook/media on sys.path)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.codex = Codex(self.tmp.name)
        quota_at(self.codex.home, 5.0)
        self.folder = os.path.join(self.tmp.name, "art")
        self.out = os.path.join(self.folder, "key.png")

    def gen(self, *args, mode=None, **env):
        e = dict(self.codex.env, **env)
        if mode:
            e["FAKE_CODEX_MODE"] = mode
        return run("image_gen.py", *args, env=e)

    def files(self):
        return sorted(os.listdir(self.folder)) if os.path.isdir(self.folder) else []

    def sidecar(self):
        with open(self.out[:-4] + ".gen.json", encoding="utf-8") as fh:
            return json.load(fh)


class TestBeforeAnythingIsSpent(Base):
    """Paths that need neither Pillow nor a working codex."""

    def test_dry_run_prints_the_prompt_and_starts_nothing(self):
        ref = os.path.join(self.tmp.name, "sheet.png")
        with open(ref, "wb") as fh:
            fh.write(b"ref")
        r = self.gen("A blue sketch of a kettle", self.out, "--ref", ref, "--dry-run", "--aspect", "16:9",
                     AGENT_PLAYBOOK_TEST_HIDE_MODULES="PIL,numpy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "prompt: Use your image generation tool to create exactly one image. A blue sketch of a kettle"
                                   " Do not write any code or files yourself; just generate the image and reply 'done'.")
        self.assertEqual(lines[1], "reference: " + os.path.realpath(ref))
        self.assertTrue(lines[2].startswith("command: "), lines[2])
        self.assertIn("exec --json --skip-git-repo-check --sandbox workspace-write -i " + os.path.realpath(ref) + " -", lines[2])
        self.assertEqual(self.codex.runs(), [])
        self.assertEqual(self.files(), [])
        # and with no codex installed at all
        r = run("image_gen.py", "A blue sketch", self.out, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_non_ascii_prompt_is_a_usage_error_naming_the_character(self):
        r = self.gen("A sign that says \u201copen\u201d", self.out)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("U+201C LEFT DOUBLE QUOTATION MARK at position 18", r.stderr)
        self.assertEqual(self.codex.runs(), [])
        self.assertEqual(image_gen.non_ascii("plain ASCII ~ text"), "")
        self.assertEqual(image_gen.non_ascii("5 \u2013 6"), "U+2013 EN DASH at position 3")

    def test_pillow_missing_is_exit_3_before_anything_starts(self):
        r = self.gen("prompt", self.out, AGENT_PLAYBOOK_TEST_HIDE_MODULES="PIL")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("pip install Pillow", r.stderr)
        self.assertEqual(self.codex.runs(), [])
        self.assertEqual(self.files(), [])


@needs("Pillow")
class TestImageGen(Base):
    def test_a_16_9_image_is_accepted(self):
        ref = os.path.join(self.tmp.name, "sheet.png")
        with open(ref, "wb") as fh:
            fh.write(b"ref")
        r = self.gen("A blue sketch of a kettle", self.out, "--aspect", "16:9", "--ref", ref, mode="wide")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"^saved .*key\.png 1536x864 in \d+ s, try 1\n$")
        self.assertEqual(self.files(), ["key.gen.json", "key.png", "key.txt"])
        with open(self.out[:-4] + ".txt", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "A blue sketch of a kettle\n")
        got = self.sidecar()
        self.assertEqual(sorted(got), ["accepted", "prompt_sha256", "quota_used_percent", "refs", "rejected", "seconds", "size", "tries"])
        self.assertEqual(got["prompt_sha256"], hashlib.sha256(b"A blue sketch of a kettle").hexdigest())
        self.assertEqual(got["refs"], [{"file": os.path.realpath(ref), "sha256": hashlib.sha256(b"ref").hexdigest()}])
        self.assertEqual((got["accepted"], got["tries"], got["rejected"], got["size"], got["quota_used_percent"]),
                         (True, 1, [], [1536, 864], 5.0))
        runs = self.codex.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["argv"], ["exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write", "-i", os.path.realpath(ref), "-"])
        self.assertIn("A blue sketch of a kettle", runs[0]["prompt"])

    def test_the_wrong_shape_is_tried_three_times_then_fails(self):
        r = self.gen("prompt", self.out, "--aspect", "16:9", mode="3x2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no accepted image after 3 tries: try 3: aspect 1.50, wanted 1.78", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.files(), ["key.gen.json", "key.rejected-1.png", "key.rejected-2.png", "key.rejected-3.png"])
        got = self.sidecar()
        self.assertEqual((got["accepted"], got["tries"], got["size"]), (False, 3, None))
        self.assertEqual(got["rejected"], ["try %d: aspect 1.50, wanted 1.78" % n for n in (1, 2, 3)])
        self.assertEqual(len(self.codex.runs()), 3)
        # the same picture is fine when 3:2 is what was asked for, or when no shape was
        for aspect in ("3:2", "any"):
            r = self.gen("prompt", os.path.join(self.folder, aspect.replace(":", "x") + ".png"), "--aspect", aspect, mode="3x2")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_all_black_image_is_rejected_as_flat(self):
        r = self.gen("prompt", self.out, "--tries", "1", mode="black")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no accepted image after 1 try: try 1: one flat colour", r.stderr)
        self.assertEqual(self.files(), ["key.gen.json", "key.rejected-1.png"])

    def test_alpha_needs_real_transparency(self):
        r = self.gen("prompt", self.out, "--alpha", "--tries", "2", mode="checker")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("try 2: opaque image with a painted checkerboard", r.stderr)
        self.assertNotIn("key.png", self.files())
        r = self.gen("prompt", self.out, "--alpha", mode="3x2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no alpha channel", r.stderr)
        r = self.gen("prompt", self.out, "--alpha", mode="alpha")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("key.png", self.files())
        self.assertEqual(self.gen("prompt", os.path.join(self.folder, "plain.png"), mode="checker").returncode, 0, "without --alpha a checkerboard is a picture")

    def test_a_small_image_is_rejected(self):
        r = self.gen("prompt", self.out, "--tries", "1", mode="small")
        self.assertEqual(r.returncode, 1)
        self.assertIn("320x180, shorter side under 800", r.stderr)
        r = self.gen("prompt", self.out, "--min-side", "180", mode="small")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_quota_too_high_never_starts_codex(self):
        quota_at(self.codex.home, 96.0, minutes_ago=1, name="newer.jsonl")
        r = self.gen("prompt", self.out)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("96.0% of the weekly Codex limit is used (limit for a single image: 95%)", r.stderr)
        self.assertEqual(self.codex.runs(), [], "the stand-in was started")
        self.assertEqual(self.files(), [])
        r = self.gen("prompt", self.out, "--max-used", "97")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sidecar()["quota_used_percent"], 96.0)
        r = self.gen("prompt", os.path.join(self.folder, "second.png"), "--no-quota")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_no_quota_reading_never_starts_codex(self):
        other = Codex(os.path.join(self.tmp.name))  # the same stand-in, pointed at a Codex home with no sessions
        empty = os.path.join(self.tmp.name, "empty-home")
        r = run("image_gen.py", "prompt", self.out, env=dict(other.env, CODEX_HOME=empty))
        self.assertEqual(r.returncode, 1)
        self.assertIn("no Codex sessions folder", r.stderr)
        self.assertEqual(other.runs(), [])
        r = run("image_gen.py", "prompt", self.out, "--no-quota", env=dict(other.env, CODEX_HOME=empty))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.out[:-4] + ".gen.json", encoding="utf-8") as fh:
            self.assertIsNone(json.load(fh)["quota_used_percent"])

    def test_a_run_that_makes_no_image_is_tried_again(self):
        r = self.gen("prompt", self.out, "--wait", "0", mode="crash")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no accepted image after 3 tries: try 3: no image produced for this run's thread", r.stderr)
        self.assertEqual(len(self.codex.runs()), 3)
        self.assertEqual(self.files(), ["key.gen.json"], "nothing was made, so nothing is kept")
        r = self.gen("prompt", self.out, "--wait", "0", FAKE_CODEX_FAIL_FIRST="4")  # the 4th start fails, the 5th works
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = self.sidecar()
        self.assertEqual((got["accepted"], got["tries"], len(got["rejected"])), (True, 2, 1))

    def test_an_image_already_there_stays_until_an_accepted_one_replaces_it(self):
        os.makedirs(self.folder)
        with open(self.out, "wb") as fh:
            fh.write(b"the old image")
        with open(os.path.join(self.folder, "key.rejected-1.png"), "wb") as fh:
            fh.write(b"an earlier reject")
        r = self.gen("prompt", self.out, "--tries", "1", mode="black")
        self.assertEqual(r.returncode, 1)
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read(), b"the old image")
        with open(os.path.join(self.folder, "key.rejected-1.png"), "rb") as fh:
            self.assertEqual(fh.read(), b"an earlier reject", "a kept reject is never overwritten")
        self.assertIn("key.rejected-2.png", self.files())
        self.assertNotIn("key.part.png", self.files())
        r = self.gen("prompt", self.out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")

    def test_codex_missing_is_exit_1(self):
        r = run("image_gen.py", "prompt", self.out, env={"CODEX_HOME": self.codex.home})
        self.assertEqual(r.returncode, 1)
        self.assertIn("codex not found", r.stderr)
        self.assertIn("install the Codex CLI or pass --codex PATH", r.stderr)
        self.assertEqual(self.files(), [])
        r = run("image_gen.py", "prompt", self.out, "--codex", self.codex.env["CODEX_BIN"],
                env={"CODEX_HOME": self.codex.home, "FAKE_CODEX_LOG": self.codex.log})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_problem_names_what_is_wrong_with_a_file(self):
        from PIL import Image
        os.makedirs(self.folder)
        junk = os.path.join(self.folder, "junk.png")
        with open(junk, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"this is not a picture")
        self.assertTrue(image_gen.problem(junk).startswith("not a readable image"), image_gen.problem(junk))
        tall = os.path.join(self.folder, "tall.png")
        im = Image.new("RGB", (900, 1600), (250, 250, 244))
        im.paste((30, 60, 160), (100, 100, 500, 700))
        im.save(tall)
        self.assertEqual(image_gen.problem(tall, image_gen.ASPECTS["9:16"]), "")
        self.assertEqual(image_gen.problem(tall, image_gen.ASPECTS["16:9"]), "aspect 0.56, wanted 1.78")
        self.assertEqual(image_gen.problem(tall, image_gen.ASPECTS["9:16"], min_side=1000), "900x1600, shorter side under 1000")
        nearly = os.path.join(self.folder, "nearly.png")
        im.resize((1536, 900)).save(nearly)  # 1.71: within 5 percent of 16:9
        self.assertEqual(image_gen.problem(nearly, image_gen.ASPECTS["16:9"]), "")


if __name__ == "__main__":
    unittest.main()
