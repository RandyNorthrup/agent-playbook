"""Offline tests for the helpers brought into agent-playbook/scripts: codex_img, loop_qc, captions, mklibanner,
render_short and the banner templates; plus the Windows-only guard and the CPU wait.

No image generation, no browser, no clipboard, no network: codex_img runs against a stand-in "codex" that
writes a file, loop_qc measures loops drawn here, and bridge_wait is tested up to the point
where it would read the process list. Tests that need numpy or Pillow skip when those aren't installed
(they fail instead when AGENT_PLAYBOOK_REQUIRE_MEDIA=1: see mediatest.py).
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediatest import needs  # noqa: E402
from test_scripts import ROOT, ended, executable, read_pid, run  # noqa: E402

TEMPLATES = os.path.join(ROOT, "agent-playbook", "templates")
HAVE_NUMPY = importlib.util.find_spec("numpy") is not None



class TestWindowsOnlyGuards(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "the guards are for other systems")
    def test_clear_message_off_windows(self):
        for args in (["1"], ["--list"]):
            with self.subTest(args=args):
                r = run("bridge_wait.py", *args)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("Windows only", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_wait_cpu_reads_load_on_this_system(self):
        import wait_cpu
        sample = wait_cpu.pick_sampler()
        self.assertIsNotNone(sample, "Windows, Linux and macOS all have a sampler")
        self.assertTrue(0.0 <= sample(0.05) <= 100.0 * 64)

    def test_wait_cpu_streak_and_timeout(self):
        import wait_cpu
        loads = iter([90, 10, 10, 90, 10, 10, 10])
        self.assertEqual(wait_cpu.wait(lambda interval: next(loads), 75, 1, 0), (True, 10))
        self.assertEqual(wait_cpu.wait(lambda interval: 99.0, 75, -1, 0), (False, 99.0))  # deadline already past
        r = run("wait_cpu.py", "100", "1", "0.2")  # any load is under 100: three quick samples
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("cpu-ok "), r.stdout)

    def test_bridge_wait_reads_the_process_list(self):
        import bridge_wait
        self.assertEqual(bridge_wait.parse_hosts(""), [])
        one = '{"ProcessId":4242,"Created":"2026-11-09T10:00:00.0000000Z","Chrome":true}'
        self.assertEqual([h["ProcessId"] for h in bridge_wait.parse_hosts(one)], [4242])
        self.assertEqual(len(bridge_wait.parse_hosts("[%s,%s]" % (one, one.replace("true", "false")))), 2)



FAKE_CODEX = r'''
import json, os, sys
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_CODEX_MODE", "ok")
record = {"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}
with open(os.environ["FAKE_CODEX_LOG"], "w", encoding="utf-8") as fh:
    json.dump(record, fh)
if mode == "crash":
    sys.stderr.write("usage limit reached\n")
    sys.exit(1)
if mode == "hang":  # what node running a stuck codex binary looks like: a child of its own, then nothing
    import subprocess, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
    with open(os.environ["TREE_PID_FILE"], "w") as fh:
        fh.write(str(child.pid))
    time.sleep(90)
print(json.dumps({"type": "thread.started", "thread_id": "thread-abc"}))
folder = os.path.join(os.environ["CODEX_HOME"], "generated_images", "thread-abc" if mode == "ok" else "another-thread")
os.makedirs(folder, exist_ok=True)
with open(os.path.join(folder, "ig_0001.png"), "wb") as fh:
    fh.write(b"\x89PNG\r\n\x1a\n" + b"this run's image")
print(json.dumps({"type": "turn.completed"}))
'''


class TestCodexImg(unittest.TestCase):
    """codex_img.py against a stand-in codex: no image generation, no network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        script = os.path.join(d, "fake_codex.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(FAKE_CODEX)
        self.codex = executable(d, "codex", python_script=script)
        self.log = os.path.join(d, "codex_log.json")
        self.home = os.path.join(d, "codex-home")
        # an older image from another run must never be picked up
        other = os.path.join(self.home, "generated_images", "older-thread")
        os.makedirs(other)
        with open(os.path.join(other, "ig_0000.png"), "wb") as fh:
            fh.write(b"someone else's image")
        self.env = {"CODEX_HOME": self.home, "FAKE_CODEX_LOG": self.log, "CODEX_BIN": self.codex}
        self.out = os.path.join(d, "art", "scene.png")

    def test_saves_this_runs_image_and_its_prompt(self):
        ref = os.path.join(self.tmp.name, "sheet.png")
        with open(ref, "wb") as fh:
            fh.write(b"ref")
        r = run("codex_img.py", "A blue sketch \u65e5\u672c", self.out, "--ref", ref, "--timeout", "60", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(r.stdout.startswith("saved "), r.stdout)
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read(), b"\x89PNG\r\n\x1a\n" + b"this run's image")
        with open(self.out[:-4] + ".txt", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "A blue sketch \u65e5\u672c\n")
        with open(self.log, encoding="utf-8") as fh:
            seen = json.load(fh)
        self.assertEqual(seen["argv"][:5], ["exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write"])
        self.assertEqual(seen["argv"][5:], ["-i", os.path.realpath(ref), "-"])
        self.assertIn("A blue sketch \u65e5\u672c", seen["prompt"])  # the prompt goes in on stdin as UTF-8
        self.assertNotEqual(os.path.realpath(seen["cwd"]), os.path.realpath(self.tmp.name))  # a throwaway work folder

    def test_codex_flag_wins_over_the_environment(self):
        env = dict(self.env, CODEX_BIN=os.path.join(self.tmp.name, "not-here"))
        r = run("codex_img.py", "prompt", self.out, "--codex", self.codex, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_exit_1_when_the_run_made_no_image_for_its_thread(self):
        for mode in ("other-thread", "crash"):
            with self.subTest(mode=mode):
                r = run("codex_img.py", "prompt", self.out, env=dict(self.env, FAKE_CODEX_MODE=mode))
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("no image produced for this run's thread", r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertFalse(os.path.exists(self.out), "another run's image was copied")

    def test_a_run_past_the_timeout_is_stopped_with_what_it_started(self):
        pid_file = os.path.join(self.tmp.name, "grandchild.pid")
        t0 = time.time()
        r = run("codex_img.py", "prompt", self.out, "--timeout", "3", env=dict(self.env, FAKE_CODEX_MODE="hang", TREE_PID_FILE=pid_file))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("codex did not finish in 3 s (stopped)", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertLess(time.time() - t0, 60)
        self.assertTrue(ended(read_pid(pid_file)), "the process codex started outlived the timeout")
        self.assertFalse(os.path.exists(self.out))

    def test_exit_1_when_codex_is_missing(self):
        r = run("codex_img.py", "prompt", self.out, env=dict(self.env, CODEX_BIN=os.path.join(self.tmp.name, "not-here")))
        self.assertEqual(r.returncode, 1)
        self.assertIn("codex not found", r.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_thread_id_and_newest_png(self):
        import codex_img
        from pathlib import Path
        events = '{"type":"session.created"}\n{"type":"thread.started","thread_id":"t-9"}\n{"type":"turn.completed"}\n'
        self.assertEqual(codex_img.thread_id(events), "t-9")
        self.assertIsNone(codex_img.thread_id('{"type":"error","message":"usage limit"}\n'))
        self.assertIsNone(codex_img.thread_id('"thread.started" but not json\n'))
        self.assertIsNone(codex_img.newest_png(None, 0))
        self.assertIsNone(codex_img.newest_png(Path(self.tmp.name) / "missing", 0))
        older = Path(self.home) / "generated_images" / "older-thread"
        self.assertEqual(codex_img.newest_png(older, 0).name, "ig_0000.png")
        self.assertIsNone(codex_img.newest_png(older, os.path.getmtime(str(older / "ig_0000.png")) + 60), "made before this run")


def blob_frames(n, size=64, radius=16.0, path="circle"):
    """n RGBA frames of a soft dot: once round a circle (a clean loop), a straight line that never comes
    back ("line"), or a new random place every frame ("jump")."""
    import numpy as np
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:size, 0:size].astype(float)
    frames = []
    for i in range(n):
        if path == "circle":
            a = 2 * np.pi * i / n
            cx, cy = size / 2 + radius * np.cos(a), size / 2 + radius * np.sin(a)
        elif path == "line":
            cx, cy = 10 + 44.0 * i / (n - 1), size / 2
        else:
            cx, cy = rng.uniform(8, size - 8, 2)
        alpha = 255 * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 6.0 ** 2))
        frame = np.zeros((size, size, 4), np.uint8)
        frame[..., :3] = (240, 200, 90)
        frame[..., 3] = alpha.round().astype(np.uint8)
        frames.append(frame)
    return frames


@needs("numpy", "Pillow")
class TestLoopQc(unittest.TestCase):
    def save(self, d, frames, name="anim"):
        from PIL import Image
        folder = os.path.join(d, name)
        os.makedirs(folder)
        for i, f in enumerate(frames):
            Image.fromarray(f).save(os.path.join(folder, "f%03d.png" % i))
        return folder

    def test_a_built_loop_passes(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.save(d, blob_frames(96))
            out = os.path.join(d, "qc")
            r = run("loop_qc.py", folder, "--fps", "24", "--out", out)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertRegex(r.stdout, r"^anim: 96 frames, cycle 4\.00s \| .* \| ok\n$")
            self.assertEqual(sorted(os.listdir(out)), ["seam.png", "strip.png"])

    def test_default_out_folder_is_beside_the_animation(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.save(d, blob_frames(96))
            r = run("loop_qc.py", folder, "--fps", "24")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(sorted(os.listdir(d)), ["anim", "anim_loopqc"])

    def test_a_loop_that_does_not_close_fails_on_seam(self):
        with tempfile.TemporaryDirectory() as d:
            r = run("loop_qc.py", self.save(d, blob_frames(96, path="line")), "--fps", "24", "--out", os.path.join(d, "qc"))
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertRegex(r.stdout, r"FAIL .*seam")
            self.assertIn("FAIL", r.stderr)
            self.assertTrue(os.path.exists(os.path.join(d, "qc", "seam.png")), "the sheets are written for a failing loop too")

    def test_frames_drawn_afresh_fail_on_boil(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.save(d, blob_frames(96, path="jump"))
            r = run("loop_qc.py", folder, "--fps", "24", "--out", os.path.join(d, "qc"))
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertRegex(r.stdout, r"FAIL .*boil")
            allowed = run("loop_qc.py", folder, "--fps", "24", "--out", os.path.join(d, "qc"), "--allow-boil")
            self.assertNotIn("boil", allowed.stdout.split("|")[-1])

    def test_a_loop_shorter_than_a_second_and_a_half_is_too_fast(self):
        with tempfile.TemporaryDirectory() as d:
            r = run("loop_qc.py", self.save(d, blob_frames(96)), "--fps", "96", "--out", os.path.join(d, "qc"))
            self.assertEqual(r.returncode, 1)
            self.assertIn("too fast", r.stdout)

    def test_animated_gif_uses_its_own_frame_durations(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            frames = [Image.fromarray((f[..., :3].astype(float) * f[..., 3:4] / 255).astype("uint8")) for f in blob_frames(48)]
            gif = os.path.join(d, "loop.gif")
            frames[0].save(gif, save_all=True, append_images=frames[1:], duration=50, loop=0)
            r = run("loop_qc.py", gif, "--out", os.path.join(d, "qc"))
            self.assertRegex(r.stdout, r"^loop\.gif: 48 frames, cycle 2\.40s ")

    def test_input_that_cannot_be_measured_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            two = self.save(d, blob_frames(96)[:2], "two")
            mixed = self.save(d, [blob_frames(4)[0], blob_frames(4, size=32)[1], blob_frames(4)[2]], "mixed")
            text = os.path.join(d, "notes.gif")
            with open(text, "w") as fh:
                fh.write("not an image")
            for target, message in ((two, "need at least 3 frames"), (mixed, "not all the same size"),
                                    (text, "can't read"), (os.path.join(d, "missing"), "no such file or folder")):
                with self.subTest(target=os.path.basename(target)):
                    r = run("loop_qc.py", target, "--out", os.path.join(d, "qc"))
                    self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                    self.assertIn(message, r.stderr)
                    self.assertNotIn("Traceback", r.stderr)

    def test_the_fast_measures_equal_the_plain_float64_ones(self):
        # flatten() works in float32 and change() takes the channel maximum with np.maximum: the numbers must be
        # the ones the plain float64 .max(-1) formulas give
        import numpy as np
        import loop_qc
        loop_qc.load_modules()
        bg = (30, 33, 40)
        for path in ("circle", "jump"):
            frames = blob_frames(24, size=48, path=path)
            ref = [f[..., :3].astype(float) * (f[..., 3:4] / 255.0) + np.array(bg, float) * (1 - f[..., 3:4] / 255.0) for f in frames]

            def plain_residual(a, b, reach=2):
                raw = np.abs(b - a).max(-1).mean()
                if raw < loop_qc.FLOOR:
                    return 0.0
                h, w = a.shape[:2]
                best = raw
                for dy in range(-reach, reach + 1):
                    for dx in range(-reach, reach + 1):
                        sa = a[max(0, -dy): h - max(0, dy), max(0, -dx): w - max(0, dx)]
                        sb = b[max(0, dy): h - max(0, -dy), max(0, dx): w - max(0, -dx)]
                        best = min(best, float(np.abs(sb - sa).max(-1).mean()))
                return best / raw
            steps = [np.abs(ref[(i + 1) % len(ref)] - ref[i]).max(-1) for i in range(len(ref))]
            med = max(float(np.median([s.mean() for s in steps[:-1]])), loop_qc.FLOOR)
            want = {"step": med, "jump": max(s.mean() for s in steps[:-1]) / med, "seam": steps[-1].mean() / med,
                    "pop": max(float(np.percentile(s, 99.9)) for s in steps),
                    "boil": float(np.median([plain_residual(ref[i], ref[(i + 1) % len(ref)]) for i in range(len(ref))]))}
            rgb = [loop_qc.flatten(f, bg) for f in frames]
            self.assertEqual(rgb[0].dtype, np.float32, "float32 frames: half the memory")
            got, _ = loop_qc.measure(rgb, 4.0)
            for key, value in want.items():
                self.assertAlmostEqual(got[key], value, delta=1e-3 * max(1.0, abs(value)), msg="%s %s" % (path, key))

    def test_limits_match_the_documentation(self):
        import loop_qc
        self.assertEqual(loop_qc.LIMITS, {"jump": 2.5, "seam": 1.5, "boil": 0.5})
        for text in ("Limit 2.5.", "Limit 1.5.", "Limit 0.5;", "under 1.5 s"):
            self.assertIn(text, loop_qc.__doc__)


class TestLoopQcWithoutItsModules(unittest.TestCase):
    def test_missing_module_exits_3_with_the_install_line(self):
        with tempfile.TemporaryDirectory() as d:
            for hide, message in (("numpy", "needs numpy: pip install numpy"), ("PIL", "needs PIL: pip install Pillow")):
                r = run("loop_qc.py", d, env={"AGENT_PLAYBOOK_TEST_HIDE_MODULES": hide})
                if hide == "PIL" and not HAVE_NUMPY:
                    message = "needs numpy"
                self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                self.assertIn(message, r.stderr)
                self.assertNotIn("Traceback", r.stderr)


class TestClipImage(unittest.TestCase):
    @needs("Pillow")
    def test_transparent_parts_go_on_white_not_black(self):
        # a CF_DIB has no transparency; dropping the alpha showed the colour transparent pixels hold: black
        from PIL import Image
        import clipimage
        im = Image.new("RGBA", (4, 2), (0, 0, 0, 0))
        im.putpixel((0, 0), (200, 10, 10, 255))
        im.putpixel((1, 0), (0, 0, 200, 128))
        flat = clipimage.flat_rgb(im)
        self.assertEqual(flat.mode, "RGB")
        self.assertEqual(flat.getpixel((0, 0)), (200, 10, 10))
        self.assertEqual(flat.getpixel((3, 1)), (255, 255, 255))
        self.assertTrue(all(abs(c - w) <= 1 for c, w in zip(flat.getpixel((1, 0)), (127, 127, 227))), flat.getpixel((1, 0)))
        self.assertEqual(clipimage.flat_rgb(Image.new("RGB", (2, 2), (1, 2, 3))).getpixel((0, 0)), (1, 2, 3))
        p = Image.new("P", (2, 1))
        p.putpalette([0, 0, 0, 10, 20, 30] + [0] * 762)
        p.putpixel((1, 0), 1)
        p.info["transparency"] = 0
        self.assertEqual([clipimage.flat_rgb(p).getpixel((x, 0)) for x in (0, 1)], [(255, 255, 255), (10, 20, 30)])

    def test_an_applescript_path_is_quoted(self):
        import clipimage
        self.assertEqual(clipimage.applescript_text('/Users/x/a "b"\\c.png'), '"/Users/x/a \\"b\\"\\\\c.png"')


class TestCaptions(unittest.TestCase):
    CUES = [{"start": 0.0, "end": 3.5, "text": "line one\nline two"},
            {"start": 3.5, "end": 3725.25, "text": "BIG", "style": "Big"},
            {"start": 4, "end": 5, "text": "tag", "style": "Tag"}]

    def test_build_ass(self):
        import captions
        ass = captions.build_ass(self.CUES, 1080, 1920)
        self.assertIn("PlayResX: 1080\nPlayResY: 1920\n", ass)
        self.assertIn("Style: Cap,Segoe UI Semibold,64,", ass)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:03.50,Cap,,0,0,0,,{\\fad(180,180)}line one\\Nline two\n", ass)
        self.assertIn("Dialogue: 0,0:00:03.50,1:02:05.25,Big,,0,0,0,,{\\fad(180,180)}BIG\n", ass)
        half = captions.build_ass(self.CUES, 540, 960)
        self.assertIn("Style: Cap,Segoe UI Semibold,32,", half)  # sizes scale with the width

    def test_times_round_to_a_real_clock_time(self):
        import captions
        # %05.2f of 59.996 printed "0:00:60.00", which players read wrong
        for t, want in ((59.996, "0:01:00.00"), (3599.999, "1:00:00.00"), (0.004, "0:00:00.00"), (0.006, "0:00:00.01"),
                        (3725.25, "1:02:05.25"), (-3, "0:00:00.00"), (61.5, "0:01:01.50"), (36000, "10:00:00.00")):
            self.assertEqual(captions.stamp(t), want, t)

    def test_writes_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            cues, out = os.path.join(d, "cues.json"), os.path.join(d, "out.ass")
            with open(cues, "w", encoding="utf-8") as fh:
                json.dump(self.CUES, fh)
            r = run("captions.py", cues, out, "1080", "1920")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("3 cues", r.stdout)
            with open(out, encoding="utf-8-sig") as fh:
                self.assertEqual(fh.read().count("Dialogue: "), 3)

    def test_malformed_cues_exit_1_and_name_the_cue(self):
        bad = [("{}", "must hold a list"), ("[1]", "cue 1 is not an object"),
               ('[{"start": 0, "end": 1, "text": "ok"}, {"start": 2, "end": 1, "text": "x"}]', "cue 2: end (1) is not after start (2)"),
               ('[{"start": "0", "end": 1, "text": "x"}]', "cue 1: start must be a number"),
               ('[{"start": 0, "end": NaN, "text": "x"}]', "cue 1: end must be a number"),
               ('[{"start": -Infinity, "end": 1, "text": "x"}]', "cue 1: start must be a number"),
               ('[{"start": 0, "end": 1}]', "cue 1: text is missing"),
               ('[{"start": 0, "end": 1, "text": "x", "style": "Huge"}]', "cue 1: style 'Huge'"),
               ("[not json", "cues.json:")]
        with tempfile.TemporaryDirectory() as d:
            cues, out = os.path.join(d, "cues.json"), os.path.join(d, "out.ass")
            for text, message in bad:
                with self.subTest(cues=text):
                    with open(cues, "w", encoding="utf-8") as fh:
                        fh.write(text)
                    r = run("captions.py", cues, out, "1080", "1920")
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn(message, r.stderr)
                    self.assertNotIn("Traceback", r.stderr)
                    self.assertFalse(os.path.exists(out))


class TestBanners(unittest.TestCase):
    def source(self, headline="<h1>Your headline<br>Goes here</h1>"):
        """A stand-in for the 1500x500 banner page: every piece mklibanner.py swaps and a two-line headline."""
        import mklibanner
        return ("<!doctype html>\n" + "\n".join(old for old, _ in mklibanner.SWAPS) + "\n" + headline + "\n"
                + "".join(mklibanner.LOCAL_FONTS))

    def test_converts_every_piece(self):
        import mklibanner
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "banner.html")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(self.source())
            r = run("mklibanner.py", src)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(d, "banner_li.html"), encoding="utf-8") as fh:
                page = fh.read()
            for old, new in mklibanner.SWAPS[1:]:
                self.assertIn(new, page)
                self.assertNotIn(old, page)
            self.assertIn("<h1>Your headline · Goes here</h1>", page)  # one line
            self.assertIn("fonts.googleapis.com", page)
            self.assertNotIn("@font-face", page)
            other = os.path.join(d, "out", "li.html")
            os.makedirs(os.path.dirname(other))
            self.assertEqual(run("mklibanner.py", src, other).returncode, 0)
            self.assertTrue(os.path.exists(other))

    def test_a_changed_source_fails_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "banner.html")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(self.source().replace("width: 1500px; height: 500px;", "width: 1600px; height: 500px;"))
            r = run("mklibanner.py", src)
            self.assertEqual(r.returncode, 1)
            self.assertIn("1 expected piece missing", r.stderr)
            self.assertIn("width: 1500px; height: 500px;", r.stderr)
            self.assertEqual(os.listdir(d), ["banner.html"])

    def test_any_two_line_headline_converts_and_a_missing_one_is_named(self):
        # the headline used to be one owner's exact text, so every other banner was refused
        import mklibanner
        page, missing = mklibanner.convert(self.source("<h1>Build tools \u00b7 Games<br>Open source</h1>"))
        self.assertEqual(missing, [])
        self.assertIn("<h1>Build tools \u00b7 Games \u00b7 Open source</h1>", page)
        page, missing = mklibanner.convert(self.source("<h1>Already one line</h1>"))
        self.assertEqual(missing, [mklibanner.HEADLINE_PIECE])

    def test_the_kept_templates_are_the_right_sizes(self):
        for name, size in (("banner_li.html", "width: 1584px; height: 396px;"), ("banner_fb.html", "width: 1640px; height: 624px;")):
            with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn(size, page, name)
            self.assertIn("<h1>Your headline", page, name)  # an obvious placeholder, not anyone's real headline
            self.assertIn("YOURSITE.EXAMPLE", page, name)
            self.assertNotIn("file:///", page, name)

    def test_the_templates_hold_placeholders_only(self):
        for name in sorted(os.listdir(TEMPLATES)):
            with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
                page = fh.read()
            self.assertNotRegex(page, r"[\w.+-]+@[\w-]+\.[a-z]{2,}", name)  # no e-mail addresses
            self.assertNotIn("reference/", page, "%s points at a file this repository does not have" % name)
        with open(os.path.join(TEMPLATES, "carousel.html"), encoding="utf-8") as fh:
            self.assertIn("'@yourhandle'", fh.read())


class TestRenderShort(unittest.TestCase):
    def test_needs_to_know_where_shorts_py_is(self):
        with tempfile.TemporaryDirectory() as d:
            r = run("render_short.py", d, "--from", "1", "--to", "2", "--hook", "x")
            self.assertEqual(r.returncode, 2)
            self.assertIn("--shorts-dir", r.stderr)
            r = run("render_short.py", d, env={"AGENT_PLAYBOOK_SHORTS_DIR": os.path.join(d, "nowhere")})
            self.assertEqual(r.returncode, 1)
            self.assertIn("no shorts.py in", r.stderr)

    def test_arguments_after_the_video_folder_go_to_shorts_py(self):
        import render_short
        a = render_short.build_parser().parse_args(["--shorts-dir", "S", "videos/10", "--from", "3.85", "--to", "33.29",
                                                    "--hook", "Why *this* works", "--style", "strip", "--no-log"])
        self.assertEqual((a.shorts_dir, a.video_dir), ("S", "videos/10"))
        self.assertEqual(a.shorts_args, ["--from", "3.85", "--to", "33.29", "--hook", "Why *this* works", "--style", "strip", "--no-log"])

    @needs("numpy")
    def test_runs_shorts_py_with_the_edges_moved_to_the_real_onsets(self):
        # a stand-in shorts.py and a stand-in ffmpeg (10 ms frames: silence, then speech from 2.30 s to 4.00 s)
        shorts = '''
import json, sys
NO_WINDOW = 0
def window(words, A, B, pre, post):
    ws = [w for w in words if A <= w["start"] and w["end"] <= B]
    return ws[0]["start"] - pre, ws[-1]["end"] + post, ws
def main():
    words = [{"w": "before", "start": 1.0, "end": 1.5}, {"w": "hello", "start": 2.5, "end": 3.0},
             {"w": "there", "start": 3.0, "end": 4.0}, {"w": "next", "start": 4.3, "end": 5.0}]
    a, b, ws = window(words, 2.0, 4.1, 0.15, 0.6)
    json.dump({"argv": sys.argv[1:], "a": a, "b": b, "words": [w["w"] for w in ws]}, open("result.json", "w"))
'''
        ffmpeg = '''
import struct, sys
SR = 16000
def level(t):
    return 8000 if (1.0 <= t < 1.5 or 2.30 <= t < 4.00 or 4.25 <= t < 5.0) else 0
out = sys.stdout.buffer
for i in range(SR * 6):
    out.write(struct.pack("<h", level(i / SR) * (1 if i % 2 else -1)))
'''
        with tempfile.TemporaryDirectory() as d:
            for name, text in (("shorts.py", shorts), ("fake_ffmpeg.py", ffmpeg)):
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            bin_dir = os.path.join(d, "bin")
            video = os.path.join(d, "video")
            os.makedirs(bin_dir)
            os.makedirs(video)
            executable(bin_dir, "ffmpeg", python_script=os.path.join(d, "fake_ffmpeg.py"))
            with open(os.path.join(video, "narration.mp3"), "wb") as fh:
                fh.write(b"not really audio")
            env = {"PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""), "AGENT_PLAYBOOK_SHORTS_DIR": d}
            r = run("render_short.py", video, "--from", "2.0", "--to", "4.1", "--hook", "Hi", env=env, cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("edges 'hello..there'", r.stdout)
            with open(os.path.join(d, "result.json"), encoding="utf-8") as fh:
                got = json.load(fh)
            self.assertEqual(got["argv"], [video, "--from", "2.0", "--to", "4.1", "--hook", "Hi"])
            self.assertEqual(got["words"], ["hello", "there"])
            # "hello" is timed at 2.50 but really starts at 2.30: the cut moves to 40 ms before the real onset
            self.assertAlmostEqual(got["a"], 2.26, places=2)
            # shorts.py's own end (4.60) would catch "next", which really starts at 4.25: pulled in to 4.21
            self.assertAlmostEqual(got["b"], 4.21, places=2)

    @needs("numpy")
    def test_words_timed_past_the_end_of_the_audio_do_not_crash(self):
        import numpy
        import render_short
        render_short.np = numpy
        db = numpy.full(100, -90.0)  # one second of silence; words.json says the speech goes on to 3.5 s
        words = [{"w": "a", "start": 0.2, "end": 0.5}, {"w": "b", "start": 3.0, "end": 3.5}]
        a, b = render_short.refine(words, 1, 1, 2.85, 4.1, db)
        self.assertTrue(0.0 <= a <= 2.85 and b == 4.1, (a, b))

    @needs("numpy")
    def test_missing_narration_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "shorts.py"), "w") as fh:
                fh.write("def window(*a):\n    pass\ndef main():\n    pass\n")
            video = os.path.join(d, "video")
            os.makedirs(video)
            r = run("render_short.py", "--shorts-dir", d, video)
            self.assertEqual(r.returncode, 1)
            self.assertIn("no narration.mp3", r.stderr)

    def test_missing_numpy_exits_3(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "shorts.py"), "w") as fh:
                fh.write("def main():\n    pass\n")
            r = run("render_short.py", "--shorts-dir", d, d, env={"AGENT_PLAYBOOK_TEST_HIDE_MODULES": "numpy"})
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertIn("needs numpy: pip install numpy", r.stderr)



if __name__ == "__main__":
    unittest.main()
