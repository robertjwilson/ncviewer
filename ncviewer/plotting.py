"""The viewer's plots, drawn with hvplot and sent to the page as Bokeh
json_items.

How a map is drawn follows ncplot (ncplot/plot.py): an image for a regular
longitude/latitude grid, a quadmesh for an uneven or curvilinear one, the
grid's own indices when its longitudes/latitudes cannot place the cells
(ncplot's change_coords: model grids whose land cells all have 0 or a fill
value), a colour scale symmetric about 0 in RdBu_r when the values span 0
(ncplot's _get_color_limits), and coastlines at a resolution set by the
extent (get_coastline) when geoviews is there.
"""

import warnings

import holoviews as hv
import hvplot.pandas  # noqa: F401 - registers .hvplot
import hvplot.xarray  # noqa: F401 - registers .hvplot
import numpy as np
import pandas as pd
import xarray as xr
from bokeh.embed import json_item
from bokeh.models import CustomJS

# a quadmesh of more cells than this is thinned out to it, as a browser draws each cell
MAX_QUADS = 250_000
# an image of more than this is thinned out too
MAX_PIXELS = 4_000_000

COLOUR_MAPS = ["viridis", "cividis", "plasma", "inferno", "magma", "turbo", "RdBu_r", "coolwarm", "Blues", "YlGnBu", "gray"]


def has_geoviews():
    try:
        import cartopy  # noqa: F401
        import geoviews  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _render(plot, tap=False):
    figure = hv.render(plot, backend="bokeh")
    figure.sizing_mode = "stretch_both"
    figure.toolbar.logo = None
    if tap:
        # a click on the plot is told to the page, which asks for that cell's plots
        figure.js_on_event("tap", CustomJS(code=(
            "document.dispatchEvent(new CustomEvent('ncviewer-tap', {detail: {x: cb_obj.x, y: cb_obj.y}}));"
        )))
    return json_item(figure)


def _label(about, name):
    words = about.get("long_name") or name
    return f"{words} ({about['units']})" if about.get("units") else words


def colour_limits(values, vmin=None, vmax=None, log=False):
    """The colour scale's limits, and whether it is symmetric about 0: the
    limits given, or the values' range, made symmetric about 0 when it spans
    it (as ncplot's _get_color_limits)."""
    finite = np.asarray(values, dtype="float64")
    finite = finite[np.isfinite(finite)]
    if log:
        finite = finite[finite > 0]
    low = float(finite.min()) if finite.size else 0.0
    high = float(finite.max()) if finite.size else 1.0
    if vmin is not None and vmax is not None:
        return (float(vmin), float(vmax)), False
    if vmin is not None:
        return (float(vmin), max(high, float(vmin))), False
    if vmax is not None:
        return (min(low, float(vmax)), float(vmax)), False
    if low < 0 < high and not log:
        top = max(-low, high)
        return (-top, top), True
    if low == high:
        return (low - 0.5, high + 0.5) if low == 0 else (low * 0.99, high * 1.01) if low > 0 else (low * 1.01, high * 0.99), False
    return (low, high), False


def _repeated_coordinates(lons, lats):
    """Whether cells share a longitude and latitude: the 0s (or fill values)
    a model writes at land cells, which put those cells on top of one
    another (ncplot's change_coords, which looks for a value far more common
    than any other; here any pair of cells in one place is enough, as a
    quadmesh is pulled out of shape by one)."""
    pairs = pd.DataFrame({"lon": np.asarray(lons).ravel(), "lat": np.asarray(lats).ravel()})
    return bool(pairs.duplicated().any())


def _uneven(values):
    values = np.asarray(values, dtype="float64")
    if values.size < 3:
        return False
    steps = np.abs(np.diff(values))
    if steps.min() == 0:
        return True
    return steps.max() / steps.min() > 1.001


def _thin(data, limit):
    cells = int(np.prod(data.shape))
    if cells <= limit:
        return data, 1
    stride = int(np.ceil(np.sqrt(cells / limit)))
    return data.isel({dim: slice(None, None, stride) for dim in data.dims}), stride


def coastline_scale(lon, lat):
    """ncplot's get_coastline: the resolution of the coastline for an extent."""
    area = (np.nanmax(lon) - np.nanmin(lon)) * (np.nanmax(lat) - np.nanmin(lat)) / 360**2
    if area < 0.0016:
        return "10m"
    if area < 0.25:
        return "50m"
    return "110m"


def map_plot(data, about, name, when=None, cmap=None, vmin=None, vmax=None, log=False, coast=False):
    """The map of a 2-D slice (see Catalogue.field), as (json_item, notes,
    the colour scale's limits, its axes: "lonlat" or "index")."""
    notes = []
    data = data.squeeze(drop=True).astype("float64")
    data.name = name
    lon, lat = about.get("lon"), about.get("lat")
    if lon not in data.coords or lat not in data.coords:
        lon = lat = None
    y_dim, x_dim = about["horizontal"][-2:]
    kind = "image"
    if lon is not None:
        lons, lats = np.asarray(data[lon].values), np.asarray(data[lat].values)
        if lons.ndim == 1 and lats.ndim == 1:
            # a regular lon-lat grid is an image; an uneven one a quadmesh
            x, y = lon, lat
            kind = "quadmesh" if _uneven(lats) or _uneven(lons) else "image"
            if data[lat].dims[0] != data.dims[0]:
                data = data.transpose(data[lat].dims[0], data[lon].dims[0])
        elif not np.all(np.isfinite(lons)) or not np.all(np.isfinite(lats)) or _repeated_coordinates(lons, lats):
            # cells the coordinates cannot place: the grid's own indices (ncplot's change_coords)
            lon = lat = None
            notes.append("The longitudes and latitudes cannot place every cell, so the grid's own indices are shown.")
        else:
            x, y = lon, lat
            kind = "quadmesh"
    if lon is None:
        data = data.drop_vars([key for key in data.coords if key not in data.dims])
        data = data.assign_coords({dim: np.arange(data.sizes[dim]) for dim in data.dims})
        data = data.transpose(y_dim, x_dim)
        x, y = x_dim, y_dim
        kind = "image"
    if kind == "quadmesh":
        data, stride = _thin(data, MAX_QUADS)
    else:
        data, stride = _thin(data, MAX_PIXELS)
    if stride > 1:
        notes.append(f"Every {stride}th cell is shown, as the grid is large.")

    (low, high), symmetric = colour_limits(data.values, vmin, vmax, log)
    if log and high <= 0:
        log = False
        notes.append("There are no positive values, so the scale is not logarithmic.")
    if log:
        low = max(low, np.nanmin(np.where(data.values > 0, data.values, np.nan)))
    colour = cmap or ("RdBu_r" if symmetric else "viridis")
    title = _label(about, name) + (f", {when}" if when else "")
    options = dict(
        x=x, y=y, cmap=colour, clim=(low, high), logz=log, title=title,
        tools=["hover", "tap"], responsive=True, colorbar=True, clabel=about.get("units") or None,
    )
    geographic = coast and lon is not None and has_geoviews()
    if coast and lon is not None and not geographic:
        notes.append("Coastlines need cartopy and geoviews installed.")
    if geographic:
        options.update(geo=True, coastline=coastline_scale(data[x].values, data[y].values))
    else:
        options.update(xlabel=x, ylabel=y)
        if lon is not None:
            options.update(xlabel="Longitude", ylabel="Latitude")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if kind == "quadmesh":
            plot = data.hvplot.quadmesh(**options)
        else:
            plot = data.hvplot.image(**options)
    return _render(plot, tap=True), notes, [float(low), float(high)], ("index" if lon is None else "lonlat")


def time_axis(times):
    """The times (ISO strings) as the x of a plot: datetimes where pandas can
    take them all (the standard calendars), otherwise decimal years (360-day
    and the like), and the axis's label."""
    try:
        return pd.to_datetime(pd.Series(times), format="ISO8601"), "Time"
    except (ValueError, TypeError, OverflowError):
        pass
    years = []
    for stamp in times:
        year, month, day = int(stamp[0:4]), int(stamp[5:7]), int(stamp[8:10])
        hour = int(stamp[11:13]) if len(stamp) >= 13 else 0
        years.append(year + (month - 1) / 12 + (day - 1 + hour / 24) / 360)
    return pd.Series(years), "Time (decimal years)"


def series_plot(times, values, about, name, where=""):
    """A variable through time at a cell."""
    x, xlabel = time_axis([str(stamp) for stamp in times])
    frame = pd.DataFrame({"time": x, name: pd.to_numeric(pd.Series(values), errors="coerce")})
    plot = frame.hvplot.line(
        x="time", y=name, title=f"{_label(about, name)}{where}", xlabel=xlabel,
        ylabel=about.get("units") or name, tools=["hover"], responsive=True,
    )
    plot = (plot * frame.hvplot.scatter(x="time", y=name, size=12, tools=["hover"], responsive=True)).opts(responsive=True)
    return _render(plot)


def line_plot(data, about, name, when=None):
    """A variable along its one horizontal dimension."""
    data = data.squeeze(drop=True).astype("float64")
    dim = data.dims[0]
    frame = pd.DataFrame({dim: np.arange(data.sizes[dim]), name: data.values})
    plot = frame.hvplot.line(
        x=dim, y=name, title=_label(about, name) + (f", {when}" if when else ""),
        ylabel=about.get("units") or name, tools=["hover", "tap"], responsive=True,
    )
    return _render(plot, tap=True)


def _depth_down(about):
    """Whether the levels are depths, to be drawn down the page."""
    values = about.get("level_values") or []
    units = (about.get("level_units") or "").lower()
    vertical = (about.get("vertical") or "").lower()
    if units in ("", "1") and "depth" not in vertical:
        return False
    return len(values) < 2 or values[-1] >= values[0]


def profile_plot(levels, values, about, name, where="", when=None):
    """A variable through the levels at a cell and time."""
    depth = about.get("vertical") or "level"
    frame = pd.DataFrame({depth: levels, name: pd.to_numeric(pd.Series(values), errors="coerce")})
    plot = frame.hvplot.line(
        x=name, y=depth, title=f"{_label(about, name)}{where}" + (f", {when}" if when else ""),
        xlabel=about.get("units") or name, ylabel=f"{depth} ({about.get('level_units')})" if about.get("level_units") else depth,
        tools=["hover"], responsive=True, flip_yaxis=_depth_down(about),
    ) * frame.hvplot.scatter(x=name, y=depth, size=12, tools=["hover"], responsive=True, flip_yaxis=_depth_down(about))
    return _render(plot.opts(responsive=True))


def hovmoller_plot(times, values, about, name, where="", cmap=None, vmin=None, vmax=None, log=False):
    """A variable at a cell, its levels against time (ncplot's heat map of two
    non-spatial dimensions)."""
    x, xlabel = time_axis([str(stamp) for stamp in times])
    depth = about.get("vertical") or "level"
    array = np.array([[np.nan if value is None else value for value in row] for row in values], dtype="float64")
    data = xr.DataArray(array.T, dims=(depth, "time"), coords={depth: about["level_values"], "time": x.values}, name=name)
    (low, high), symmetric = colour_limits(array, vmin, vmax, log)
    plot = data.hvplot.quadmesh(
        x="time", y=depth, cmap=cmap or ("RdBu_r" if symmetric else "viridis"), clim=(low, high), logz=log,
        title=f"{_label(about, name)}{where}", xlabel=xlabel, tools=["hover"], responsive=True,
        flip_yaxis=_depth_down(about), clabel=about.get("units") or None,
    )
    return _render(plot)
