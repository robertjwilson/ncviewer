import os

import numpy as np
import pytest
from simulations import nemo_simulation, regular_file

from ncviewer import catalogue, times


@pytest.fixture
def found(simulation):
    return catalogue.scan(simulation, 2)


def test_streams_and_variables(found):
    summary = found.summary()
    assert summary["files"] == 48
    assert [stream["pattern"] for stream in summary["streams"]] == [
        "amm7_1m_**_**_grid_T.nc", "amm7_1m_**_**_ptrc_T.nc",
    ]
    variables = summary["variables"]
    # bounds and coordinates are not variables to look at
    assert set(variables) == {"votemper", "sossheig", "chl"}
    # a variable in two file types says both
    assert [entry["stream"] for entry in variables["votemper"]["streams"]] == [0, 1]
    temperature = variables["votemper"]["streams"][0]
    assert temperature["levels"] == 4
    assert temperature["level_values"] == [1.0, 5.0, 20.0, 50.0]
    assert temperature["kinds"] == ["map", "series", "profile", "hovmoller"]
    assert variables["sossheig"]["streams"][0]["kinds"] == ["map", "series"]
    assert variables["chl"]["long_name"] == "Chlorophyll"


def test_times(found):
    stream = found.summary()["streams"][0]
    assert stream["steps"] == 24
    assert stream["first"] == "2000-01-16 12:00:00"
    assert stream["last"] == "2001-12-16 12:00:00"
    assert stream["calendar"] == "noleap"
    assert stream["resolution"] == "monthly"
    assert stream["times"] == sorted(stream["times"])


def test_times_are_kept(simulation, monkeypatch):
    catalogue.scan(simulation, 2)
    read = []
    monkeypatch.setattr(times, "file_times", lambda path: read.append(path))
    again = catalogue.scan(simulation, 2)
    assert read == []
    assert again.summary()["streams"][0]["steps"] == 24


def test_a_changed_file_is_read_again(tmp_path):
    root = nemo_simulation(str(tmp_path / "sim"), years=(2000,), layout="flat")
    catalogue.scan(root, 0)
    path = sorted(os.listdir(root))[0]
    os.utime(os.path.join(root, path), ns=(1, 1))
    kept = times.load_cache([os.path.join(root, name) for name in os.listdir(root)])
    assert os.path.join(root, path) not in kept
    assert len(kept) == 23


def test_360_day_daily(tmp_path):
    root = nemo_simulation(str(tmp_path / "sim"), years=(2000,), layout="flat", per_month=30, calendar="360_day")
    stream = catalogue.scan(root, 0).summary()["streams"][0]
    assert stream["steps"] == 360
    assert stream["calendar"] == "360_day"
    assert stream["resolution"] == "daily"
    assert "2000-02-30 12:00:00" in stream["times"]


def test_many_files_in_a_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue, "PARALLEL_FROM", 2)
    root = nemo_simulation(str(tmp_path / "sim"), years=(2000,), layout="flat")
    told = []
    found = catalogue.scan(root, 0, progress=lambda **news: told.append(news))
    assert found.summary()["streams"][0]["steps"] == 12
    assert told[-1] == {"phase": "variables", "done": 2, "total": 2}
    assert {"phase": "times", "done": 24, "total": 24} in told


def test_stopped(simulation):
    with pytest.raises(catalogue.Stopped):
        catalogue.scan(simulation, 2, stopped=lambda: True)


def test_no_files(tmp_path):
    with pytest.raises(ValueError):
        catalogue.scan(str(tmp_path), 0)


def test_field_series_profile(found):
    data, when = found.field(0, "votemper", step=5, level=1)
    assert data.dims == ("y", "x")
    assert when == "2000-06-16 00:00:00"
    assert "nav_lon" in data.coords
    cell = found.nearest_cell(0, "votemper", -8.25, 50.06)
    assert cell == [5, 3]
    assert found.cell_position(0, "votemper", cell) == pytest.approx([-8.25, 50.06], abs=1e-4)
    stamps, values = found.series(0, "votemper", cell, level=0)
    assert len(stamps) == len(values) == 24
    assert values[1] - values[0] == pytest.approx(1.0)
    levels, profile, when = found.profile(0, "votemper", cell, step=3)
    assert levels == [1.0, 5.0, 20.0, 50.0]
    assert profile == sorted(profile, reverse=True)
    stamps, grid = found.series(0, "votemper", cell, all_levels=True)
    assert np.shape(grid) == (24, 4)


def test_nearest_cell_by_index(found):
    assert found.nearest_cell(0, "votemper", 3.2, 6.7, by_index=True) == [7, 3]
    assert found.nearest_cell(0, "votemper", 99, -5, by_index=True) == [0, 9]


def test_regular_grid(tmp_path):
    regular_file(str(tmp_path / "r.nc"))
    found = catalogue.scan(str(tmp_path), 0)
    variables = found.summary()["variables"]
    assert variables["sst"]["streams"][0]["kinds"] == ["map", "series"]
    assert variables["station_temp"]["streams"][0]["kinds"] == ["series"]
    assert found.nearest_cell(0, "sst", 0, 50) == [10, 20]
    stamps, values = found.series(0, "station_temp", [])
    assert len(values) == 3


def test_resolution():
    assert catalogue.resolution([]) == ""
    assert catalogue.resolution(["2000-01-01 00:00:00", "2000-01-01 06:00:00", "2000-01-01 12:00:00", "2000-01-01 18:00:00"]) == "6-hourly"
    assert catalogue.resolution([f"2000-01-{day:02d} 12:00:00" for day in range(1, 31, 5)]) == "5-daily"
    assert catalogue.resolution([f"{year}-07-01 00:00:00" for year in range(2000, 2005)]) == "annual"
