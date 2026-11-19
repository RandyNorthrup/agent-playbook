"""Test guard, loaded into script subprocesses through PYTHONPATH (Python imports sitecustomize at start-up).

AGENT_PLAYBOOK_TEST_NET_MARKER=<file>   any attempt to open a network connection or resolve a name appends a
                                    line to the file and raises, so a test can assert "no network call".
AGENT_PLAYBOOK_TEST_HIDE_MODULES=a,b    importing these modules raises ImportError, as if they weren't
                                    installed, to exercise a script's "needs X: pip install X" exit.
"""
import os
import sys

_marker = os.environ.get("AGENT_PLAYBOOK_TEST_NET_MARKER")
if _marker:
    import socket

    def _blocked(*args, **kwargs):
        with open(_marker, "a", encoding="utf-8") as fh:
            fh.write("network call attempted: %r\n" % (args[-1:],))
        raise OSError("network blocked by the test guard")

    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked

_hide = set(filter(None, os.environ.get("AGENT_PLAYBOOK_TEST_HIDE_MODULES", "").split(",")))
if _hide:
    class _Hide:
        @staticmethod
        def find_spec(fullname, path=None, target=None):
            if fullname.split(".")[0] in _hide:
                raise ImportError("hidden by the test guard: " + fullname)
            return None

    sys.meta_path.insert(0, _Hide())
