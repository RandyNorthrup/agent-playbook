"""Shared helpers for the playbook tests. Every test works on a throwaway copy of tests/fixtures/playbook,
never on the real store."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAYBOOK = ROOT / "agent-playbook" / "playbook"
FIXTURE = ROOT / "tests" / "fixtures" / "playbook"

if str(PLAYBOOK) not in sys.path:
    sys.path.insert(0, str(PLAYBOOK))

import lib  # noqa: E402,F401
from lib import schema, store as storelib  # noqa: E402


class PlaybookCase(unittest.TestCase):
    """setUp gives self.root (a fresh copy of the fixture playbook) and self.store."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="pbtest-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.root = self.tmp / "playbook"
        shutil.copytree(str(FIXTURE), str(self.root))
        self.store = storelib.Store(self.root)

    # ------------------------------------------------------------ files
    def doc(self, col, doc_id):
        with open(str(self.root / "store" / col / (doc_id + ".json")), encoding="utf-8") as fh:
            return json.load(fh)

    def raw(self, col, doc_id):
        return (self.root / "store" / col / (doc_id + ".json")).read_bytes()

    def write_json(self, name, obj):
        path = self.tmp / name
        with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh)
        return str(path)

    def versions(self, col, doc_id):
        d = self.root / "history" / col / doc_id
        return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []

    def changes(self):
        f = self.root / "history" / "changes.jsonl"
        if not f.exists():
            return []
        return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]

    # ------------------------------------------------------------ command lines
    def _run(self, script, args, stdin=None):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(PLAYBOOK / script), "--root", str(self.root)] + [str(a) for a in args],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=120, input=stdin)

    def pb(self, *args):
        return self._run("pb.py", args)

    def audit(self, *args):
        return self._run("audit.py", args)

    def assertRan(self, r, code=0):
        self.assertEqual(r.returncode, code, "exit %s\nstdout: %s\nstderr: %s" % (r.returncode, r.stdout, r.stderr))
        self.assertNotIn("Traceback", r.stderr)


def good_flow():
    """A small valid flow document."""
    return {"name": "Sample", "group": "Platforms", "icon": "map", "summary": "A sample.", "order": 5,
            "nodes": [{"id": "s-start", "kind": "start", "title": "Start here", "x": 0, "y": 0},
                      {"id": "s-step", "kind": "step", "title": "Do the thing", "body": "Do it.", "x": 320, "y": 0},
                      {"id": "s-end", "kind": "output", "title": "Done", "x": 640, "y": 0}],
            "edges": [{"from": "s-start", "to": "s-step", "type": "flow"}, {"from": "s-step", "to": "s-end", "type": "flow"}]}


def messages(findings):
    return ["%s: %s" % (p, m) for _, p, m in findings]


def validate(col, doc_id, doc):
    return messages(schema.validate(col, doc_id, doc))
