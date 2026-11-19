"""Open the Agent Playbook canvas: start the local server if it isn't running (no console window),
then open the page in the default browser. The Desktop and Start menu shortcuts run this.

  python launch.pyw [--port N] [--no-browser]     port: default 47321, or $AGENT_PLAYBOOK_PORT
Works on Windows, macOS and Linux. When the port is taken by another program, or the server stops at once, it
says why (in a message box when there is no console) and exits 1 instead of waiting."""
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from lib import config  # noqa: E402

START_WAIT = 8.0  # seconds a fresh server gets to answer


def arg_value(argv, name):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def arg_port(argv):
    return arg_value(argv, "--port")


def up(url):
    """Whether our server (and not some other program) answers on the port."""
    try:
        with urllib.request.urlopen(url + "api/ping", timeout=1) as r:
            return json.load(r).get("app") == config.APP
    except Exception:
        return False


def answers_within(url, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if up(url):
            return True
        time.sleep(0.2)
    return up(url)


def taken(port):
    """Whether something accepts connections on the port."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def tell(message):
    """Say why the canvas did not open: on the console when there is one, else in a message box (Windows)."""
    if sys.stderr is not None:
        try:
            print(message, file=sys.stderr)
            return
        except (OSError, ValueError):
            pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Agent Playbook", 0x10)
        except Exception:
            pass


def last_log_line(root):
    try:
        lines = (pathlib.Path(root) / "server.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return lines[-1].split(" ", 3)[-1] if lines else ""


def start_server(port, root=None):
    """Start server.py detached from this process, with no console window."""
    exe = sys.executable
    kw = {"cwd": str(HERE), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        pyw = pathlib.Path(sys.executable).with_name("pythonw.exe")
        if pyw.exists():
            exe = str(pyw)
        kw["creationflags"] = 0x08000000 | 0x00000008  # no window, detached
    else:
        kw["start_new_session"] = True
    args = [exe, str(HERE / "server.py"), "--port", str(port)] + (["--root", str(root)] if root else [])
    return subprocess.Popen(args, **kw)


def main(argv):
    if "-h" in argv or "--help" in argv:  # help only: never start a server or open a browser
        if sys.stdout is not None:
            print(__doc__)
        else:
            tell(__doc__)
        return
    try:
        port = config.port(arg_port(argv))
    except ValueError as e:
        tell(str(e))
        sys.exit(2)
    root = arg_value(argv, "--root")  # another playbook folder (the tests use a fixture)
    url = "http://127.0.0.1:%d/" % port
    if not up(url):
        if taken(port) and not up(url):  # a second look: our own server may have been starting
            tell("Port %d is in use by another program, so the playbook canvas cannot start there. Close that program, "
                 "or open the canvas on another port: launch.pyw --port N (or set %s)." % (port, config.PORT_ENV))
            sys.exit(1)
        proc = start_server(port, root)
        deadline = time.monotonic() + START_WAIT
        while not up(url):
            code = proc.poll()
            if code is not None:  # it stopped without serving: no point waiting out the clock
                if answers_within(url, 1.0):
                    break  # another launch started the server first, so this one could not bind: it is up
                why = last_log_line(root or HERE)
                tell("The playbook server stopped at once (exit %d)%s. See server.log in %s."
                     % (code, ": " + why if why else "", root or HERE))
                sys.exit(1)
            if time.monotonic() > deadline:
                tell("The playbook server did not start on port %d; see server.log in %s." % (port, root or HERE))
                sys.exit(1)
            time.sleep(0.1)
    if "--no-browser" not in argv:
        webbrowser.open(url)


if __name__ == "__main__":
    main(sys.argv[1:])
