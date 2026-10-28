"""What a simulation's output holds: its streams (file types), the variables
in each, and the times of every file. Also the reading of the slices the
viewer plots, which is all that is ever read of the data, as the files can be
many GB.

The variables are read from the first file of each stream (every file of a
stream holds the same ones), as OceanVal's analysis.streams does; the times
from every file (see ncviewer.times).
"""

import concurrent.futures
import multiprocessing
import os
import threading
from collections import OrderedDict

import numpy as np
import xarray as xr

from . import files as found_files
from . import times as file_times
from .times import natural_key

# netCDF-C and HDF5 are not thread-safe, and the server answers in threads:
# every read of a file in this process holds it
LOCK = threading.RLock()

# read in a pool of processes once there are this many files to read
PARALLEL_FROM = 40
WORKERS = 4

# the names of vertical dimensions, as OceanVal tells them
_VERTICAL = ("depth", "lev", "sig", "s_rho", "z", "height", "nz", "deptht", "depthu", "depthv", "depthw")


class Stopped(Exception):
    """A scan or read that was stopped as it is no longer wanted."""


# ---- the variables of a file ----

def _attr(variable, name):
    value = variable.attrs.get(name, "")
    return str(value) if value is not None else ""


def _is_vertical(ds, dim):
    name = dim.lower()
    if dim in ds.variables:
        attrs = ds[dim].attrs
        if str(attrs.get("axis", "")).upper() == "Z" or attrs.get("positive") in ("up", "down"):
            return True
        if str(attrs.get("standard_name", "")) in ("depth", "height", "altitude"):
            return True
    return any(name == word or name.startswith(word) for word in _VERTICAL)


def _bounds_names(ds):
    names = set()
    for variable in ds.variables.values():
        for key in ("bounds", "climatology", "edges"):
            if key in variable.attrs:
                names.add(str(variable.attrs[key]))
    return names


def _lonlat(ds, name):
    """The names of the longitude and latitude of variable name, or Nones:
    its coordinates with a standard_name, units or name that says so (the
    rules of ncplot's get_dims, without metpy)."""
    variable = ds[name]
    candidates = list(variable.coords)
    for word in _attr(variable, "coordinates").split():
        if word in ds.variables and word not in candidates:
            candidates.append(word)
    candidates += [dim for dim in variable.dims if dim in ds.variables and dim not in candidates]

    def find(standard, units, words):
        for key in candidates:
            attrs = ds[key].attrs
            if str(attrs.get("standard_name", "")) == standard:
                return key
        for key in candidates:
            if str(ds[key].attrs.get("units", "")).lower() in units:
                return key
        for key in candidates:
            if any(key.lower().startswith(word) or key.lower().endswith(word) for word in words):
                return key
        return None

    lon = find("longitude", ("degrees_east", "degree_east", "degrees_e", "degreee"), ("lon", "nav_lon", "glamt"))
    lat = find("latitude", ("degrees_north", "degree_north", "degrees_n", "degreen"), ("lat", "nav_lat", "gphit"))
    # coordinates that do not span the variable's own dimensions cannot place it
    for key in (lon, lat):
        if key is not None and not set(ds[key].dims) <= set(variable.dims):
            return None, None
    return lon, lat


def describe_variables(ds, time_dim):
    """{name: about} for each data variable of ds (an open xarray Dataset):
    its long_name, units, dims, sizes, which dim is time, which is vertical,
    which are the horizontal ones, and its longitude/latitude names."""
    skip = _bounds_names(ds)
    coords = set(ds.coords)
    found = {}
    for name, variable in ds.data_vars.items():
        if name in skip or name in coords or variable.ndim == 0:
            continue
        if name.endswith(("_bnds", "_bounds")):
            continue
        dims = list(variable.dims)
        time = time_dim if time_dim in dims else None
        rest = [dim for dim in dims if dim != time]
        vertical = next((dim for dim in rest if _is_vertical(ds, dim)), None)
        # the last two of the others are the horizontal ones; any before them are taken at their first index
        horizontal = [dim for dim in rest if dim != vertical][-2:]
        lon, lat = _lonlat(ds, name)
        levels = int(variable.sizes[vertical]) if vertical else 1
        found[name] = {
            "long_name": _attr(variable, "long_name") or _attr(variable, "standard_name"),
            "units": _attr(variable, "units"),
            "dims": dims,
            "shape": [int(variable.sizes[dim]) for dim in dims],
            "time_dim": time,
            "vertical": vertical,
            "levels": levels,
            "level_values": _level_values(ds, vertical) if vertical else None,
            "level_units": _attr(ds[vertical], "units") if vertical and vertical in ds.variables else "",
            "horizontal": horizontal,
            "lon": lon,
            "lat": lat,
        }
    return found


def _level_values(ds, dim):
    if dim in ds.variables and ds[dim].ndim == 1:
        try:
            return [float(value) for value in np.asarray(ds[dim].values, dtype="float64")]
        except (TypeError, ValueError):
            pass
    return list(range(int(ds.sizes[dim])))


def open_dataset(path):
    """An xarray Dataset of path, with times left as numbers (they are read
    by ncviewer.times, for any calendar). The caller holds LOCK."""
    return xr.open_dataset(path, decode_times=False, cache=False)


# ---- the scan ----

def _read_times(paths, report, stopped):
    """{path: times} of every one of paths, those kept from before first,
    the rest read in a pool of processes when there are many."""
    read = file_times.load_cache(paths)
    todo = [path for path in paths if path not in read]
    report(len(read))
    fresh = {}
    if len(todo) >= PARALLEL_FROM:
        context = multiprocessing.get_context("spawn")
        try:
            with concurrent.futures.ProcessPoolExecutor(WORKERS, mp_context=context) as pool:
                futures = {pool.submit(file_times.file_times, path): path for path in todo}
                try:
                    for future in concurrent.futures.as_completed(futures):
                        if stopped():
                            raise Stopped()
                        fresh[futures[future]] = future.result()
                        report(len(read) + len(fresh))
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
        except (concurrent.futures.process.BrokenProcessPool, OSError):
            # workers that could not start (a __main__ they cannot import): the rest are read here
            pass
    for path in todo:
        if path in fresh:
            continue
        if stopped():
            raise Stopped()
        fresh[path] = file_times.file_times(path)
        report(len(read) + len(fresh))
    file_times.save_cache(fresh)
    read.update(fresh)
    return read


def resolution(times):
    """How often the output is, from its times (ISO strings): "hourly",
    "3-hourly", "daily", "5-daily", "monthly", "annual", or "" if there is
    one time or none. Only the dates are compared, so any calendar works."""
    if len(times) < 2:
        return ""
    stamps = sorted(set(times))
    days = [(int(t[0:4].lstrip("-") or 0), int(t[5:7]), int(t[8:10])) for t in stamps]
    per_day = len(stamps) / max(1, len(set(days)))
    if per_day > 1.5:
        hours = max(1, round(24 / per_day))
        return "hourly" if hours == 1 else f"{hours}-hourly"
    months = {day[:2] for day in days}
    if len(months) == len(days):
        years = {day[0] for day in days}
        return "annual" if len(years) == len(days) and len(days) > 1 else "monthly"
    gaps = sorted(later[2] - earlier[2] for earlier, later in zip(days, days[1:]) if earlier[:2] == later[:2])
    if not gaps:
        return "daily"
    step = max(1, gaps[len(gaps) // 2])
    return "daily" if step == 1 else f"{step}-daily"


def scan(simdir, ndown, exclude="", require="", progress=lambda **_: None, stopped=lambda: False):
    """The Catalogue of the output files ndown directories below simdir that
    pass the file filters. progress(phase=, done=, total=) is told how far it
    has got, and stopped() is asked whether to give up (raising Stopped)."""
    simdir = os.path.abspath(os.path.expanduser(simdir.strip()))
    paths = found_files.simulation_paths(
        simdir, int(ndown), exclude.split() or None, require.split() or None
    )
    if not paths:
        raise ValueError("No netCDF files were found.")
    groups = OrderedDict()
    for path in sorted(paths, key=natural_key):
        groups.setdefault(found_files.file_pattern(path), []).append(path)

    progress(phase="times", done=0, total=len(paths))
    read = _read_times(
        paths, lambda done: progress(phase="times", done=done, total=len(paths)), stopped
    )

    streams = []
    progress(phase="variables", done=0, total=len(groups))
    for number, (pattern, members) in enumerate(groups.items()):
        if stopped():
            raise Stopped()
        streams.append(_stream(number, pattern, members, read))
        progress(phase="variables", done=number + 1, total=len(groups))
    return Catalogue(simdir, int(ndown), streams)


def _stream(number, pattern, members, read):
    first = members[0]
    time_name = read[first].get("name")
    variables, time_dim, error = {}, None, None
    try:
        with LOCK:
            with open_dataset(first) as ds:
                if time_name and time_name in ds.variables and ds[time_name].ndim == 1:
                    time_dim = ds[time_name].dims[0]
                variables = describe_variables(ds, time_dim)
    except Exception as problem:  # noqa: BLE001 - a stream that cannot be read is reported
        error = f"{os.path.basename(first)} could not be read: {problem}"
    # every time of the stream, in order, with the file and index it is at
    steps = []
    calendar = None
    unreadable = 0
    for index, path in enumerate(members):
        times = read.get(path) or {}
        if times.get("error"):
            unreadable += 1
        calendar = calendar or times.get("calendar")
        if times.get("times"):
            steps += [(stamp, index, at) for stamp, at in zip(times["times"], times["index"])]
        else:
            # no time axis: the file is one step, named by the file
            steps.append((None, index, 0))
    if all(stamp is not None for stamp, _, _ in steps):
        steps.sort(key=lambda step: (step[0], step[1]))
    stamps = [stamp for stamp, _, _ in steps]
    return {
        "id": number,
        "pattern": pattern,
        "files": members,
        "variables": variables,
        "time_name": time_name,
        "time_dim": time_dim,
        "calendar": calendar,
        "steps": steps,
        "times": [stamp if stamp is not None else os.path.basename(members[index]) for stamp, index, _ in steps],
        "has_times": all(stamp is not None for stamp in stamps) and bool(stamps),
        "resolution": resolution([stamp for stamp in stamps if stamp is not None]),
        "unreadable": unreadable,
        "error": error,
    }


class Catalogue:
    """The streams of a simulation's output (see scan), and the reading of
    the slices of its variables that the viewer plots."""

    def __init__(self, simdir, ndown, streams):
        self.simdir = simdir
        self.ndown = ndown
        self.streams = streams
        self._open = OrderedDict()
        self._grids = {}

    # ---- what the viewer is told ----

    def summary(self):
        """What the viewer page shows: each stream and each variable name,
        with the streams it is in (as OceanVal's picker_state merges them)."""
        streams = []
        variables = {}
        for stream in self.streams:
            times = stream["times"]
            streams.append({
                "id": stream["id"],
                "pattern": stream["pattern"],
                "files": len(stream["files"]),
                "first_file": os.path.relpath(stream["files"][0], self.simdir),
                "steps": len(times),
                "first": times[0] if times else None,
                "last": times[-1] if times else None,
                "times": times,
                "has_times": stream["has_times"],
                "calendar": stream["calendar"],
                "resolution": stream["resolution"],
                "unreadable": stream["unreadable"],
                "error": stream["error"],
            })
            for name, about in stream["variables"].items():
                entry = variables.setdefault(name, {"long_name": "", "units": "", "streams": []})
                entry["long_name"] = entry["long_name"] or about["long_name"]
                entry["units"] = entry["units"] or about["units"]
                entry["streams"].append({
                    "stream": stream["id"],
                    "dims": about["dims"],
                    "shape": about["shape"],
                    "time": about["time_dim"] is not None,
                    "vertical": about["vertical"],
                    "levels": about["levels"],
                    "level_values": about["level_values"],
                    "level_units": about["level_units"],
                    "horizontal": about["horizontal"],
                    "kinds": plot_kinds(about),
                })
        return {
            "simdir": self.simdir,
            "ndown": self.ndown,
            "files": sum(len(stream["files"]) for stream in self.streams),
            "streams": streams,
            "variables": dict(sorted(variables.items(), key=lambda item: item[0].lower())),
        }

    def about(self, stream, name):
        try:
            return self.streams[int(stream)]["variables"][name]
        except (IndexError, KeyError, ValueError):
            raise KeyError(f"{name} is not in that file type") from None

    # ---- reading ----

    def _dataset(self, path):
        """An open Dataset of path, a few kept open. The caller holds LOCK."""
        if path in self._open:
            self._open.move_to_end(path)
            return self._open[path]
        ds = open_dataset(path)
        self._open[path] = ds
        while len(self._open) > 8:
            _, old = self._open.popitem(last=False)
            old.close()
        return ds

    def close(self):
        with LOCK:
            for ds in self._open.values():
                ds.close()
            self._open.clear()

    def _step(self, stream, step):
        stream = self.streams[int(stream)]
        steps = stream["steps"]
        step = min(max(int(step), 0), len(steps) - 1)
        _, index, at = steps[step]
        return stream, stream["files"][index], at, stream["times"][step]

    def field(self, stream, name, step=0, level=0):
        """The 2-D (or 1-D) slice of variable name at time step and level, as
        a loaded DataArray with its longitude/latitude as coords, and the
        time it is at."""
        about = self.about(stream, name)
        record, path, at, when = self._step(stream, step)
        select = {}
        if about["time_dim"]:
            select[about["time_dim"]] = at
        if about["vertical"]:
            select[about["vertical"]] = min(max(int(level), 0), about["levels"] - 1)
        with LOCK:
            ds = self._dataset(path)
            data = ds[name].isel(select)
            # anything else that is not horizontal is taken at its first index
            extra = {dim: 0 for dim in data.dims if dim not in about["horizontal"]}
            data = data.isel(extra).load()
            for key in (about["lon"], about["lat"]):
                if key is not None and key not in data.coords and key in ds.variables:
                    data = data.assign_coords({key: ds[key].load()})
        return data, (when if about["time_dim"] else None)

    def _cell_select(self, about, cell):
        return {dim: int(index) for dim, index in zip(about["horizontal"], cell)}

    def nearest_cell(self, stream, name, x, y, by_index=False):
        """The indices of the horizontal cell of variable name nearest the
        point (x, y), in the plot's coordinates: longitude and latitude if it
        has them (and the map was drawn with them, not by_index), otherwise
        the cell's own indices (x the last horizontal dimension's)."""
        about = self.about(stream, name)
        horizontal = about["horizontal"]
        if not horizontal:
            return []
        key = (int(stream), name)
        if key not in self._grids:
            with LOCK:
                ds = self._dataset(self.streams[int(stream)]["files"][0])
                lon = ds[about["lon"]].values if about["lon"] else None
                lat = ds[about["lat"]].values if about["lat"] else None
                sizes = [int(ds[name].sizes[dim]) for dim in horizontal]
            self._grids[key] = (lon, lat, sizes)
        lon, lat, sizes = self._grids[key]
        if len(horizontal) == 1:
            return [int(min(max(round(x), 0), sizes[0] - 1))]
        if by_index or lon is None or lat is None:
            return [int(min(max(round(y), 0), sizes[0] - 1)), int(min(max(round(x), 0), sizes[1] - 1))]
        if lon.ndim == 1 and lat.ndim == 1:
            first = horizontal[0]
            with LOCK:
                ds = self._dataset(self.streams[int(stream)]["files"][0])
                lat_first = ds[about["lat"]].dims[0] == first
            iy = int(np.nanargmin(np.abs(lat - y)))
            ix = int(np.nanargmin(np.abs(((lon - x) + 180) % 360 - 180)))
            return [iy, ix] if lat_first else [ix, iy]
        # a curvilinear grid: the cell nearest on the sphere
        rad = np.pi / 180
        dlat = (lat - y) * rad
        dlon = (lon - x) * rad
        a = np.sin(dlat / 2) ** 2 + np.cos(lat * rad) * np.cos(y * rad) * np.sin(dlon / 2) ** 2
        flat = int(np.nanargmin(np.where(np.isfinite(a), a, np.inf)))
        return [int(index) for index in np.unravel_index(flat, lon.shape)]

    def cell_position(self, stream, name, cell):
        """The longitude and latitude of a cell, if the variable has them."""
        about = self.about(stream, name)
        if not about["lon"] or not about["lat"]:
            return None
        with LOCK:
            ds = self._dataset(self.streams[int(stream)]["files"][0])
            select = {dim: index for dim, index in self._cell_select(about, cell).items()
                      if dim in ds[about["lon"]].dims}
            lon = float(ds[about["lon"]].isel(select).values)
            select = {dim: index for dim, index in self._cell_select(about, cell).items()
                      if dim in ds[about["lat"]].dims}
            lat = float(ds[about["lat"]].isel(select).values)
        return [lon, lat]

    def series(self, stream, name, cell, level=0, all_levels=False, stopped=lambda: False):
        """The values of variable name at one horizontal cell through every
        time of the stream, file by file (one cell of each is read), at one
        level, or at every level (all_levels). Returns (times, values), the
        values a list, or a list of lists of the levels."""
        about = self.about(stream, name)
        record = self.streams[int(stream)]
        select = self._cell_select(about, cell)
        if about["vertical"] and not all_levels:
            select[about["vertical"]] = min(max(int(level), 0), about["levels"] - 1)
        if not about["time_dim"]:
            with LOCK:
                values = self._dataset(record["files"][0])[name].isel(select)
                values = _first_of_others(values, about, keep=about["vertical"] if all_levels else None).load()
            return [None], [values.values.tolist()]
        by_file = {}
        for step, (_, index, at) in enumerate(record["steps"]):
            by_file.setdefault(index, []).append((step, at))
        values = [None] * len(record["steps"])
        for index, wanted in by_file.items():
            if stopped():
                raise Stopped()
            with LOCK:
                ds = self._dataset(record["files"][index])
                data = ds[name].isel(select)
                data = _first_of_others(data, about, keep=[about["time_dim"]] + ([about["vertical"]] if all_levels else []))
                data = data.isel({about["time_dim"]: [at for _, at in wanted]}).load()
            if all_levels:
                data = data.transpose(about["time_dim"], about["vertical"])
            for (step, _), value in zip(wanted, data.values.tolist()):
                values[step] = value
        return record["times"], values

    def profile(self, stream, name, cell, step=0):
        """The values of variable name at one cell and time, at every level."""
        about = self.about(stream, name)
        record, path, at, when = self._step(stream, step)
        select = self._cell_select(about, cell)
        if about["time_dim"]:
            select[about["time_dim"]] = at
        with LOCK:
            data = self._dataset(path)[name].isel(select)
            data = _first_of_others(data, about, keep=about["vertical"]).load()
        return about["level_values"], data.values.tolist(), (when if about["time_dim"] else None)


def _first_of_others(data, about, keep=None):
    """data with any dimension not kept taken at its first index."""
    keep = [keep] if isinstance(keep, str) else list(keep or [])
    extra = {dim: 0 for dim in data.dims if dim not in keep}
    return data.isel(extra) if extra else data


def plot_kinds(about):
    """The plots a variable can have, the first the one shown to start with:
    "map" (two horizontal dimensions), "line" (one: values along it),
    "series" (through time, at a cell), "profile" (through the levels, at a
    cell and time) and "hovmoller" (levels against time, at a cell)."""
    kinds = []
    horizontal = len(about["horizontal"])
    if horizontal >= 2:
        kinds.append("map")
    elif horizontal == 1:
        kinds.append("line")
    if about["time_dim"]:
        kinds.append("series")
    if about["vertical"] and about["levels"] > 1:
        kinds.append("profile")
        if about["time_dim"]:
            kinds.append("hovmoller")
    return kinds
