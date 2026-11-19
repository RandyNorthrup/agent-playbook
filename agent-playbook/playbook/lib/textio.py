"""File primitives: strict JSON, a cross-process lock, atomic replace and locked appends.

Every file the engine writes goes through here, so the bytes are the same on Windows, macOS and Linux:
UTF-8, LF line endings, nothing half-written.
"""
import json
import os
import re
import sys
import threading
import time
import uuid

try:  # Windows
    import msvcrt
except ImportError:  # macOS, Linux
    msvcrt = None
    import fcntl


class LockTimeout(OSError):
    """Another process held the lock for too long."""


def safe_stdio():
    """Print UTF-8 with LF endings whatever the console or pipe would pick (Windows defaults to a code page that
    cannot print an emoji, and a crash there loses the output and the exit code)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", newline="\n", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def split_lines(text):
    """The lines of a JSON Lines file. Only LF ends a record: `str.splitlines()` also breaks on U+2028, U+2029
    and U+0085, which JSON keeps unescaped inside strings, so a post that contains one would be cut in two."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _no_constant(name):
    raise ValueError("%s is not valid JSON" % name)


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key %r" % (k,))
        out[k] = v
    return out


MAX_DEPTH = 64
# a whole string (skipped: brackets inside it do not nest), or one bracket. A string that never closes runs to the
# end of the text, so a match never fails once started and the scan stays linear on hostile input.
_TOKEN_RE = re.compile(r'"[^"\\]*(?:\\(?:.|\Z)[^"\\]*)*(?:"|\Z)|[\[\]{}]', re.S)


def _depth(text, limit=None):
    """The deepest nesting of brackets and braces in JSON text, ignoring those inside strings. With `limit`, it
    stops counting as soon as the nesting goes past it."""
    deepest = depth = 0
    for m in _TOKEN_RE.finditer(text):
        ch = m.group(0)
        if ch in "[{":
            depth += 1
            if depth > deepest:
                deepest = depth
                if limit is not None and deepest > limit:
                    break
        elif ch in "]}":
            depth -= 1
    return deepest


def _too_deep(text):
    # fewer brackets than the limit cannot nest past it: the usual case for a log line, so no scan
    if text.count("[") + text.count("{") <= MAX_DEPTH:
        return False
    return _depth(text, MAX_DEPTH) > MAX_DEPTH


def loads(data):
    """Parse JSON strictly: UTF-8, no NaN or Infinity, no duplicate keys. Raises ValueError with a plain message."""
    if isinstance(data, (bytes, bytearray)):
        try:
            data = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise ValueError("not UTF-8 (%s)" % e.reason)
    elif isinstance(data, str) and data.startswith("\ufeff"):
        data = data[1:]
    if _too_deep(data):  # checked here: whether the parser itself refuses deep nesting depends on the Python build
        raise ValueError("nested too deeply")
    try:
        return json.loads(data, parse_constant=_no_constant, object_pairs_hook=_no_duplicates)
    except RecursionError:
        raise ValueError("nested too deeply")
    except ValueError as e:
        raise ValueError("not valid JSON: %s" % e)


def dumps(obj, indent=None):
    """JSON text with non-ASCII kept readable. Raises ValueError on NaN or Infinity."""
    return json.dumps(obj, ensure_ascii=False, indent=indent, allow_nan=False)


def _retry(fn, tries=60):
    """Run fn, waiting out the sharing errors Windows raises while another process has the file open."""
    delay = 0.005
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 0.1)


def read_bytes(path):
    """The file's bytes (FileNotFoundError when it is missing)."""
    def go():
        with open(str(path), "rb") as f:
            return f.read()
    return _retry(go)


TEMP_RE = re.compile(r"^\..+\.\d+\.[0-9a-f]{8}\.tmp$")  # the private temp files atomic_write makes


def _sync_folder(folder):
    """Make a rename in `folder` survive a power cut (POSIX; Windows has no way to open a folder for this)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path, data):
    """Replace `path` with `data` (bytes) in one step: readers see the old file or the new one, never a part."""
    path = str(path)
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, ".%s.%d.%s.tmp" % (os.path.basename(path), os.getpid(), uuid.uuid4().hex[:8]))
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _retry(lambda: os.replace(tmp, path))
        _sync_folder(folder)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def sweep_temp(folder, older_than=3600.0):
    """Remove temp files a killed process left in `folder` (a crash between writing and swapping one in). Only
    files named like atomic_write's and older than `older_than` seconds, so a write in progress is never touched.
    Returns the names removed."""
    gone = []
    try:
        names = os.listdir(str(folder))
    except OSError:
        return gone
    cutoff = time.time() - older_than
    for name in names:
        if not TEMP_RE.match(name):
            continue
        p = os.path.join(str(folder), name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.unlink(p)
                gone.append(name)
        except OSError:
            pass
    return gone


def write_text(path, text):
    """Write text as UTF-8 with LF line endings on every OS."""
    atomic_write(path, text.replace("\r\n", "\n").encode("utf-8"))


_threads = {}
_threads_guard = threading.Lock()


class FileLock(object):
    """A lock shared by every thread and process that opens the same lock file. Not re-entrant.

        with FileLock(path):
            ...read, decide, write...
    """

    def __init__(self, path, timeout=20.0):
        self.path = os.path.abspath(str(path))
        self.timeout = timeout
        self._fh = None
        self._thread_lock = None

    def __enter__(self):
        # one deadline for both waits, on a clock that a system time change cannot move
        deadline = time.monotonic() + self.timeout
        with _threads_guard:
            tl = _threads.setdefault(os.path.normcase(self.path), threading.Lock())
        if not tl.acquire(timeout=max(self.timeout, 0)):
            raise LockTimeout("timed out waiting for " + self.path)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fh = open(self.path, "a+b")
            # The operating system drops these locks when the process that holds them exits, however it exits,
            # so a crash never leaves a stale lock behind: the lock file itself staying on disk means nothing.
            while True:
                try:
                    if msvcrt:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        fh.close()
                        raise LockTimeout("timed out waiting for " + self.path)
                    time.sleep(0.01)
            self._fh = fh
            self._thread_lock = tl
        except BaseException:
            tl.release()
            raise
        return self

    def __exit__(self, *exc):
        fh, tl = self._fh, self._thread_lock
        self._fh = self._thread_lock = None
        try:
            if fh:
                try:
                    if msvcrt:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                finally:
                    fh.close()
        finally:
            if tl:
                tl.release()
        return False


def append_lines(path, lines, lock_path=None):
    """Append whole lines (text without newlines) to a log in one write, under a lock. LF endings on every OS."""
    data = "".join(line + "\n" for line in lines).encode("utf-8")
    if not data:
        return
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def go():
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            # a log whose last line lost its newline must not swallow the next record
            size = os.fstat(fd).st_size
            lead = b""
            if size:
                with open(path, "rb") as r:
                    r.seek(size - 1)
                    if r.read(1) not in (b"\n",):
                        lead = b"\n"
            view = memoryview(lead + data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)

    if lock_path is None:
        _retry(go)
    else:
        with FileLock(lock_path):
            _retry(go)
