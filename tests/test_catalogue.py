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


def test_field(found):
    data, when = found.field(0, "votemper", step=5, level=1)
    assert data.dims == ("y", "x")
    assert when == "2000-06-16 00:00:00"
    assert "nav_lon" in data.coords
    # a later month is warmer, a deeper level colder
    assert float(found.field(0, "votemper", 6, 1)[0].mean()) > float(data.mean())
    assert float(found.field(0, "votemper", 5, 3)[0].mean()) < float(data.mean())
    # a variable with no levels takes none
    data, when = found.field(0, "sossheig", step=2, level=3)
    assert data.dims == ("y", "x") and when == "2000-03-16 12:00:00"


def test_regular_grid(tmp_path):
    regular_file(str(tmp_path / "r.nc"))
    found = catalogue.scan(str(tmp_path), 0)
    # only what can be mapped is listed: not a series with no horizontal dimensions
    assert list(found.summary()["variables"]) == ["sst"]
    data, when = found.field(0, "sst", 1)
    assert data.dims == ("lat", "lon")


def test_resolution():
    assert catalogue.resolution([]) == ""
    assert catalogue.resolution(["2000-01-01 00:00:00", "2000-01-01 06:00:00", "2000-01-01 12:00:00", "2000-01-01 18:00:00"]) == "6-hourly"
    assert catalogue.resolution([f"2000-01-{day:02d} 12:00:00" for day in range(1, 31, 5)]) == "5-daily"
    assert catalogue.resolution([f"{year}-07-01 00:00:00" for year in range(2000, 2005)]) == "annual"


def test_missing_mask():
    import xarray as xr

    from ncviewer.catalogue import missing_mask

    data = xr.DataArray(np.array([[0, -999, 5.5, 1e20, np.nan]], dtype="float32"))
    assert missing_mask(data, []).values.sum() == 0
    # a single number, and a float32 1e20, which is not the float64 one
    assert missing_mask(data, [(-999, -999)]).values.tolist() == [[False, True, False, False, False]]
    assert missing_mask(data, [(1e20, 1e20)]).values.tolist() == [[False, False, False, True, False]]
    # a range includes both ends, and its ends can be either way round
    assert missing_mask(data, [(-999, 0)]).values.tolist() == [[True, True, False, False, False]]
    assert missing_mask(data, [(5.5, 0)]).values.tolist() == [[True, False, True, False, False]]
    # several, and NaN never matches
    assert missing_mask(data, [(0, 0), (1e20, 1e20)]).values.sum() == 2
    # a number too big for the type matches nothing, and is no error
    assert missing_mask(data, [(1e40, 1e41)]).values.sum() == 0
    integers = xr.DataArray(np.array([0, 1, 2, 3]))
    assert missing_mask(integers, [(1, 2)]).values.tolist() == [False, True, True, False]
    assert missing_mask(integers, [(0.5, 1.5)]).values.tolist() == [False, True, False, False]


def test_field_with_missing_values(found):
    data, _ = found.field(0, "votemper", 0, 0)
    assert data.attrs["n_missing"] == 0
    rows = sorted(set(np.round(data.values[np.isfinite(data.values)], 3)))
    low = float(rows[0])
    masked, _ = found.field(0, "votemper", 0, 0, [(low, low)])
    assert masked.attrs["n_missing"] > 0
    assert np.isnan(masked.values).sum() == np.isnan(data.values).sum() + masked.attrs["n_missing"]
    assert float(np.nanmin(masked.values)) > low
    # everything from the lowest to the highest
    everything, _ = found.field(0, "votemper", 0, 0, [(low - 1, float(rows[-1]) + 1)])
    assert np.isnan(everything.values).all()


def test_sum_of_variables(found):
    chl, when = found.field(1, "chl", 3, 1)
    temperature, _ = found.field(1, "votemper", 3, 1)
    total, summed_when = found.field(1, ["chl", "votemper"], 3, 1)
    assert summed_when == when
    assert np.allclose(total.values, (chl + temperature).values, equal_nan=True)
    # land is missing in both, so in the sum
    assert np.isnan(total.values).sum() == np.isnan(chl.values).sum() > 0
    # one name in a list is that variable
    alone, _ = found.field(1, ["chl"], 3, 1)
    assert np.array_equal(alone.values, chl.values, equal_nan=True)
    about = found.combined(1, ["chl", "votemper"])
    assert about["name"] == "chl + votemper"
    assert about["long_name"] == "Chlorophyll + Temperature"
    assert about["units_differ"] == ["degC", "mg m-3"]
    assert found.combined(1, ["chl"])["units_differ"] is None


def test_sum_applies_missing_values_to_each_variable(found):
    chl, _ = found.field(1, "chl", 3, 1)
    low = float(np.nanmin(chl.values))
    # a range that only chl has in it: those cells are missing in the sum too
    total, _ = found.field(1, ["chl", "votemper"], 3, 1, [(low, low)])
    assert total.attrs["n_missing"] > 0
    plain, _ = found.field(1, ["chl", "votemper"], 3, 1)
    assert np.isnan(total.values).sum() == np.isnan(plain.values).sum() + total.attrs["n_missing"]


def test_what_cannot_be_added(found):
    with pytest.raises(ValueError, match="different dimensions"):
        found.combined(0, ["votemper", "sossheig"])
    with pytest.raises(ValueError, match="only be added once"):
        found.combined(0, ["votemper", "votemper"])
    with pytest.raises(ValueError, match="not in that file type"):
        found.combined(0, ["votemper", "chl"])
    with pytest.raises(ValueError, match="Choose a variable"):
        found.combined(0, [])


def test_limits_cover_the_whole_run(found):
    low, high, symmetric = found.limits(1, ["chl"], level=1)
    # wider than any one month's range
    month_low = month_high = None
    for step in range(24):
        data, _ = found.field(1, "chl", step, 1)
        values = data.values[np.isfinite(data.values)]
        month_low = values.min() if month_low is None else min(month_low, values.min())
        month_high = values.max() if month_high is None else max(month_high, values.max())
    first, _ = found.field(1, "chl", 0, 1)
    assert low == pytest.approx(float(month_low)) and high == pytest.approx(float(month_high))
    # the first month is not the whole run
    assert high > float(np.nanmax(first.values))
    assert symmetric is False


def test_limits_use_missing_values_and_sums(found):
    low, high, _ = found.limits(1, ["chl"], level=1)
    cut, cut_high, _ = found.limits(1, ["chl"], level=1, missing=[(low, low + 0.5)])
    assert cut > low
    total_low, total_high, _ = found.limits(1, ["chl", "votemper"], level=1)
    assert total_low > high
    # everything missing: nothing to give
    assert found.limits(1, ["chl"], level=1, missing=[(-1e9, 1e9)]) is None


def test_limits_read_a_few_steps_only(found, monkeypatch):
    steps = []
    real = found.field
    monkeypatch.setattr(found, "field", lambda stream, names, step, *args: steps.append(step) or real(stream, names, step, *args))
    found.limits(1, ["chl"], level=0, samples=5)
    assert steps == [0, 6, 12, 17, 23]


def test_limits_span_zero_are_symmetric(tmp_path):
    import xarray as xr

    lon, lat = np.arange(4.0), np.arange(3.0)
    values = np.stack([np.full((3, 4), -2.0), np.full((3, 4), 5.0)])
    xr.Dataset(
        {"anomaly": (("time", "lat", "lon"), values, {"units": "K"})},
        coords={"lon": ("lon", lon, {"units": "degrees_east"}), "lat": ("lat", lat, {"units": "degrees_north"}),
                "time": ("time", [0.0, 30.0], {"units": "days since 2000-01-01", "calendar": "standard"})},
    ).to_netcdf(tmp_path / "a.nc")
    found = catalogue.scan(str(tmp_path), 0)
    assert found.limits(0, ["anomaly"]) == (-5.0, 5.0, True)
