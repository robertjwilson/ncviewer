"""The time series of a cell: the cell a click on the map is for, and the
values read from every file (ncviewer.series), as xarray would give them."""

import netCDF4
import numpy as np
import pytest
import xarray as xr
from simulations import regular_file

from ncviewer import catalogue, series


@pytest.fixture
def found(simulation):
    found = catalogue.scan(simulation, 2)
    yield found
    found.close()


def _xarray_series(found, stream, name, level, j, i):
    record = found.streams[stream]
    values = []
    for _, at, position in record["steps"]:
        with xr.open_dataset(record["files"][at]) as ds:
            values.append(float(ds[name].isel(time_counter=position, deptht=level, y=j, x=i)))
    return np.array(values)


# ---- reading a cell ----

@pytest.mark.parametrize("fmt", ["NETCDF4", "NETCDF3_64BIT"])
def test_values_are_decoded_as_xarray_does(tmp_path, fmt):
    raw = np.arange(24, dtype="int16").reshape(4, 2, 3)
    raw[1, 1, 2] = -1
    path = str(tmp_path / f"packed_{fmt}.nc")
    # written as stored: packed integers, with a fill value
    with netCDF4.Dataset(path, "w", format=fmt) as out:
        for dim, size in zip(("time", "y", "x"), raw.shape):
            out.createDimension(dim, size)
        variable = out.createVariable("packed", "i2", ("time", "y", "x"), fill_value=np.int16(-1))
        variable.set_auto_maskandscale(False)
        variable.scale_factor, variable.add_offset = 0.5, 10.0
        variable[:] = raw
    got = series.point_values(path, ["packed"], [None, 1, 2])["packed"]
    with xr.open_dataset(path) as expected:
        np.testing.assert_allclose(got, expected["packed"].isel(y=1, x=2).values)
    assert np.isnan(got[1]) and got[0] == 10 + 0.5 * 5


def test_float32_keeps_its_type(tmp_path):
    path = regular_file(str(tmp_path / "regular.nc"))
    got = series.point_values(path, ["sst"], [None, 3, 4])["sst"]
    assert got.dtype == np.float32 and got.shape == (3,)


def test_default_cores():
    assert series.default_cores() == max(1, series.max_cores() // 2)
    assert series.default_cores() >= 1


# ---- the cell clicked ----

def test_cell_on_the_grids_indices(found):
    chl = 1
    assert found.cell(chl, ["chl"], 5.2, 6.4, "index") == {"j": 6, "i": 5, "lon": pytest.approx(-7.2), "lat": pytest.approx(50.5)}
    # land (x < 2, y < 3) is taken to the nearest cell with a value
    cell = found.cell(chl, ["chl"], 0, 0, "index")
    assert (cell["j"], cell["i"]) == (0, 2)
    with pytest.raises(ValueError, match="outside the grid"):
        found.cell(chl, ["chl"], 40, 6, "index")
    # nothing within 2 cells: every value near there missing
    with pytest.raises(ValueError, match="no data within 2 cells"):
        found.cell(chl, ["chl"], 5, 6, "index", missing=[(-1e9, 1e9)])


def test_cell_on_a_lonlat_grid(tmp_path):
    root = tmp_path / "regular"
    root.mkdir()
    for number in range(3):
        regular_file(str(root / f"sst_{number}.nc"))
    found = catalogue.scan(str(root), 0)
    try:
        # lon -20..10 by 1, lat 40..65 by 1
        cell = found.cell(0, ["sst"], -14.8, 50.3, "lonlat")
        assert (cell["j"], cell["i"]) == (10, 5)
        assert (cell["lon"], cell["lat"]) == (-15.0, 50.0)
        # the same point, as geoviews gives it in Web Mercator
        x = np.radians(-14.8) * 6378137.0
        y = np.log(np.tan(np.pi / 4 + np.radians(50.3) / 2)) * 6378137.0
        assert found.cell(0, ["sst"], x, y, "mercator") == cell
        assert catalogue.mercator_to_lonlat(x, y) == (pytest.approx(-14.8), pytest.approx(50.3))
        # longitudes are compared round the globe: 345 is -15
        assert found.cell(0, ["sst"], 345.2, 50.3, "lonlat") == cell
        with pytest.raises(ValueError, match="outside the grid"):
            found.cell(0, ["sst"], 100, 50, "lonlat")
    finally:
        found.close()


# ---- the series ----

def test_series_is_every_time_of_the_cell(found):
    times, values = found.series(1, ["chl"], 2, 6, 5)
    assert times == found.streams[1]["times"] and len(values) == 24
    np.testing.assert_allclose(values, _xarray_series(found, 1, "chl", 2, 6, 5), rtol=1e-6)


def test_series_with_missing_values_and_a_sum(found):
    plain = _xarray_series(found, 1, "chl", 0, 6, 5)
    temperature = _xarray_series(found, 1, "votemper", 0, 6, 5)
    _, summed = found.series(1, ["chl", "votemper"], 0, 6, 5)
    np.testing.assert_allclose(summed, plain + temperature, rtol=1e-6)
    # a value the user says is missing is missing in the sum
    missing = [(float(np.float32(plain[0])), float(np.float32(plain[0])))]
    _, summed = found.series(1, ["chl", "votemper"], 0, 6, 5, missing=missing)
    assert np.isnan(summed[0]) and np.isfinite(summed[1:]).all()


def test_series_in_a_pool_of_processes(found, monkeypatch):
    monkeypatch.setattr(catalogue, "PARALLEL_FROM", 4)
    told = []
    _, pooled = found.series(1, ["chl"], 1, 6, 5, cores=2, progress=lambda done, total: told.append((done, total)))
    np.testing.assert_allclose(pooled, _xarray_series(found, 1, "chl", 1, 6, 5), rtol=1e-6)
    # the pool is kept for the next, and made again for other cores
    pool = found._pool
    found.series(1, ["chl"], 1, 6, 5, cores=2)
    assert found._pool is pool
    found.series(1, ["chl"], 1, 6, 5, cores=3)
    assert found._pool is not pool and found._pool_cores == 3
    # the files read are counted as they go
    assert told[0] == (0, 24) and told[-1] == (24, 24) and len(told) > 3


def test_series_counts_each_file(found):
    told = []
    found.series(1, ["chl"], 1, 6, 5, progress=lambda done, total: told.append(done))
    assert told == list(range(25))


def test_series_can_be_stopped(found):
    with pytest.raises(catalogue.Stopped):
        found.series(1, ["chl"], 1, 6, 5, stopped=lambda: True)
