"""Shared pieces for the media tool tests: run a tool, the needs() gate, a stand-in codex and a quota fixture.

Nothing here generates an image with a real service: every tool subprocess gets a CODEX_BIN and a CODEX_HOME
that point nowhere unless the test passes its own stand-in, so a test that forgets one fails instead of
spending the owner's quota.

AGENT_PLAYBOOK_REQUIRE_MEDIA=1 (one CI row sets it) turns a test that would skip for want of Pillow, numpy or
ffmpeg into a failure, so the other rows may skip honestly.
"""
import datetime
import functools
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_scripts import GUARD, ROOT, executable  # noqa: E402

MEDIA = os.path.join(ROOT, "agent-playbook", "media")
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "media")
REQUIRE = os.environ.get("AGENT_PLAYBOOK_REQUIRE_MEDIA", "") not in ("", "0")
HAVE = {"Pillow": importlib.util.find_spec("PIL") is not None,
        "numpy": importlib.util.find_spec("numpy") is not None,
        "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))}
NOWHERE = os.path.join(tempfile.gettempdir(), "agent-playbook-tests-nothing-here")
CLEARED = ("CODEX_BIN", "CODEX_HOME", "AGENT_PLAYBOOK_EXPLAINER_DIR", "AGENT_PLAYBOOK_FFMPEG", "AGENT_PLAYBOOK_FFPROBE", "AGENT_PLAYBOOK_FONT_DIR")

if MEDIA not in sys.path:
    sys.path.insert(0, MEDIA)


def needs(*names):
    """Decorator for a test or a test class that needs Pillow, numpy or ffmpeg: skipped when one is missing,
    or failed when AGENT_PLAYBOOK_REQUIRE_MEDIA=1."""
    missing = [n for n in names if not HAVE[n]]
    if not missing:
        return lambda obj: obj
    reason = "needs " + " and ".join(missing)
    if not REQUIRE:
        return unittest.skip(reason)

    def decorate(obj):
        def fail(self, *args, **kwargs):
            self.fail("%s, and AGENT_PLAYBOOK_REQUIRE_MEDIA=1 forbids skipping" % reason)
        if isinstance(obj, type):
            obj.setUp = fail
            return obj
        return functools.wraps(obj)(fail)
    return decorate


def run(script, *args, env=None, cwd=None, stdin=None, marker=None, timeout=180):
    """Run a media tool offline. CODEX_BIN and CODEX_HOME point nowhere unless `env` sets them."""
    e = dict(os.environ, PYTHONIOENCODING="utf-8", AGENT_PLAYBOOK_OFFLINE="1", PYTHONPATH=GUARD)
    for name in CLEARED:
        e.pop(name, None)
    e["CODEX_BIN"] = os.path.join(NOWHERE, "codex")
    e["CODEX_HOME"] = os.path.join(NOWHERE, "codex-home")
    if marker:
        e["AGENT_PLAYBOOK_TEST_NET_MARKER"] = marker
    e.update(env or {})
    return subprocess.run([sys.executable, os.path.join(MEDIA, script), *args], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", env=e, cwd=cwd, timeout=timeout)


def fixture_path(*parts):
    return os.path.join(FIXTURES, *parts)


def read_fixture(*parts):
    with open(fixture_path(*parts), encoding="utf-8") as fh:
        return fh.read()


# A stand-in for the codex binary. Every start writes a file of its own into the folder FAKE_CODEX_LOG (two
# starts at once must not lose one), so a test can count the runs or assert there were none. It "generates"
# by drawing a PNG into this run's own thread folder under CODEX_HOME/generated_images, the way the real one
# does.
#   FAKE_CODEX_MODE        3x2 (1536x1024, the default), wide (1536x864), small (320x180), black, checker
#                          (an opaque painted checkerboard), alpha (a real cut-out), crash (no image, exit 1)
#   FAKE_CODEX_FAIL_ON     a run whose prompt contains this text makes no image
#   FAKE_CODEX_FAIL_FIRST  the first N runs make no image
FAKE_CODEX = r'''
import json, os, sys, time, uuid
prompt = sys.stdin.read()
log = os.environ["FAKE_CODEX_LOG"]
os.makedirs(log, exist_ok=True)
with open(os.path.join(log, uuid.uuid4().hex + ".json"), "w", encoding="utf-8") as fh:
    json.dump({"at": time.time(), "argv": sys.argv[1:], "prompt": prompt}, fh)
runs = len(os.listdir(log))
mode = os.environ.get("FAKE_CODEX_MODE", "3x2")
fail_on = os.environ.get("FAKE_CODEX_FAIL_ON", "")
if mode == "crash" or (fail_on and fail_on in prompt) or runs <= int(os.environ.get("FAKE_CODEX_FAIL_FIRST", "0")):
    sys.stderr.write("usage limit reached\n")
    sys.exit(1)
thread = "thread-" + uuid.uuid4().hex
print(json.dumps({"type": "thread.started", "thread_id": thread}))
folder = os.path.join(os.environ["CODEX_HOME"], "generated_images", thread)
os.makedirs(folder)
from PIL import Image, ImageDraw
if mode == "black":
    im = Image.new("RGB", (1536, 864), (0, 0, 0))
elif mode == "checker":
    im = Image.new("RGB", (1024, 1024), (204, 204, 204))
    d = ImageDraw.Draw(im)
    for y in range(0, 1024, 16):
        for x in range(0, 1024, 16):
            if (x // 16 + y // 16) % 2:
                d.rectangle([x, y, x + 15, y + 15], fill=(255, 255, 255))
    d.ellipse([312, 312, 712, 712], fill=(40, 90, 200))
elif mode == "alpha":
    im = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse([312, 312, 712, 712], fill=(40, 90, 200, 255))
else:
    w, h = {"3x2": (1536, 1024), "wide": (1536, 864), "small": (320, 180)}[mode]
    im = Image.new("RGB", (w, h), (250, 250, 244))
    d = ImageDraw.Draw(im)
    d.rectangle([w // 8, h // 8, w // 2, h // 2], outline=(30, 60, 160), width=6)
    d.ellipse([w // 2, h // 3, w - w // 8, h - h // 8], fill=(240, 200, 60))
    d.text((20, 20), "run %d" % runs, fill=(30, 60, 160))
im.save(os.path.join(folder, "ig_0001.png"))
print(json.dumps({"type": "turn.completed"}))
'''


class Codex(object):
    """A stand-in codex in `folder`: .env goes to run(env=...), .runs() are the starts it recorded."""

    def __init__(self, folder):
        script = os.path.join(folder, "fake_codex.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(FAKE_CODEX)
        self.home = os.path.join(folder, "codex-home")
        self.log = os.path.join(folder, "codex_runs")
        self.env = {"CODEX_BIN": executable(folder, "codex", python_script=script), "CODEX_HOME": self.home,
                    "FAKE_CODEX_LOG": self.log}

    def runs(self):
        """Every start of the stand-in, oldest first, as {at, argv, prompt}."""
        found = []
        for name in os.listdir(self.log) if os.path.isdir(self.log) else []:
            with open(os.path.join(self.log, name), encoding="utf-8") as fh:
                found.append(json.load(fh))
        return sorted(found, key=lambda r: r["at"])


def stamp(when):
    return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def token_count(used, when, resets_in_hours=40.0, **limits):
    """One session-file line with a rate-limit reading, in the shape Codex writes."""
    primary = {"used_percent": used, "window_minutes": 10080, "resets_at": int(when.timestamp() + resets_in_hours * 3600)}
    rate = dict({"limit_id": "codex", "limit_name": None, "primary": primary, "secondary": None,
                 "credits": {"has_credits": False, "unlimited": False, "balance": "0"}, "plan_type": "prolite"}, **limits)
    return json.dumps({"timestamp": stamp(when), "type": "event_msg",
                       "payload": {"type": "token_count", "info": None, "rate_limits": rate}})


def session_file(home, name, lines, minutes_ago=5.0, day=("2026", "11", "09")):
    """Write <home>/sessions/<day>/<name> with these lines and set its modified time."""
    folder = os.path.join(home, "sessions", *day)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(line + "\n" for line in lines))
    t = time.time() - minutes_ago * 60
    os.utime(path, (t, t))
    return path


def quota_at(home, used, minutes_ago=5.0, name="a.jsonl"):
    """A session file whose only reading says `used` percent of the weekly limit is used."""
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    meta = json.dumps({"timestamp": stamp(when), "type": "session_meta", "payload": {"id": "session-1"}})
    reply = json.dumps({"timestamp": stamp(when), "type": "response_item", "payload": {"type": "message", "role": "assistant"}})
    return session_file(home, name, [meta, token_count(used, when), reply], minutes_ago)
