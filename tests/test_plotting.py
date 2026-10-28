import re

import numpy as np
import pytest
import xarray as xr
from simulations import regular_file

from ncviewer import catalogue, plotting


def _kinds(item):
    """The kinds of glyph a json_item draws (a quadmesh of a curvilinear grid
    is drawn as Patches, one of a rectilinear one as Quads)."""
    names = set(re.findall(r"'name': '([A-Za-z]+)'", str(item)))
    found = names & {"Image", "Line", "Scatter"}
    if names & {"QuadMesh", "Patches", "Quad"}:
        found.add("QuadMesh")
    return found


@pytest.fixture
def found(simulation):
    return catalogue.scan(simulation, 2)


def test_curvilinear_land_at_zero_is_drawn_by_index(found):
    data, when = found.field(0, "votemper", 0, 0)
    item, notes, clim, axes = plotting.map_plot(data, found.about(0, "votemper"), "votemper", when)
    assert axes == "index"
    assert "Image" in _kinds(item)
    assert any("indices" in note for note in notes)
    assert clim[0] < clim[1]


def test_curvilinear_grid_is_a_quadmesh():
    y, x = np.meshgrid(np.arange(8), np.arange(6), indexing="ij")
    data = xr.DataArray(
        np.random.default_rng(0).random((8, 6)), dims=("y", "x"),
        coords={"lon": (("y", "x"), -10 + x + 0.1 * y), "lat": (("y", "x"), 50 + y + 0.1 * x)},
    )
    about = {"horizontal": ["y", "x"], "lon": "lon", "lat": "lat", "units": "K", "long_name": "t"}
    item, notes, clim, axes = plotting.map_plot(data, about, "t")
    assert axes == "lonlat"
    assert "QuadMesh" in _kinds(item)


def test_regular_grid_is_an_image(tmp_path):
    regular_file(str(tmp_path / "r.nc"))
    found = catalogue.scan(str(tmp_path), 0)
    data, when = found.field(0, "sst", 1)
    item, notes, clim, axes = plotting.map_plot(data, found.about(0, "sst"), "sst", when)
    assert axes == "lonlat"
    assert "Image" in _kinds(item)


def test_uneven_latitudes_are_a_quadmesh():
    lat = np.array([40, 41, 43, 47, 55.0])
    data = xr.DataArray(np.ones((5, 4)), dims=("lat", "lon"), coords={"lat": lat, "lon": np.arange(4.0)})
    about = {"horizontal": ["lat", "lon"], "lon": "lon", "lat": "lat", "units": "", "long_name": ""}
    item, notes, clim, axes = plotting.map_plot(data, about, "v")
    assert "QuadMesh" in _kinds(item)


def test_colour_limits():
    assert plotting.colour_limits([-1, 3]) == ((-3, 3), True)
    assert plotting.colour_limits([1, 3]) == ((1, 3), False)
    assert plotting.colour_limits([1, 3], vmin=0, vmax=10) == ((0, 10), False)
    assert plotting.colour_limits([np.nan, 2, 4]) == ((2, 4), False)
    low, high = plotting.colour_limits([2, 2])[0]
    assert low < 2 < high


def test_point_plots(found):
    about = found.about(0, "votemper")
    stamps, values = found.series(0, "votemper", [5, 5])
    assert "Line" in _kinds(plotting.series_plot(stamps, values, about, "votemper"))
    levels, values, when = found.profile(0, "votemper", [5, 5])
    assert "Line" in _kinds(plotting.profile_plot(levels, values, about, "votemper"))
    stamps, grid = found.series(0, "votemper", [5, 5], all_levels=True)
    assert "QuadMesh" in _kinds(plotting.hovmoller_plot(stamps, grid, about, "votemper"))


def test_360_day_times_are_decimal_years():
    x, label = plotting.time_axis(["2000-02-30 00:00:00", "2000-03-01 00:00:00"])
    assert label == "Time (decimal years)"
    assert x.iloc[0] < x.iloc[1]
