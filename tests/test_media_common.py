"""Offline tests for agent-playbook/media: what every media tool must do, and the shared helpers in mcommon.py.

No image generation, no network, no browser. Images are drawn here (nothing binary is in the repository);
ffmpeg is a stand-in except in the one test that needs the real one. Tests that need Pillow, numpy or ffmpeg
skip without them, or fail when AGENT_PLAYBOOK_REQUIRE_MEDIA=1.
"""
import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_scripts  # noqa: E402
from mediatest import HAVE, MEDIA, needs, run  # noqa: E402
from test_scripts import GUARD, executable  # noqa: E402

import mcommon  # noqa: E402  (mediatest put agent-playbook/media on sys.path)


def media_scripts():
    return sorted(n for n in os.listdir(MEDIA) if n.endswith(".py") and n != "mcommon.py")


def exits(fn, *args, **kwargs):
    """Call a helper that is expected to exit. Returns (exit code, what it wrote on stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            fn(*args, **kwargs)
        except SystemExit as e:
            return e.code, err.getvalue()
    raise AssertionError("did not exit; stderr: " + err.getvalue())


@contextlib.contextmanager
def environ(**changes):
    saved = dict((k, os.environ.get(k)) for k in changes)
    try:
        for k, v in changes.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


class TestEveryMediaScript(unittest.TestCase):
    def test_all_compile_and_parse_as_python_3_8(self):
        import py_compile
        for name in sorted(os.listdir(MEDIA)):
            if name.endswith(".py"):
                py_compile.compile(os.path.join(MEDIA, name), doraise=True)
                with open(os.path.join(MEDIA, name), encoding="utf-8") as fh:
                    try:
                        ast.parse(fh.read(), name, feature_version=(3, 8))
                    except SyntaxError as e:
                        self.fail("%s is not Python 3.8 syntax: %s" % (name, e))

    def test_help_works_with_no_pillow_numpy_or_ffmpeg(self):
        # the guard hides the modules; PATH is an empty folder, so no ffmpeg, codex or Chrome can be found
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "net.txt")
            empty = os.path.join(d, "empty-path")
            os.mkdir(empty)
            work = os.path.join(d, "cwd")
            os.mkdir(work)
            for name in media_scripts():
                with self.subTest(script=name):
                    r = run(name, "--help", cwd=work, marker=marker,
                            env={"PATH": empty, "AGENT_PLAYBOOK_TEST_HIDE_MODULES": "PIL,numpy"})
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertTrue(r.stdout.startswith("usage: " + name), r.stdout[:200])
                    self.assertEqual(r.stderr, "")
                    self.assertFalse(os.path.exists(marker), "%s --help made a network call" % name)
                    self.assertEqual(os.listdir(work), [], "%s --help wrote files" % name)
            for args in (["lint", "--help"], ["gen", "--help"]):
                r = run("scenes.py", *args, cwd=work, env={"PATH": empty, "AGENT_PLAYBOOK_TEST_HIDE_MODULES": "PIL,numpy"})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(r.stdout.startswith("usage: scenes.py " + args[0]), r.stdout[:200])

    def test_help_works_in_isolated_mode(self):
        # python -I leaves the script's folder off sys.path; each tool puts its own folder and ../scripts back
        for name in media_scripts():
            with self.subTest(script=name):
                r = subprocess.run([sys.executable, "-I", os.path.join(MEDIA, name), "--help"],
                                   capture_output=True, text=True, encoding="utf-8", timeout=120,
                                   env=dict(os.environ, AGENT_PLAYBOOK_OFFLINE="1"))
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_usage_errors_exit_2(self):
        cases = {
            "image_gen.py": [[], ["only a prompt"], ["  ", "out.png"], ["p", "out.jpg"], ["p", "out.png", "--aspect", "4:3"],
                             ["p", "out.png", "--tries", "0"], ["p", "out.png", "--timeout", "soon"],
                             ["p", "out.png", "--max-used", "0"], ["p", "out.png", "--min-side", "0"], ["p", "out.png", "--wait", "-1"]],
            "quota.py": [["extra"], ["--max-used", "lots"], ["--max-used", "0"], ["--max-used", "101"], ["--max-age-hours", "0"]],
            "scenes.py": [[], ["render", "video"], ["lint"], ["lint", "video", "--count", "many"], ["lint", "video", "--count", "65:55"],
                          ["lint", "video", "--channel", "no-such-channel"], ["gen", "video"], ["gen", "video", "--channel", "no-such-channel"],
                          ["gen", "video", "--channel", "chalkboard", "--only", "three"], ["gen", "video", "--channel", "chalkboard", "--only", "0"],
                          ["gen", "video", "--channel", "chalkboard", "--branch", "science"],
                          ["gen", "video", "--channel", "blueprint", "--branch", "history"],
                          ["gen", "video", "--channel", "chalkboard", "--workers", "0"], ["gen", "video", "--channel", "chalkboard", "--tries", "0"],
                          ["gen", "video", "--channel", "chalkboard", "--max-used", "0"], ["gen", "video", "--channel", "chalkboard", "--backoff", "-5"],
                          ["gen", ".", "--channel", "chalkboard"]],  # no --explainer-dir, no $AGENT_PLAYBOOK_EXPLAINER_DIR, none above
        }
        self.assertEqual(sorted(cases), media_scripts(), "every media script needs a usage-error case")
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "net.txt")
            work = os.path.join(d, "cwd")
            os.mkdir(work)
            for name, arg_lists in sorted(cases.items()):
                for args in arg_lists:
                    with self.subTest(script=name, args=args):
                        r = run(name, *args, cwd=work, marker=marker)
                        self.assertEqual(r.returncode, 2, "%s %s\n%s%s" % (name, args, r.stdout, r.stderr))
                        self.assertNotIn("Traceback", r.stderr)
                        self.assertTrue(r.stderr.strip(), "a usage error says why")
            self.assertFalse(os.path.exists(marker), "a usage error made a network call")
            self.assertEqual(os.listdir(work), [], "a usage error wrote files")

    def test_unknown_channel_lists_the_channels(self):
        r = run("scenes.py", "gen", "video", "--channel", "chalk")
        self.assertEqual(r.returncode, 2)
        self.assertIn("blueprint, chalkboard, notebook, parchment", r.stderr)

    def test_missing_input_files_exit_1_without_traceback(self):
        cases = [("image_gen.py", ["a prompt", "out.png", "--ref", "nope.png"]),
                 ("scenes.py", ["lint", "nope"]),
                 ("scenes.py", ["gen", "nope", "--channel", "chalkboard", "--explainer-dir", "nope"]),
                 ("quota.py", ["--codex-home", "nope"])]
        with tempfile.TemporaryDirectory() as d:
            for name, args in cases:
                with self.subTest(script=name, args=args):
                    r = run(name, *args, cwd=d)
                    self.assertEqual(r.returncode, 1, "%s %s\n%s%s" % (name, args, r.stdout, r.stderr))
                    self.assertNotIn("Traceback", r.stderr)
                    self.assertIn(name + ":", r.stderr)
            self.assertEqual(os.listdir(d), [], "a failed run left files behind")

    def test_no_user_or_scratchpad_paths_in_media(self):
        bad = re.compile(r"[A-Za-z]:[\\/]+Users|/Users/[A-Za-z]|/home/[a-z]|scratchpad|AppData[\\/]+Local[\\/]+Temp", re.I)
        for folder, _dirs, names in os.walk(MEDIA):
            for name in sorted(names):
                if name.endswith((".py", ".json")):
                    with open(os.path.join(folder, name), encoding="utf-8") as fh:
                        for n, line in enumerate(fh, 1):
                            self.assertIsNone(bad.search(line), "%s:%d holds a machine path: %s" % (name, n, line.strip()[:120]))

    def test_index_is_current(self):
        r = test_scripts.run("make_index.py", "--dir", MEDIA, "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(MEDIA, "INDEX.json"), encoding="utf-8") as fh:
            index = json.load(fh)
        self.assertEqual([s["name"] for s in index["scripts"]], media_scripts())
        self.assertEqual([m["name"] for m in index["modules"]], ["mcommon.py"])
        for s in index["scripts"]:
            self.assertTrue(s["purpose"] and s["usage"].startswith(s["name"]), s)
            self.assertTrue(set(s["side_effects"]) <= {"none", "clipboard", "network", "files", "browser"}, s)

    def test_make_index_dir_writes_beside_the_scripts_it_reads(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "hello.py"), "w", encoding="utf-8") as fh:
                fh.write('"""Say hello.\n\nMore.\n"""\nimport argparse\nNEEDS = []\nEFFECTS = ["none"]\n\n\n'
                         'def build_parser():\n    return argparse.ArgumentParser(prog="hello.py", usage="hello.py NAME", description="Say hello.")\n')
            r = test_scripts.run("make_index.py", "--dir", d, "--check")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("INDEX.json is missing", r.stderr)
            r = test_scripts.run("make_index.py", "--dir", d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with open(os.path.join(d, "INDEX.json"), encoding="utf-8") as fh:
                index = json.load(fh)
            self.assertEqual([(s["name"], s["usage"]) for s in index["scripts"]], [("hello.py", "hello.py NAME")])
            self.assertEqual(index["modules"], [])
            self.assertEqual(test_scripts.run("make_index.py", "--dir", d, "--check").returncode, 0)
            r = test_scripts.run("make_index.py", "--dir", os.path.join(d, "missing"))
            self.assertEqual(r.returncode, 1)
            self.assertIn("no such folder", r.stderr)


class TestFfmpegHelpers(unittest.TestCase):
    def test_run_ff_reports_the_end_of_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "fake_ffmpeg.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write("import sys\nsys.stderr.write('x' * 3000 + '\\nOutput file #0 does not contain any stream\\n')\nsys.exit(3)\n")
            fake = executable(d, "ffmpeg", python_script=script)
            code, err = exits(mcommon.run_ff, [fake, "-i", "in.mp4", "out.mp4"], "encode out.mp4")
            self.assertEqual(code, 1)
            self.assertIn("encode out.mp4: ", err)
            self.assertTrue(err.rstrip().endswith("Output file #0 does not contain any stream"), err[-200:])
            self.assertLess(len(err), 1700, "only the last 1,500 characters of stderr are shown")
            ok = executable(d, "ffmpeg-ok")
            self.assertEqual(mcommon.run_ff([ok], "noop").returncode, 0)
            code, err = exits(mcommon.run_ff, [os.path.join(d, "not-here")], "encode out.mp4")
            self.assertEqual(code, 1)
            self.assertIn("can't start", err)

    def test_a_hung_ffmpeg_is_ended_at_the_timeout_with_what_it_started(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "hang.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(test_scripts.SPAWN_AND_HANG)
            fake = executable(d, "ffprobe", python_script=script)  # through a launcher, so there is a tree to end
            pid_file = os.path.join(d, "pid.txt")
            t0 = time.time()
            with environ(TREE_PID_FILE=pid_file):
                code, err = exits(mcommon.run_ff, [fake, "clip.mp4"], "ffprobe clip.mp4", timeout=3)
            self.assertEqual(code, 1)
            self.assertIn("ffprobe clip.mp4: ", err)
            self.assertIn("did not finish in 3 s", err)
            self.assertLess(time.time() - t0, 30, "the timeout waited on pipes a grandchild held")
            self.assertTrue(test_scripts.ended(test_scripts.read_pid(pid_file)))
            self.assertEqual(mcommon.PROBE_TIMEOUT, 120)

    def test_ffmpeg_gets_an_empty_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "reads.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write("import sys\nsys.stdout.write('got %r' % sys.stdin.read())\n")
            r = mcommon.run_ff([sys.executable, script], "read stdin")
            self.assertEqual(r.stdout, b"got ''")
            r = mcommon.run_ff([sys.executable, script], "read stdin", input=b"q")
            self.assertEqual(r.stdout, b"got 'q'")

    def test_ffmpeg_is_the_environment_variable_or_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            fake = executable(d, "my-ffmpeg")
            with environ(AGENT_PLAYBOOK_FFMPEG=fake):
                self.assertEqual(mcommon.ffmpeg(), fake)
            with environ(AGENT_PLAYBOOK_FFMPEG=os.path.join(d, "not-here")):
                code, err = exits(mcommon.ffmpeg)  # a wrong path never falls back to another build
                self.assertEqual(code, 1)
                self.assertIn("AGENT_PLAYBOOK_FFMPEG", err)
            with environ(AGENT_PLAYBOOK_FFMPEG=None, AGENT_PLAYBOOK_FFPROBE=None, PATH=d):
                code, err = exits(mcommon.ffmpeg)
                self.assertEqual((code, err.strip().split(": ", 1)[1]), (1, "ffmpeg not found on PATH; install it or set AGENT_PLAYBOOK_FFMPEG"))
                code, err = exits(mcommon.ffprobe)
                self.assertIn("ffprobe not found on PATH; install it or set AGENT_PLAYBOOK_FFPROBE", err)

    def test_faststart_reads_the_atom_order(self):
        with tempfile.TemporaryDirectory() as d:
            for name, data, want in (("fast.mp4", b"....ftyp....moov....mdat", True), ("slow.mp4", b"....ftyp....mdat....moov", False),
                                     ("none.mp4", b"not a video", False)):
                path = os.path.join(d, name)
                with open(path, "wb") as fh:
                    fh.write(data)
                self.assertEqual(mcommon.faststart(path), want, name)

    @needs("ffmpeg", "Pillow")
    def test_probe_and_video_frame_on_a_real_clip(self):
        with tempfile.TemporaryDirectory() as d:
            clip = os.path.join(d, "clip.mp4")
            mcommon.run_ff([mcommon.ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=1"]
                           + mcommon.X264 + mcommon.TAIL + [clip], "make the test clip")
            info = mcommon.probe(clip)
            video = [s for s in info["streams"] if s["codec_type"] == "video"][0]
            self.assertEqual((video["width"], video["height"], video["pix_fmt"]), (160, 90, "yuv420p"))
            self.assertAlmostEqual(float(info["format"]["duration"]), 1.0, delta=0.2)
            self.assertTrue(mcommon.faststart(clip))
            frame = mcommon.video_frame(clip, 0.5)
            self.assertEqual((frame.size, frame.mode), ((160, 90), "RGB"))
            self.assertFalse(mcommon.is_flat(frame))
            code, err = exits(mcommon.video_frame, clip, 30.0)
            self.assertEqual(code, 1)
            self.assertIn("no frame at 30.000 s", err)
            code, err = exits(mcommon.probe, os.path.join(d, "missing.mp4"))
            self.assertEqual(code, 1)
            self.assertIn("ffprobe", err)


class TestFonts(unittest.TestCase):
    def test_a_bare_name_is_found_in_the_font_folders(self):
        with tempfile.TemporaryDirectory() as d, environ(AGENT_PLAYBOOK_FONT_DIR=None):
            nested = os.path.join(d, "truetype", "family")
            os.makedirs(nested)
            path = os.path.join(nested, "Example-Bold-For-Tests.ttf")
            with open(path, "wb") as fh:
                fh.write(b"not really a font")
            self.assertEqual(mcommon.find_font(path), path)
            self.assertEqual(mcommon.find_font("example-bold-for-tests.ttf", font_dir=d), path)
            with environ(AGENT_PLAYBOOK_FONT_DIR=d):
                self.assertEqual(mcommon.find_font("Example-Bold-For-Tests.ttf"), path)
                self.assertEqual(mcommon.font_dirs("first")[:2], ["first", d])
            self.assertIsNone(mcommon.find_font("Example-Bold-For-Tests.ttf", font_dir=os.path.join(d, "empty")))
            self.assertIsNone(mcommon.find_font(os.path.join(d, "Example-Bold.ttf")), "a path is taken as given, never searched for")

    @needs("Pillow")
    def test_builtin_needs_no_font_file_and_an_unknown_font_exits_1(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as d, environ(AGENT_PLAYBOOK_FONT_DIR=None):
            f = mcommon.font("builtin", 40)
            im = Image.new("RGB", (300, 80), "white")
            ImageDraw.Draw(im).text((10, 10), "Hello", font=f, fill="black")
            self.assertFalse(mcommon.is_flat(im))
            wide = ImageDraw.Draw(im).textlength("Hello", font=mcommon.font("builtin", 80))
            self.assertGreater(wide, ImageDraw.Draw(im).textlength("Hello", font=f), "the builtin font scales")
            code, err = exits(mcommon.font, "No-Such-Font-Anywhere.ttf", 40, font_dir=d)
            self.assertEqual(code, 1)
            self.assertIn("font not found: No-Such-Font-Anywhere.ttf", err)
            self.assertIn(d, err)  # the folders searched are named
            broken = os.path.join(d, "Broken.ttf")
            with open(broken, "wb") as fh:
                fh.write(b"not really a font")
            code, err = exits(mcommon.font, "Broken.ttf", 40, font_dir=d)
            self.assertEqual(code, 1)
            self.assertIn("can't use the font", err)


@needs("Pillow")
class TestImageMeasures(unittest.TestCase):
    def checkerboard(self, size=256, square=16, mode="RGB"):
        from PIL import Image, ImageDraw
        im = Image.new(mode, (size, size), (204, 204, 204))
        d = ImageDraw.Draw(im)
        for y in range(0, size, square):
            for x in range(0, size, square):
                if (x // square + y // square) % 2:
                    d.rectangle([x, y, x + square - 1, y + square - 1], fill=(255, 255, 255))
        return im

    def test_a_solid_frame_is_flat(self):
        from PIL import Image, ImageDraw
        self.assertTrue(mcommon.is_flat(Image.new("RGB", (320, 180), (0, 0, 0))))
        self.assertTrue(mcommon.is_flat(Image.new("RGB", (320, 180), (26, 35, 50))))
        self.assertTrue(mcommon.is_flat(Image.new("RGBA", (320, 180), (255, 255, 255, 0))))
        drawn = Image.new("RGB", (320, 180), (250, 250, 244))
        ImageDraw.Draw(drawn).rectangle([40, 40, 200, 120], fill=(30, 60, 160))
        self.assertFalse(mcommon.is_flat(drawn))

    def test_black_tiles_are_counted(self):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (256, 256), (120, 130, 140))
        self.assertEqual(mcommon.black_blocks(im), 0)
        ImageDraw.Draw(im).rectangle([64, 128, 127, 191], fill=(0, 0, 0))
        self.assertEqual(mcommon.black_blocks(im), 1)
        ImageDraw.Draw(im).point((100, 150), fill=(9, 9, 9))  # one pixel that is not black: the tile is not black
        self.assertEqual(mcommon.black_blocks(im), 0)
        self.assertEqual(mcommon.black_blocks(Image.new("RGB", (256, 256), (2, 2, 2))), 16)
        self.assertEqual(mcommon.black_blocks(Image.new("RGB", (256, 256), (0, 0, 0)), block=128), 4)
        self.assertEqual(mcommon.black_blocks(Image.new("RGB", (100, 60), (0, 0, 0))), 0, "part tiles are not counted")

    def test_alpha_is_read_from_every_mode_that_has_it(self):
        from PIL import Image, ImageDraw
        for mode, clear, solid in (("RGBA", (0, 0, 0, 0), (40, 90, 200, 255)), ("LA", (0, 0), (120, 255))):
            im = Image.new(mode, (128, 128), solid)
            ImageDraw.Draw(im).ellipse([32, 32, 95, 95], fill=clear)
            self.assertEqual(mcommon.alpha_problem(im), "", mode)
        pa = Image.new("RGBA", (128, 128), (40, 90, 200, 255))
        ImageDraw.Draw(pa).rectangle([0, 0, 63, 127], fill=(0, 0, 0, 0))
        self.assertEqual(mcommon.alpha_problem(pa.convert("PA")), "")
        p = Image.new("P", (128, 128), 1)
        p.putpalette([0, 0, 0, 40, 90, 200] + [0] * 762)
        ImageDraw.Draw(p).rectangle([0, 0, 63, 127], fill=0)
        p.info["transparency"] = 0
        self.assertEqual(mcommon.alpha_problem(p), "")
        self.assertEqual(mcommon.alpha_problem(Image.new("RGBA", (128, 128), (1, 2, 3, 255))),
                         "alpha channel but only 0.00% of pixels are transparent")

    def test_real_alpha_is_a_hole_not_a_painted_checkerboard(self):
        from PIL import Image, ImageDraw
        cutout = Image.new("RGBA", (256, 256), (40, 90, 200, 255))
        ImageDraw.Draw(cutout).ellipse([64, 64, 191, 191], fill=(0, 0, 0, 0))
        self.assertTrue(mcommon.real_alpha(cutout))
        self.assertEqual(mcommon.alpha_problem(cutout), "")
        board = self.checkerboard()
        self.assertFalse(mcommon.real_alpha(board))
        self.assertEqual(mcommon.alpha_problem(board), "opaque image with a painted checkerboard")
        self.assertEqual(mcommon.alpha_problem(self.checkerboard(mode="RGBA")), "opaque image with a painted checkerboard")
        photo = Image.new("RGB", (256, 256), (250, 250, 244))
        ImageDraw.Draw(photo).ellipse([64, 64, 191, 191], fill=(240, 200, 60))
        self.assertEqual(mcommon.alpha_problem(photo), "no alpha channel")
        self.assertFalse(mcommon.painted_checkerboard(photo))
        stripes = Image.new("RGB", (256, 256), (204, 204, 204))
        for x in range(0, 256, 32):
            ImageDraw.Draw(stripes).rectangle([x, 0, x + 15, 255], fill=(255, 255, 255))
        self.assertFalse(mcommon.painted_checkerboard(stripes), "stripes alternate one way only")
        speck = Image.new("RGBA", (256, 256), (40, 90, 200, 255))
        speck.putpixel((0, 0), (0, 0, 0, 0))
        self.assertIn("only 0.00% of pixels are transparent", mcommon.alpha_problem(speck))


class TestSmallHelpers(unittest.TestCase):
    def test_write_json_is_one_canonical_form(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            mcommon.write_json(path, {"name": "caf\u00e9", "ids": [1, 2]})
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b'{\n "name": "caf\xc3\xa9",\n "ids": [\n  1,\n  2\n ]\n}\n')
            self.assertEqual(os.listdir(d), ["out.json"])

    def test_sha256_and_parse_ids(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.bin")
            with open(path, "wb") as fh:
                fh.write(b"abc")
            self.assertEqual(mcommon.sha256(path), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(mcommon.parse_ids("3,17,40"), [3, 17, 40])
        self.assertEqual(mcommon.parse_ids(" 7 , 3,7"), [7, 3])
        for bad in ("", "3,,4", "three", "3-7", "0", "-2", "3.5", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                mcommon.parse_ids(bad)

    def test_report_prints_each_check_and_fails_on_one_failure(self):
        rep = mcommon.Report()
        rep.ok("dims 1280x720", "A_it-works.jpg")
        self.assertEqual(self.finish(rep), (0, "ok   dims 1280x720  A_it-works.jpg\n", ""))
        self.assertFalse(rep.fail("no-exif", "IMG_3915.jpg", "GPS tags present"))
        code, out, err = self.finish(rep)
        self.assertEqual(code, 1)
        self.assertEqual(out, "ok   dims 1280x720  A_it-works.jpg\nFAIL no-exif  IMG_3915.jpg: GPS tags present\n")
        self.assertTrue(err.endswith(": 1 of 2 checks failed\n"), err)
        self.assertEqual(rep.as_list()[1], {"file": "IMG_3915.jpg", "check": "no-exif", "ok": False, "detail": "GPS tags present"})

    def finish(self, rep):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rep.finish()
        return code, out.getvalue(), err.getvalue()

    def test_the_format_numbers(self):
        self.assertEqual(mcommon.THUMB_MAX_BYTES, 2047152)  # 2 MiB less a margin: YouTube's thumbnail limit
        self.assertEqual(mcommon.UPLOAD_MAX_BYTES, 2147483648)
        self.assertEqual((mcommon.FRAME_9x16, mcommon.FPS, mcommon.THUMB), ((1080, 1920), 30, (1280, 720)))
        self.assertEqual(mcommon.SAFE, {"top": 280, "right": 950, "bottom": 1600})
        self.assertEqual(mcommon.YT_BANNER["safe"], (1546, 423))

    def test_importing_mcommon_needs_no_optional_module(self):
        code = "import sys; sys.path.insert(0, %r); import mcommon; print(sorted(m for m in ('PIL', 'numpy') if m in sys.modules))" % MEDIA
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=60,
                           env=dict(os.environ, PYTHONPATH=GUARD, AGENT_PLAYBOOK_TEST_HIDE_MODULES="PIL,numpy"))
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "[]"), r.stderr)


class TestTheGate(unittest.TestCase):
    def test_needs_skips_or_fails_by_the_environment(self):
        import mediatest
        saved = dict(mediatest.HAVE), mediatest.REQUIRE
        try:
            mediatest.HAVE.update(Pillow=False, ffmpeg=True)

            def body(self):
                return "ran"
            mediatest.REQUIRE = False
            with self.assertRaises(unittest.SkipTest) as ctx:
                mediatest.needs("ffmpeg", "Pillow")(body)(self)
            self.assertEqual(str(ctx.exception), "needs Pillow")
            mediatest.REQUIRE = True
            with self.assertRaises(AssertionError) as ctx:
                mediatest.needs("ffmpeg", "Pillow")(body)(self)
            self.assertIn("AGENT_PLAYBOOK_REQUIRE_MEDIA=1 forbids skipping", str(ctx.exception))
            self.assertEqual(mediatest.needs("ffmpeg")(body)(self), "ran")
        finally:
            mediatest.HAVE.clear()
            mediatest.HAVE.update(saved[0])
            mediatest.REQUIRE = saved[1]

    @unittest.skipUnless(os.environ.get("AGENT_PLAYBOOK_REQUIRE_MEDIA", "") not in ("", "0"), "only on the row that must have them")
    def test_this_row_has_pillow_numpy_and_ffmpeg(self):
        self.assertEqual(HAVE, {"Pillow": True, "numpy": True, "ffmpeg": True})


if __name__ == "__main__":
    unittest.main()
