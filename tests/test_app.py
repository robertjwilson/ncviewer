import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from ncviewer import app as ncapp


@pytest.fixture
def running():
    app = ncapp.App()
    url = app.start(0)
    yield app, url.split("?")[0], app.token
    app.close()


def _get(base, path, **params):
    query = "&".join(f"{key}={urllib.request.quote(str(value))}" for key, value in params.items())
    with urllib.request.urlopen(f"{base}{path}?{query}", timeout=60) as response:
        return json.loads(response.read())


def _post(base, path, token, body):
    request = urllib.request.Request(
        f"{base}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-NCViewer-Token": token},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _wait_for(app, view, seconds=60):
    deadline = time.monotonic() + seconds
    while app.view != view and time.monotonic() < deadline:
        time.sleep(0.05)
    assert app.view == view


def _wait_for_times(app, seconds=60):
    """The times of every file are read in the background once the viewer has opened."""
    deadline = time.monotonic() + seconds
    while (app.times is not None or not app.catalogue.times_complete) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert app.catalogue.times_complete and app.times is None


def test_the_token_is_needed(running):
    app, base, token = running
    with pytest.raises(urllib.error.HTTPError) as refused:
        _get(base, "api/state")
    assert refused.value.code == 403
    with pytest.raises(urllib.error.HTTPError):
        _post(base, "api/continue", "wrong", {})
    page = urllib.request.urlopen(f"{base}?token={token}").read().decode()
    assert "__NCVIEWER_STATE__" not in page and "withChoices" in page


def test_bokeh_is_served(running):
    app, base, token = running
    assert len(urllib.request.urlopen(f"{base}bokeh.min.js").read()) > 100_000


def test_probe_then_view(running, simulation):
    app, base, token = running
    found = _get(base, "api/probe", token=token, simdir=simulation, ndown="")
    assert found["layout"]["ndown"] == 2
    assert _post(base, "api/continue", token, {"simdir": simulation, "ndown": "5"})["ok"] is False
    # a missing value that is not a number is said so, and nothing is read
    refused = _post(base, "api/continue", token, {"simdir": simulation, "ndown": "2", "missing": '[["abc",""]]'})
    assert refused == {"ok": False, "error": "Missing value 1: 'abc' is not a number."}
    assert app.view == "files"
    assert _post(base, "api/continue", token, {"simdir": simulation, "ndown": "2", "missing": '[["-999",""]]'})["ok"] is True
    _wait_for(app, "viewer")
    _wait_for_times(app)
    summary = _get(base, "api/catalogue", token=token)
    assert summary["files"] == 48
    reply = _get(base, "api/plot", token=token, stream=0, vars='["votemper"]', step=3, level=1)
    assert reply["ok"] and reply["file"].startswith("2000/04/")
    assert reply["when"] == "2000-04-16 00:00:00"
    assert reply["clim"][0] < reply["clim"][1]
    assert reply["n_missing"] == 0
    # the missing values were given on page 1: the warmest and coldest cells go, and the scale narrows
    lo, hi = reply["clim"]
    app.missing = [(lo, lo + 0.01), (hi - 0.01, hi)]
    narrower = _get(base, "api/plot", token=token, stream=0, vars='["votemper"]', step=3, level=1)
    assert narrower["n_missing"] > 0
    assert lo < narrower["clim"][0] and narrower["clim"][1] < hi
    app.missing = [(-1e9, 1e9)]
    everything = _get(base, "api/plot", token=token, stream=0, vars='["votemper"]', step=3, level=1)
    assert everything["ok"] and any("Every cell is missing" in note for note in everything["notes"])
    app.missing = []
    reply = _get(base, "api/plot", token=token, stream=0, vars='["votemper"]', vmin=0, vmax=30)
    assert reply["clim"] == [0, 30]
    assert _get(base, "api/plot", token=token, stream=0, vars='["nothing"]')["ok"] is False
    # the sum of two variables of a file type: one map, titled by both, and the units said to differ
    summed = _get(base, "api/plot", token=token, stream=1, vars='["chl", "votemper"]', step=3, level=1)
    assert summed["ok"] and any("units differ (degC, mg m-3)" in note for note in summed["notes"])
    single = _get(base, "api/plot", token=token, stream=1, vars='["chl"]', step=3, level=1)
    assert summed["clim"][0] > single["clim"][1] and "units differ" not in " ".join(single["notes"])
    # the figure names the sum by its hover, not a title
    assert "chl + votemper" in str(summed["item"])
    # what cannot be added is said in words, not as an exception
    refused = _get(base, "api/plot", token=token, stream=0, vars='["votemper", "sossheig"]')
    assert refused == {"ok": False, "error": "votemper and sossheig have different dimensions, so they cannot be added."}
    assert _get(base, "api/plot", token=token, stream=0, vars='["votemper", "votemper"]')["ok"] is False
    assert _get(base, "api/plot", token=token, stream=0, vars="[]") == {"ok": False, "error": "Choose a variable."}
    assert _get(base, "api/plot", token=token, stream=0, vars="nonsense")["error"] == "The variables could not be read."
    # the range over the whole run, for an animation's one colour scale
    limits = _get(base, "api/limits", token=token, stream=1, vars='["chl"]', level=1)
    assert limits["ok"] and limits["vmin"] < limits["vmax"] and limits["symmetric"] is False
    app.missing = [(-1e9, 1e9)]
    assert "Every cell is missing" in _get(base, "api/limits", token=token, stream=1, vars='["chl"]')["error"]
    app.missing = []
    assert _get(base, "api/limits", token=token, stream=0, vars='["votemper", "sossheig"]')["error"].endswith("cannot be added.")
    assert _get(base, "api/limits", token=token, stream=0, vars="[]")["error"] == "Choose a variable."
    # only maps are drawn: there is no asking for a cell
    with pytest.raises(urllib.error.HTTPError):
        _get(base, "api/cell", token=token, stream=0, x=1, y=1)
    _post(base, "api/back", token, {})
    assert app.view == "files"


def test_state_is_long_polled(running):
    app, base, token = running
    first = _get(base, "api/state", token=token)
    started = time.monotonic()
    threading.Timer(0.3, app._notify).start()
    later = _get(base, "api/state", token=token, version=first["version"])
    assert later["version"] == first["version"] + 1
    assert time.monotonic() - started < 5


def test_it_quits_when_the_page_has_gone(monkeypatch):
    monkeypatch.setattr(ncapp, "PAGE_TIMEOUT", 0.2)
    app = ncapp.App()
    app.start(0)
    app.touch("page")
    assert app.closed.wait(5)


def test_a_hidden_page_keeps_it_open(monkeypatch):
    monkeypatch.setattr(ncapp, "PAGE_TIMEOUT", 0.2)
    app = ncapp.App()
    app.start(0)
    app.page_visibility("page", True)
    assert not app.closed.wait(1.5)
    app.close()


def test_folders_like(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "a_file").write_text("")
    assert ncapp.folders_like(str(tmp_path / "a")) == [str(tmp_path / "alpha") + "/"]
    assert len(ncapp.folders_like(str(tmp_path) + "/")) == 2
    assert ncapp.folders_like("") == []


def test_parse_missing():
    assert ncapp.parse_missing("") == []
    assert ncapp.parse_missing('[["-999",""],["0","5"],["",""]]') == [(-999.0, -999.0), (0.0, 5.0)]
    assert ncapp.parse_missing('[[" 1e20 ", ""]]') == [(1e20, 1e20)]
    for bad in ('[["abc",""]]', '[["1","x"]]', '[["","3"]]', '[["nan",""]]', '[["inf",""]]', "not json", '{"a": 1}'):
        with pytest.raises(ValueError):
            ncapp.parse_missing(bad)


def test_parse_names():
    assert ncapp.parse_names("") == []
    assert ncapp.parse_names('["a", "b"]') == ["a", "b"]
    for bad in ("x", '{"a": 1}', "[1]"):
        with pytest.raises(ValueError):
            ncapp.parse_names(bad)


@pytest.fixture
def slow_times(monkeypatch):
    """The background reading of the times of the files (those after the first of each file type, which are
    read before the viewer opens) waits for the gate to be opened."""
    from ncviewer import catalogue

    gate = threading.Event()
    real = catalogue._read_times

    def gated(*args, **kwargs):
        gate.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(catalogue, "_read_times", gated)
    yield gate
    gate.set()


def test_the_viewer_opens_while_the_times_are_read(running, simulation, slow_times):
    app, base, token = running
    assert _post(base, "api/continue", token, {"simdir": simulation, "ndown": "2"})["ok"] is True
    _wait_for(app, "viewer")
    state = _get(base, "api/state", token=token)
    # the viewer is there, and says how many of the files' times are in
    assert state["view"] == "viewer" and state["times"]["total"] == 48 and state["times_number"] == 0
    summary = _get(base, "api/catalogue", token=token)
    assert summary["times_complete"] is False
    assert [(stream["steps"], stream["pending"]) for stream in summary["streams"]] == [(1, 23), (1, 23)]
    # the first map can be drawn meanwhile
    reply = _get(base, "api/plot", token=token, stream=1, vars='["chl"]', step=0, level=1)
    assert reply["ok"] and reply["when"] == "2000-01-16 12:00:00"
    # the times come in, and the page is told
    slow_times.set()
    deadline = time.monotonic() + 30
    while app.times is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    state = _get(base, "api/state", token=token)
    assert state["times"] is None and state["times_number"] == 1
    summary = _get(base, "api/catalogue", token=token)
    assert summary["times_complete"] is True
    assert [(stream["steps"], stream["pending"]) for stream in summary["streams"]] == [(24, 0), (24, 0)]
    assert summary["streams"][0]["step_files"] == list(range(24))
    # Play's colour scale for some of the steps: those of 2001 (steps 12 to 23) differ from those of 2000
    year_2000 = _get(base, "api/limits", token=token, stream=1, vars='["chl"]', level=1, first=0, last=11)
    year_2001 = _get(base, "api/limits", token=token, stream=1, vars='["chl"]', level=1, first=12, last=23)
    assert year_2000["ok"] and year_2001["ok"] and year_2001["vmin"] > year_2000["vmin"]


def test_going_back_stops_the_times_being_read(running, simulation, slow_times):
    app, base, token = running
    assert _post(base, "api/continue", token, {"simdir": simulation, "ndown": "2"})["ok"] is True
    _wait_for(app, "viewer")
    assert app.times is not None
    _post(base, "api/back", token, {})
    assert app.view == "files" and app.times is None
    # what was waiting is let go, and the reading stops: it does not say it is done
    slow_times.set()
    time.sleep(0.5)
    assert app.times is None and app.times_number == 0
    assert _get(base, "api/state", token=token)["times"] is None


def test_a_new_scan_replaces_the_one_reading(running, simulation, slow_times):
    app, base, token = running
    form = {"simdir": simulation, "ndown": "2"}
    assert _post(base, "api/continue", token, form)["ok"]
    _wait_for(app, "viewer")
    first = app.catalogue
    assert _post(base, "api/continue", token, form)["ok"]
    deadline = time.monotonic() + 30
    while app.catalogue is first and time.monotonic() < deadline:
        time.sleep(0.05)
    assert app.catalogue is not first
    slow_times.set()
    deadline = time.monotonic() + 30
    while app.times is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    # one set of times came in, not two
    assert app.times_number == 1 and app.catalogue.times_complete
