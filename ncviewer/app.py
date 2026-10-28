"""The `ncviewer` command: a window in the browser for looking at what a
simulation's output holds.

As OceanVal's window (oceanval/app.py), it is a server on 127.0.0.1 with a
token that only the page it opens is given, a page that follows the app's
state by long-polling it, and it quits when the page has gone (but never
while a tab is only hidden). Page 1 asks where the files are; Continue reads
what they hold (ncviewer.catalogue); page 2 is the viewer.
"""

import argparse
import contextlib
import json
import os
import secrets
import select
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import __version__, browser
from . import files as found_files

# a page not heard from for this long has gone; one that said its tab is hidden, after HIDDEN_PAGE_TIMEOUT
PAGE_TIMEOUT = 15
HIDDEN_PAGE_TIMEOUT = 30 * 60
# how long a request for the state is held when nothing changes
STATE_WAIT = 20
# the most folders listed as the directory box is typed in
MAX_FOLDERS = 40


def _bokeh_js():
    """The path of BokehJS of the bokeh installed, served to the page so it
    needs no internet."""
    from bokeh.util import paths

    # bokehjsdir before bokeh 3.4
    folder = paths.bokehjs_path() if hasattr(paths, "bokehjs_path") else paths.bokehjsdir()
    return os.path.join(str(folder), "js", "bokeh.min.js")


def render_page(state):
    """The page, with the shared look (gui.css, gui.js) and its first state
    put in it, as OceanVal's recipes_gui.render_page does."""
    data = resources.files("ncviewer") / "data"
    page = (data / "app.html").read_text(encoding="utf-8")
    state_json = (
        json.dumps(state).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )
    return (
        page.replace("/*__NCVIEWER_GUI_CSS__*/", (data / "gui.css").read_text(encoding="utf-8"))
        .replace("/*__NCVIEWER_GUI_JS__*/", (data / "gui.js").read_text(encoding="utf-8"))
        .replace("__NCVIEWER_STATE__", state_json)
    )


def folders_like(typed):
    """Up to MAX_FOLDERS directories whose path starts as typed does, for the
    directory box's suggestions. Only one directory is listed. Never raises."""
    typed = typed.strip()
    if not typed:
        return []
    expanded = os.path.expanduser(typed)
    parent, start = (expanded, "") if expanded.endswith(os.sep) else os.path.split(expanded)
    found = []
    with contextlib.suppress(OSError):
        with os.scandir(parent or os.sep) as entries:
            for entry in sorted(entries, key=lambda entry: entry.name):
                if entry.name.startswith(".") or not entry.name.startswith(start):
                    continue
                with contextlib.suppress(OSError):
                    if entry.is_dir():
                        found.append(os.path.join(parent, entry.name) + os.sep)
                if len(found) >= MAX_FOLDERS:
                    break
    return found


class App:
    """What the window shows, and what it has been asked to do."""

    def __init__(self, simdir="", ndown=""):
        self.token = secrets.token_urlsafe(24)
        self.version = 0
        self._changed = threading.Condition()
        self.view = "files"
        self.setup = {"simdir": simdir, "ndown": str(ndown), "exclude": "", "require": ""}
        self.scan = None
        self.error = None
        self.catalogue = None
        self.catalogue_number = 0
        self._scan_number = 0
        # page -> when it was last heard from, whether its tab is hidden, and its requests waiting
        self.pages = {}
        self.closed = threading.Event()
        self.url = None
        self._server = None
        # page -> the number of its latest request for a plot (an earlier one still reading is stopped)
        self._plot_numbers = {}

    # ---- the state the page follows ----

    def _notify(self):
        with self._changed:
            self.version += 1
            self._changed.notify_all()

    def state(self, version=None, timeout=STATE_WAIT, connected=lambda: True):
        """The state, once it is no longer version (at most timeout seconds)."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while (
                version is not None
                and int(version) == self.version
                and not self.closed.is_set()
                and time.monotonic() < deadline
                and connected()
            ):
                self._changed.wait(min(1.0, max(0.0, deadline - time.monotonic())))
            return {
                "version": self.version,
                "token": self.token,
                "ncviewer_version": __version__,
                "view": self.view,
                "setup": dict(self.setup),
                "scan": dict(self.scan) if self.scan else None,
                "error": self.error,
                "catalogue_number": self.catalogue_number if self.catalogue else None,
                "coast": _has_geoviews(),
                "closed": self.closed.is_set(),
            }

    # ---- page 1 ----

    def probe(self, simdir, ndown, exclude="", require=""):
        found = found_files.probe(simdir, ndown, exclude, require)
        found["folders"] = folders_like(simdir) if found["directory"] != "found" or simdir.endswith(os.sep) else []
        return found

    def start_scan(self, form):
        """Read what the files hold, in a thread, then show the viewer."""
        setup = {key: str(form.get(key, "")).strip() for key in ("simdir", "ndown", "exclude", "require")}
        found = found_files.probe(**setup)
        if found["directory"] != "found":
            return {"ok": False, "error": "That directory cannot be found."}
        if found["ndown"] is None:
            return {"ok": False, "error": "Say how many directories down the files are (0 or more)."}
        if not found["files"]:
            return {"ok": False, "error": "There are no netCDF files there to look at."}
        with self._changed:
            self._scan_number += 1
            number = self._scan_number
            self.setup = setup
            self.view = "scanning"
            self.error = None
            self.scan = {"phase": "files", "done": 0, "total": found["files"]}
        self._notify()
        threading.Thread(target=self._scan, args=(number, setup), daemon=True).start()
        return {"ok": True}

    def _scan(self, number, setup):
        from . import catalogue

        def progress(**news):
            if number == self._scan_number:
                self.scan = news
                self._notify()

        try:
            found = catalogue.scan(
                setup["simdir"], setup["ndown"], setup["exclude"], setup["require"],
                progress=progress, stopped=lambda: number != self._scan_number,
            )
        except catalogue.Stopped:
            return
        except Exception as error:  # noqa: BLE001 - told to the page
            traceback.print_exc()
            if number == self._scan_number:
                self.view = "files"
                self.error = f"The files could not be read: {error}"
                self._notify()
            return
        if number != self._scan_number:
            return
        old, self.catalogue = self.catalogue, found
        if old is not None:
            old.close()
        self.catalogue_number += 1
        self.view = "viewer"
        self.scan = None
        self._notify()

    def back(self):
        """Back to page 1 (a scan still going is stopped)."""
        self._scan_number += 1
        self.view = "files"
        self.scan = None
        self.error = None
        self._notify()
        return {"ok": True}

    # ---- page 2 ----

    def summary(self):
        if self.catalogue is None:
            return {"ok": False, "error": "Nothing has been read yet."}
        return {"ok": True, "number": self.catalogue_number, **self.catalogue.summary()}

    def plot(self, query, page=""):
        """A plot, as the page asks for it: {"ok", "item" (a Bokeh json_item),
        "notes", and where it is ("cell", "position", "when", "file")}."""
        from . import catalogue, plotting

        found = self.catalogue
        if found is None:
            return {"ok": False, "error": "Nothing has been read yet."}
        number = self._plot_numbers[page] = self._plot_numbers.get(page, 0) + 1

        def stopped():
            return self._plot_numbers.get(page) != number or self.catalogue is not found

        kind = query.get("kind", "map")
        stream, name = query.get("stream", "0"), query.get("var", "")
        step, level = int(query.get("step") or 0), int(query.get("level") or 0)
        colour = {
            "cmap": query.get("cmap") or None,
            "vmin": _number(query.get("vmin")),
            "vmax": _number(query.get("vmax")),
            "log": query.get("log") == "1",
        }
        try:
            about = found.about(stream, name)
            cell = [int(part) for part in query["cell"].split(",")] if query.get("cell") else None
            reply = {"ok": True, "kind": kind, "notes": []}
            if kind in ("series", "profile", "hovmoller") and cell is None:
                cell = _middle(about)
                reply["notes"].append("Click on the map to choose the cell. The middle of the grid is shown.")
            if cell is not None:
                reply["cell"] = cell
                reply["position"] = found.cell_position(stream, name, cell)
            where = _where(about, cell, reply.get("position"))
            if kind == "map":
                data, when = found.field(stream, name, step, level)
                reply["item"], notes, reply["clim"], reply["axes"] = plotting.map_plot(data, about, name, when, coast=query.get("coast") == "1", **colour)
                reply["notes"] += notes
                reply["when"] = when
            elif kind == "line":
                data, when = found.field(stream, name, step, level)
                reply["item"] = plotting.line_plot(data, about, name, when)
                reply["when"] = when
            elif kind == "series":
                times, values = found.series(stream, name, cell, level, stopped=stopped)
                if about["vertical"]:
                    where += f", {_level_text(about, level)}"
                reply["item"] = plotting.series_plot(times, values, about, name, where)
            elif kind == "profile":
                levels, values, when = found.profile(stream, name, cell, step)
                reply["item"] = plotting.profile_plot(levels, values, about, name, where, when)
                reply["when"] = when
            elif kind == "hovmoller":
                times, values = found.series(stream, name, cell, all_levels=True, stopped=stopped)
                reply["item"] = plotting.hovmoller_plot(times, values, about, name, where, **colour)
            else:
                return {"ok": False, "error": f"There is no plot called {kind}."}
            if kind in ("map", "line", "profile"):
                record = found.streams[int(stream)]
                _, index, _ = record["steps"][min(max(step, 0), len(record["steps"]) - 1)]
                reply["file"] = os.path.relpath(record["files"][index], found.simdir)
            return reply
        except catalogue.Stopped:
            return {"ok": False, "stopped": True}
        except Exception as error:  # noqa: BLE001 - told to the page
            traceback.print_exc()
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    def cell_at(self, query):
        """The cell nearest a click on the map, and where it is."""
        found = self.catalogue
        if found is None:
            return {"ok": False, "error": "Nothing has been read yet."}
        try:
            stream, name = query.get("stream", "0"), query.get("var", "")
            cell = found.nearest_cell(
                stream, name, float(query["x"]), float(query["y"]), by_index=query.get("axes") == "index"
            )
            return {"ok": True, "cell": cell, "position": found.cell_position(stream, name, cell)}
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)}

    # ---- pages coming and going ----

    def touch(self, page, waiting=0):
        if not page:
            return
        entry = self.pages.setdefault(page, {"seen": time.monotonic(), "hidden": False, "waiting": 0})
        entry["seen"] = time.monotonic()
        entry["waiting"] += waiting

    def page_visibility(self, page, hidden):
        self.touch(page)
        if page in self.pages:
            self.pages[page]["hidden"] = bool(hidden)
        return {"ok": True}

    def _alive(self, entry, now):
        limit = HIDDEN_PAGE_TIMEOUT if entry["hidden"] else PAGE_TIMEOUT
        return entry["waiting"] > 0 or now - entry["seen"] < limit

    def _watch(self):
        """Quit once every page there was has gone."""
        while not self.closed.wait(1):
            now = time.monotonic()
            if self.pages and not any(self._alive(entry, now) for entry in list(self.pages.values())):
                print("The ncviewer window was closed.", flush=True)
                self.close()

    def start(self, port=0):
        handler = type("Handler", (_Handler,), {"app": self})
        try:
            self._server = _Server(("127.0.0.1", port), handler)
        except OSError:
            self._server = _Server(("127.0.0.1", 0), handler)
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/?token={self.token}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        return self.url

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        self._notify()
        if self.catalogue is not None:
            self.catalogue.close()
        if self._server is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()


def _has_geoviews():
    try:
        import importlib.util

        return all(importlib.util.find_spec(name) is not None for name in ("geoviews", "cartopy"))
    except (ImportError, ValueError):
        return False


def _number(text):
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


def _middle(about):
    return [size // 2 for dim, size in zip(about["dims"], about["shape"]) if dim in about["horizontal"]]


def _level_text(about, level):
    values = about.get("level_values") or []
    level = min(max(int(level), 0), len(values) - 1) if values else 0
    value = values[level] if values else level
    units = f" {about['level_units']}" if about.get("level_units") else ""
    return f"{about['vertical']} {value:g}{units}" if isinstance(value, float) else f"level {value}"


def _where(about, cell, position):
    if cell is None:
        return ""
    if position:
        lon, lat = position
        return f", at {abs(lon):.3f}°{'W' if lon < 0 else 'E'} {abs(lat):.3f}°{'S' if lat < 0 else 'N'}"
    return ", at cell " + ", ".join(f"{dim}={index}" for dim, index in zip(about["horizontal"], cell))


# ---- the server (as OceanVal's recipes_gui._Server/_Handler) ----

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class _Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, *args):
        pass

    def _reply(self, status, body, content_type):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _reply_json(self, value, status=200):
        self._reply(status, json.dumps(value).encode("utf-8"), "application/json")

    def _connected(self):
        """Whether the browser is still waiting for this request's answer."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return not ready or bool(self.connection.recv(1, 0x2))  # MSG_PEEK
        except OSError:
            return False

    def _authorised(self, given):
        return bool(given) and secrets.compare_digest(str(given), self.app.token)

    def do_GET(self):
        url = urlparse(self.path)
        query = {key: values[-1] for key, values in parse_qs(url.query).items()}
        if url.path == "/bokeh.min.js":
            # the library the plots are drawn by, which is the same for anyone
            with open(_bokeh_js(), "rb") as handle:
                self._reply(200, handle.read(), "text/javascript; charset=utf-8")
            return
        if not self._authorised(query.get("token")):
            self._reply(403, b"Forbidden: open the link ncviewer printed.", "text/plain")
            return
        app = self.app
        page = query.get("page", "")
        if url.path == "/":
            body = render_page(app.state()).encode("utf-8")
            self._reply(200, body, "text/html; charset=utf-8")
        elif url.path == "/api/state":
            app.touch(page, waiting=1)
            try:
                state = app.state(query.get("version"), connected=self._connected)
            finally:
                app.touch(page, waiting=-1)
            self._reply_json(state)
        elif url.path == "/api/probe":
            app.touch(page)
            self._reply_json(app.probe(
                query.get("simdir", ""), query.get("ndown", ""), query.get("exclude", ""), query.get("require", "")
            ))
        elif url.path == "/api/catalogue":
            self._reply_json(app.summary())
        elif url.path == "/api/plot":
            self._reply_json(app.plot(query, page))
        elif url.path == "/api/cell":
            self._reply_json(app.cell_at(query))
        else:
            self._reply(404, b"Not found", "text/plain")

    def do_POST(self):
        if not self._authorised(self.headers.get("X-NCViewer-Token")):
            self._reply(403, b"Forbidden", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._reply_json({"ok": False, "error": "That request could not be read."}, 400)
            return
        app = self.app
        path = urlparse(self.path).path
        if path == "/api/continue":
            self._reply_json(app.start_scan(body))
        elif path == "/api/back":
            self._reply_json(app.back())
        elif path == "/api/visibility":
            self._reply_json(app.page_visibility(body.get("page", ""), body.get("hidden")))
        else:
            self._reply(404, b"Not found", "text/plain")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ncviewer", description="Look at what a simulation's output holds, in your browser."
    )
    parser.add_argument("directory", nargs="?", default="", help="the directory of the simulation's output")
    parser.add_argument("--ndown", default="", help="how many directories below it the files are")
    parser.add_argument("--port", type=int, default=None, help="the port to serve the window on")
    parser.add_argument("--no-browser", action="store_true", help="print the window's link instead of opening it")
    parser.add_argument("--version", action="version", version=f"ncviewer {__version__}")
    args = parser.parse_args(argv)
    if args.no_browser:
        os.environ["NCVIEWER_BROWSER"] = "0"
    simdir = os.path.abspath(os.path.expanduser(args.directory)) if args.directory else ""
    app = App(simdir=simdir, ndown=args.ndown)
    opens = browser.can_open_browser()
    port = args.port if args.port is not None else (browser.SSH_PORT if browser.over_ssh() and not opens else 0)
    url = app.start(port)
    port = int(url.split(":")[2].split("/")[0])
    if opens and browser.open_browser(url):
        print(f"ncviewer is open in your browser. If it is not, open:\n  {url}", flush=True)
    else:
        print(f"Open this link in your browser to use ncviewer:\n  {url}", flush=True)
        print(browser.remote_hint(port), flush=True)
    print("Press Ctrl+C to stop ncviewer.", flush=True)
    try:
        while not app.closed.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\nStopping ncviewer.", flush=True)
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
