import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from simulations import nemo_simulation  # noqa: E402


@pytest.fixture(autouse=True)
def ncviewercache(tmp_path, monkeypatch):
    """Every test keeps what it reads in a cache of its own."""
    monkeypatch.setenv("NCVIEWERCACHE", str(tmp_path / "ncviewercache"))


@pytest.fixture(scope="session")
def simulation(tmp_path_factory):
    """A NEMO-like simulation laid out YYYY/MM/*.nc, two years of monthly files."""
    return nemo_simulation(str(tmp_path_factory.mktemp("sim")))


@pytest.fixture
def browser():
    """A headless Chromium, or the test is skipped where there is none."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as running:
        try:
            launched = running.chromium.launch()
        except Exception:  # noqa: BLE001 - not the version installed: the one the environment has
            path = os.environ.get("NCVIEWER_CHROMIUM", "/opt/pw-browsers/chromium")
            if not os.path.isfile(path):
                pytest.skip("Chromium is not installed")
            launched = running.chromium.launch(executable_path=path)
        yield launched
        launched.close()
