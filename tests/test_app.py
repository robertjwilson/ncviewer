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
    assert _post(base, "api/continue", token, {"simdir": simulation, "ndown": "2"})["ok"] is True
    _wait_for(app, "viewer")
    summary = _get(base, "api/catalogue", token=token)
    assert summary["files"] == 48
    reply = _get(base, "api/plot", token=token, kind="map", stream=0, var="votemper", step=3, level=1)
    assert reply["ok"] and reply["axes"] == "index" and reply["file"].startswith("2000/04/")
    cell = _get(base, "api/cell", token=token, stream=0, var="votemper", x=4.2, y=6.6, axes="index")
    assert cell["cell"] == [7, 4]
    reply = _get(base, "api/plot", token=token, kind="series", stream=0, var="votemper", cell="7,4")
    assert reply["ok"] and reply["cell"] == [7, 4]
    reply = _get(base, "api/plot", token=token, kind="hovmoller", stream=0, var="votemper")
    assert reply["ok"] and reply["cell"] == [6, 5]
    assert _get(base, "api/plot", token=token, kind="map", stream=0, var="nothing")["ok"] is False
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
