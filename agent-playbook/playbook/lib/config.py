"""Settings shared by the server and the launcher."""
import os

APP = "agent-playbook"
DEFAULT_PORT = 47321
PORT_ENV = "AGENT_PLAYBOOK_PORT"


def port(value=None):
    """The port to use: an explicit value, else $AGENT_PLAYBOOK_PORT, else 47321. Raises ValueError for a bad one."""
    raw = value if value not in (None, "") else os.environ.get(PORT_ENV, "")
    if raw in (None, ""):
        return DEFAULT_PORT
    try:
        n = int(str(raw).strip())
    except ValueError:
        raise ValueError("the port must be a number, not %r" % (raw,))
    if not 1024 <= n <= 65535:
        raise ValueError("the port must be between 1024 and 65535, not %d" % n)
    return n
