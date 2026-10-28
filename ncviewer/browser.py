"""Opening the window in a browser, or telling how to reach it.

Copied from OceanVal's recipes_gui, which ncviewer does not import.
"""

import contextlib
import getpass
import os
import signal
import socket
import subprocess
import sys
import webbrowser

SSH_PORT = 8765


def over_ssh():
    """Whether this process is running in an SSH session."""
    return any(os.environ.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))


def remote_hint(port):
    """What to tell someone who has to reach the window on port from another
    machine: the command to forward it, ready to paste, if they are in an
    SSH session."""
    if not over_ssh():
        return f"On a remote machine, forward port {port} to reach it (VS Code does this for you)."
    login = f"{getpass.getuser()}@{socket.gethostname()}"
    return (
        f"You are connected over SSH, so forward port {port} first. In a new "
        "terminal on your own computer (not this one), run:\n"
        f"  ssh -L {port}:127.0.0.1:{port} {login}\n"
        "and leave it open while you use ncviewer (use your usual ssh login if "
        "it differs). VS Code does this for you."
    )


def can_open_browser():
    """Whether a graphical web browser can be opened from here.

    Without a display, webbrowser falls back on a text browser such as lynx,
    which would take over the terminal ncviewer is running in. BROWSER
    is set by VS Code's remote terminals, to open links on your own machine.
    Over SSH without it, a DISPLAY is an X11 forward (MobaXterm sets one), so
    a browser would open on the remote machine, not the one being used.
    NCVIEWER_BROWSER overrides: 1 opens one, 0 (`ncviewer --no-browser`) never.
    """
    chosen = os.environ.get("NCVIEWER_BROWSER", "").strip().lower()
    if chosen in ("1", "0"):
        return chosen == "1"
    if os.environ.get("BROWSER"):
        return True
    if over_ssh():
        return False
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _vscode_sockets():
    """The sockets VS Code's browser helper (BROWSER in its remote terminals)
    could reach a VS Code window by, to try in turn: the terminal's own, then
    the others of this user's that can be connected to, newest first, as the
    window open now is the one reloaded last. Empty if BROWSER is not the
    helper.

    VS Code brings a terminal back after its window is reloaded with the
    socket it had, which is gone by then, or still there but never answered,
    so the helper fails, or waits for ever."""
    given = os.environ.get("VSCODE_IPC_HOOK_CLI")
    if not given or os.path.basename(os.environ.get("BROWSER", "")) != "browser.sh":
        return []
    folder = os.path.dirname(given)
    others = []
    with contextlib.suppress(OSError):
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if name.startswith("vscode-ipc-") and name.endswith(".sock") and path != given:
                with contextlib.suppress(OSError):
                    others.append((os.path.getmtime(path), path))
    others.sort(reverse=True)
    return [given] + [path for _, path in others]


def _connectable(path):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1)
        try:
            connection.connect(path)
        except OSError:
            return False
    return True


def _open_in_vscode(url, sockets, tries=3, timeout=4):
    """Open url with VS Code's browser helper, through the first of sockets
    that it answers by, trying at most tries of those that can be connected
    to. Whether it was opened."""
    for path in sockets:
        if tries == 0:
            break
        if not _connectable(path):
            continue
        tries -= 1
        try:
            helper = subprocess.Popen(
                [os.environ["BROWSER"], url],
                env=dict(os.environ, VSCODE_IPC_HOOK_CLI=path),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # so the node it runs goes too if it waits too long
                start_new_session=True,
            )
        except OSError:
            continue
        try:
            if helper.wait(timeout) == 0:
                return True
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(helper.pid, signal.SIGKILL)
            helper.wait()
    return False


def open_browser(url):
    sockets = _vscode_sockets()
    if sockets:
        # VS Code opens it in the browser of the computer it runs on, and
        # forwards its port
        return _open_in_vscode(url, sockets)
    try:
        return webbrowser.open(url)
    except Exception:
        return False
