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
import math
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

from . import __version__, browser, series
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
        self.setup = {"simdir": simdir, "ndown": str(ndown), "exclude": "", "require": "", "missing": ""}
        # the numbers the user says are missing, from page 1, as (low, high) ranges
        self.missing = []
        self.scan = None
        self.error = None
        self.catalogue = None
        self.catalogue_number = 0
        self._scan_number = 0
        # the time series being read, or read (see start_series), and the number of the latest
        self.series_job = None
        self._series_number = 0
        # page -> when it was last heard from, whether its tab is hidden, and its requests waiting
        self.pages = {}
        self.closed = threading.Event()
        self.url = None
        self._server = None

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
                "cores": series.default_cores(),
                "max_cores": series.max_cores(),
                "parallel_from": series.PARALLEL_FROM,
                "closed": self.closed.is_set(),
            }

    # ---- page 1 ----

    def probe(self, simdir, ndown, exclude="", require=""):
        found = found_files.probe(simdir, ndown, exclude, require)
        found["folders"] = folders_like(simdir) if found["directory"] != "found" or simdir.endswith(os.sep) else []
        return found

    def start_scan(self, form):
        """Read what the files hold, in a thread, then show the viewer."""
        setup = {key: str(form.get(key, "")).strip() for key in ("simdir", "ndown", "exclude", "require", "missing")}
        try:
            missing = parse_missing(setup["missing"])
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        found = found_files.probe(**{key: setup[key] for key in ("simdir", "ndown", "exclude", "require")})
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
            self.missing = missing
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

    def plot(self, query):
        """The map the page asks for: {"ok", "item" (a Bokeh json_item),
        "notes", "clim" (the colour scale's limits), "when", "file"}."""
        from . import plotting

        found = self.catalogue
        if found is None:
            return {"ok": False, "error": "Nothing has been read yet."}
        stream = query.get("stream", "0")
        step, level = int(query.get("step") or 0), int(query.get("level") or 0)
        try:
            names = parse_names(query.get("vars"))
            about = found.combined(stream, names)
            name = about["name"]
            data, when = found.field(stream, names, step, level, self.missing)
            n_missing = data.attrs.get("n_missing", 0)
            item, notes, clim, frame = plotting.map_plot(
                data, about, name, when,
                cmap=query.get("cmap") or None,
                vmin=_number(query.get("vmin")),
                vmax=_number(query.get("vmax")),
                log=query.get("log") == "1",
                coast=query.get("coast") == "1",
            )
            if about["units_differ"]:
                notes.append(
                    f"The units differ ({', '.join(u or 'none' for u in about['units_differ'])}), so the sum may not mean anything."
                )
            if n_missing and not data.notnull().any():
                notes.append("Every cell is missing, so there is nothing to show. Check the missing values.")
            record = found.streams[int(stream)]
            _, index, _ = record["steps"][min(max(step, 0), len(record["steps"]) - 1)]
            return {
                "ok": True, "item": item, "notes": notes, "clim": clim, "when": when, "n_missing": n_missing,
                "file": os.path.relpath(record["files"][index], found.simdir), "frame": frame,
            }
        except ValueError as error:
            # said in words for the user
            return {"ok": False, "error": str(error)}
        except Exception as error:  # noqa: BLE001 - told to the page
            traceback.print_exc()
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    def limits(self, query):
        """The range of the variable (or sum) over the whole run, so an
        animation can keep one colour scale: {"ok", "vmin", "vmax",
        "symmetric"}."""
        found = self.catalogue
        if found is None:
            return {"ok": False, "error": "Nothing has been read yet."}
        try:
            names = parse_names(query.get("vars"))
            found.combined(query.get("stream", "0"), names)
            got = found.limits(query.get("stream", "0"), names, int(query.get("level") or 0), self.missing)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        except Exception as error:  # noqa: BLE001 - told to the page
            traceback.print_exc()
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}
        if got is None:
            return {"ok": False, "error": "Every cell is missing, so there is nothing to show. Check the missing values."}
        return {"ok": True, "vmin": got[0], "vmax": got[1], "symmetric": got[2]}

    # ---- the time series at a cell the map was clicked at ----

    def start_series(self, body):
        """Find the cell clicked (body: stream, vars, step, level, x, y,
        frame, cores) and start reading its time series in a thread (one at
        a time: one still reading is stopped). Returns {"ok", "job", "cell"}
        at once, or {"ok": False, "error"}; series_status says how it goes."""
        found = self.catalogue
        if found is None:
            return {"ok": False, "error": "Nothing has been read yet."}
        self.stop_series()
        try:
            cores = parse_cores(body.get("cores"))
            stream = str(body.get("stream", "0"))
            names = parse_names(body.get("vars"))
            level = int(body.get("level") or 0)
            x, y = float(body["x"]), float(body["y"])
            if not (math.isfinite(x) and math.isfinite(y)):
                raise ValueError("That point could not be read.")
            frame = str(body.get("frame") or "lonlat")
            if frame not in ("lonlat", "mercator", "index"):
                raise ValueError("That point could not be read.")
            about = found.combined(stream, names)
            cell = found.cell(stream, names, x, y, frame, int(body.get("step") or 0), level, self.missing)
        except (KeyError, TypeError):
            return {"ok": False, "error": "That point could not be read."}
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        except Exception as error:  # noqa: BLE001 - told to the page
            traceback.print_exc()
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}
        with self._changed:
            self._series_number += 1
            number = self._series_number
            job = self.series_job = {"job": number, "done": 0, "total": 0, "finished": False, "cell": cell}
        args = (number, job, found, stream, names, level, cell, about, cores)
        threading.Thread(target=self._read_series, args=args, daemon=True).start()
        return {"ok": True, "job": number, "cell": cell}

    def _read_series(self, number, job, found, stream, names, level, cell, about, cores):
        from . import catalogue, plotting

        def current():
            return number == self._series_number and self.catalogue is found and not self.closed.is_set()

        def progress(done, total):
            job.update(done=done, total=total)

        try:
            times, values = found.series(
                stream, names, level, cell["j"], cell["i"], self.missing, cores,
                progress=progress, stopped=lambda: not current(),
            )
            record = found.streams[int(stream)]
            item, notes = plotting.series_plot(
                times, values, about, about["name"], where_text(about, cell, level), dated=record["has_times"],
            )
            if about["units_differ"]:
                notes.append(
                    f"The units differ ({', '.join(u or 'none' for u in about['units_differ'])}), so the sum may not mean anything."
                )
            reply = {"ok": True, "finished": True, "item": item, "notes": notes}
        except catalogue.Stopped:
            return
        except ValueError as error:
            reply = {"ok": False, "finished": True, "error": str(error)}
        except Exception as error:  # noqa: BLE001 - told to the page
            traceback.print_exc()
            reply = {"ok": False, "finished": True, "error": f"{type(error).__name__}: {error}"}
        job.update(reply)

    def series_status(self, job):
        """How the time series job is going: {"ok", "done", "total"} while
        it is read, then {"ok", "finished", "item", "notes"}, or {"ok":
        False, "error"}."""
        current = self.series_job
        if current is None or str(current["job"]) != str(job):
            return {"ok": False, "finished": True, "stopped": True, "error": "That time series is no longer being read."}
        return {"ok": True, **current}

    def stop_series(self):
        with self._changed:
            self._series_number += 1
            self.series_job = None
        return {"ok": True}

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


def parse_names(text):
    """The variables to map, from the page's JSON list of names. Raises
    ValueError, in words for the user, for anything else."""
    try:
        names = json.loads(text or "[]")
        assert isinstance(names, list) and all(isinstance(name, str) for name in names)
    except (ValueError, AssertionError):
        raise ValueError("The variables could not be read.") from None
    return names


def parse_cores(text):
    """The number of processes to read a time series with: a whole number
    from 1 to the cores there are. Raises ValueError, in words for the user."""
    text = str(text if text is not None else "").strip()
    if not text:
        return series.default_cores()
    most = series.max_cores()
    if not text.isdigit() or not 1 <= int(text) <= most:
        raise ValueError(f"Cores is a whole number from 1 to {most}.")
    return int(text)


def where_text(about, cell, level):
    """Where a time series is, for its title: ", at 4.250°E 51.600°N" (or the
    cell's indices) and the depth."""
    if cell.get("lon") is not None:
        lon, lat = cell["lon"], cell["lat"]
        lon = (lon + 180) % 360 - 180
        text = f", at {abs(lon):.3f}°{'W' if lon < 0 else 'E'} {abs(lat):.3f}°{'S' if lat < 0 else 'N'}"
    else:
        y_dim, x_dim = about["horizontal"][-2:]
        text = f", at {y_dim}={cell['j']}, {x_dim}={cell['i']}"
    if about.get("vertical") and about.get("levels", 1) > 1:
        values = about.get("level_values") or []
        level = min(max(int(level), 0), len(values) - 1) if values else 0
        value = values[level] if values else level
        units = f" {about['level_units']}" if about.get("level_units") else ""
        text += f", {value:g}{units}" if isinstance(value, (int, float)) else f", level {level}"
    return text


def parse_missing(text):
    """The numbers the user says are missing, from the page's JSON: rows of
    [from, to], each a number as text, and to empty for a single number.
    Returns [(low, high), ...]. Rows with nothing in them are ignored.
    Raises ValueError, in words for the user, for anything else."""
    if not text:
        return []
    try:
        rows = json.loads(text)
        assert isinstance(rows, list)
    except (ValueError, AssertionError):
        raise ValueError("The missing values could not be read.") from None
    ranges = []
    for number, row in enumerate(rows, 1):
        parts = [str(part).strip() for part in (list(row) + ["", ""])[:2]]
        if not any(parts):
            continue
        values = []
        for part in parts:
            if part == "":
                values.append(None)
                continue
            try:
                value = float(part)
            except ValueError:
                value = float("nan")
            if not math.isfinite(value):
                raise ValueError(f"Missing value {number}: {part!r} is not a number.")
            values.append(value)
        if values[0] is None:
            raise ValueError(f"Missing value {number}: fill in From, not only To.")
        ranges.append((values[0], values[0] if values[1] is None else values[1]))
    return ranges


def _number(text):
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


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
            self._reply_json(app.plot(query))
        elif url.path == "/api/limits":
            self._reply_json(app.limits(query))
        elif url.path == "/api/series":
            self._reply_json(app.series_status(query.get("job")))
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
        elif path == "/api/series":
            self._reply_json(app.start_series(body))
        elif path == "/api/series_stop":
            self._reply_json(app.stop_series())
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
