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
from . import series as cell_series
from . import times as file_times
from .times import natural_key

# netCDF-C and HDF5 are not thread-safe, and the server answers in threads:
# every read of a file in this process holds it
LOCK = threading.RLock()

# read in a pool of processes once there are this many files to read
PARALLEL_FROM = cell_series.PARALLEL_FROM
WORKERS = 4
# the files a worker reads at a time for a time series
SERIES_BATCH = 16

# the names of vertical dimensions, as OceanVal tells them
_VERTICAL = ("depth", "lev", "sig", "s_rho", "z", "height", "nz", "deptht", "depthu", "depthv", "depthw")


class Stopped(Exception):
    """A scan that was stopped as it is no longer wanted."""


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
        # the processes time series are read in (see series)
        self._pool = None
        self._pool_cores = None
        self._pool_lock = threading.Lock()

    # ---- what the viewer is told ----

    def summary(self):
        """What the viewer page shows: each stream and each variable name
        that can be mapped, with the streams it can be mapped in (as
        OceanVal's picker_state merges them)."""
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
                if not mappable(about):
                    continue
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
        self._drop_pool()
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

    def combined(self, stream, names):
        """How to draw the sum of the variables names (one name is just that
        variable): a copy of the first one's about, with the names joined
        by " + " as its long_name (and name), and "units_differ" the units,
        if they are not all the same. Raises ValueError, in words for the
        user, if they cannot be added: they have to be in the file type, and
        have the same dimensions and sizes."""
        names = [names] if isinstance(names, str) else list(names)
        if not names:
            raise ValueError("Choose a variable.")
        if len(set(names)) != len(names):
            raise ValueError("A variable can only be added once.")
        try:
            abouts = [self.about(stream, name) for name in names]
        except KeyError as error:
            raise ValueError(str(error.args[0])) from None
        first = abouts[0]
        for name, about in zip(names[1:], abouts[1:]):
            if about["dims"] != first["dims"] or about["shape"] != first["shape"]:
                raise ValueError(
                    f"{names[0]} and {name} have different dimensions, so they cannot be added."
                )
        found = dict(first)
        units = [about["units"] for about in abouts]
        if len(names) > 1:
            found["name"] = " + ".join(names)
            found["long_name"] = " + ".join(about["long_name"] or name for name, about in zip(names, abouts))
            found["units"] = units[0] if len(set(units)) == 1 else ""
            found["units_differ"] = sorted(set(units)) if len(set(units)) > 1 else None
        else:
            found["name"] = names[0]
            found["units_differ"] = None
        return found

    def field(self, stream, names, step=0, level=0, missing=()):
        """The 2-D slice at time step and level of the variable names, or of
        the sum of them (see combined), as a loaded DataArray with its
        longitude/latitude as coords, and the time it is at. The sum is made
        here in xarray: missing is a list of (low, high) ranges (see
        missing_mask) that are set to NaN in each variable before it is
        added, and a cell that is missing in any of them is missing in the
        sum. How many cells were set missing is in attrs["n_missing"]."""
        names = [names] if isinstance(names, str) else list(names)
        about = self.combined(stream, names)
        record, path, at, when = self._step(stream, step)
        select = {}
        if about["time_dim"]:
            select[about["time_dim"]] = at
        if about["vertical"]:
            select[about["vertical"]] = min(max(int(level), 0), about["levels"] - 1)
        total = None
        n_missing = 0
        for name in names:
            with LOCK:
                ds = self._dataset(path)
                data = ds[name].isel(select)
                # anything else that is not horizontal is taken at its first index
                extra = {dim: 0 for dim in data.dims if dim not in about["horizontal"]}
                data = data.isel(extra).load()
                for key in (about["lon"], about["lat"]):
                    if key is not None and key not in data.coords and key in ds.variables:
                        data = data.assign_coords({key: ds[key].load()})
            mask = missing_mask(data, missing)
            data = data.where(~mask)
            n_missing += int(mask.sum())
            total = data if total is None else total + data
        total.attrs = {"n_missing": n_missing}
        return total, (when if about["time_dim"] else None)

    def limits(self, stream, names, level=0, missing=(), samples=12):
        """The range of the variable names (or their sum) over the whole run
        at level, from up to samples time steps spread evenly through it (one
        slice is read for each, so a long run costs a few reads, not one for
        every step): (low, high, symmetric), symmetric being whether the
        values span 0 (as plotting.colour_limits says). None if every
        sampled slice is missing."""
        from .plotting import colour_limits

        count = len(self.streams[int(stream)]["steps"])
        if count <= samples:
            steps = list(range(count))
        else:
            steps = sorted({round(i * (count - 1) / (samples - 1)) for i in range(samples)})
        low = high = None
        for step in steps:
            data, _ = self.field(stream, names, step, level, missing)
            values = data.values[np.isfinite(data.values)]
            if values.size:
                low = float(values.min()) if low is None else min(low, float(values.min()))
                high = float(values.max()) if high is None else max(high, float(values.max()))
        if low is None:
            return None
        (low, high), symmetric = colour_limits([low, high])
        return low, high, symmetric

    # ---- the time series of a cell ----

    def cell(self, stream, names, x, y, frame="lonlat", step=0, level=0, missing=()):
        """The cell a click on the map at (x, y) is for: {"j", "i", "lon",
        "lat"}, j and i its indices along the variable's two horizontal
        dimensions. frame is what the map's x and y are: "lonlat",
        "mercator" (the map with coastlines, which geoviews draws in Web
        Mercator) or "index" (the grid's own indices). The cell is the one
        nearest that has a value in the map shown (time step, level and the
        user's missing values), within 2 cells of the nearest of all, so a
        click on the coast is taken to the sea. Raises ValueError, in words
        for the user, if there is none."""
        about = self.combined(stream, names)
        data, _ = self.field(stream, names, step, level, missing)
        y_dim, x_dim = about["horizontal"][-2:]
        data = data.transpose(y_dim, x_dim)
        has_values = np.isfinite(data.values)
        lons = lats = None
        if frame != "index" and about["lon"] in data.coords and about["lat"] in data.coords:
            lons, lats = (np.asarray(part.transpose(y_dim, x_dim).values, dtype="float64")
                          for part in xr.broadcast(data[about["lon"]], data[about["lat"]]))
        if frame == "mercator":
            x, y = mercator_to_lonlat(x, y)
        ny, nx = data.shape

        if lons is None:
            def distance(jj, ii):
                return np.hypot(np.asarray(jj) - y, np.asarray(ii) - x)

            def apart(j0, i0, j1, i1):
                return np.hypot(j1 - j0, i1 - i0)

            j0, i0 = int(min(max(round(y), 0), ny - 1)), int(min(max(round(x), 0), nx - 1))
        else:
            def apart(j0, i0, j1, i1):
                dlon = (lons[j1, i1] - lons[j0, i0] + 180) % 360 - 180
                return np.hypot(dlon * np.cos(np.radians(lats[j0, i0])), lats[j1, i1] - lats[j0, i0])

            def distance(jj, ii):
                dlon = (lons[jj, ii] - x + 180) % 360 - 180
                found = np.hypot(dlon * np.cos(np.radians(y)), lats[jj, ii] - y)
                return np.where(np.isfinite(found), found, np.inf)

            whole = distance(slice(None), slice(None))
            j0, i0 = (int(at) for at in np.unravel_index(int(np.argmin(whole)), (ny, nx)))
        # the spacing of the grid there: the farthest of the cell's neighbours
        spacing = max(
            [float(apart(j0, i0, j1, i1)) for j1, i1 in ((j0 - 1, i0), (j0 + 1, i0), (j0, i0 - 1), (j0, i0 + 1))
             if 0 <= j1 < ny and 0 <= i1 < nx and np.isfinite(apart(j0, i0, j1, i1))] or [1.0]
        )
        if not float(distance(j0, i0)) <= 2 * spacing:
            raise ValueError("That point is outside the grid.")
        jj, ii = np.mgrid[max(j0 - 2, 0):min(j0 + 3, ny), max(i0 - 2, 0):min(i0 + 3, nx)]
        jj, ii = jj.ravel(), ii.ravel()
        keep = has_values[jj, ii]
        if not keep.any():
            raise ValueError("There is no data within 2 cells of that point in the map shown.")
        jj, ii = jj[keep], ii[keep]
        best = int(np.argmin(distance(jj, ii)))
        j, i = int(jj[best]), int(ii[best])
        found = {"j": j, "i": i, "lon": None, "lat": None}
        if about["lon"] in data.coords and about["lat"] in data.coords:
            lon, lat = (np.asarray(part.transpose(y_dim, x_dim).values, dtype="float64")[j, i]
                        for part in xr.broadcast(data[about["lon"]], data[about["lat"]]))
            if np.isfinite(lon) and np.isfinite(lat):
                found.update(lon=float(lon), lat=float(lat))
        return found

    def _pool_of(self, cores):
        """The pool of cores processes the series are read in, kept for the
        next series (made again if the cores are changed)."""
        with self._pool_lock:
            if self._pool is not None and self._pool_cores != cores:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
            if self._pool is None:
                self._pool = concurrent.futures.ProcessPoolExecutor(cores, mp_context=multiprocessing.get_context("spawn"))
                self._pool_cores = cores
            return self._pool

    def _drop_pool(self):
        with self._pool_lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def series(self, stream, names, level, j, i, missing=(), cores=1, progress=lambda done, total: None,
               stopped=lambda: False):
        """The values of the variable names (or their sum, see combined) at
        cell (j, i) and level through every time of the stream: (times,
        values), values a float64 array, NaN where missing. Only that cell
        is read of each file (ncviewer.series), in a pool of cores processes
        when there are PARALLEL_FROM files or more. missing are the user's
        missing values, set to NaN in each variable before they are added.
        progress(done, total) is told the files read, and stopped() is asked
        whether to give up (raising Stopped)."""
        names = [names] if isinstance(names, str) else list(names)
        about = self.combined(stream, names)
        if not about["time_dim"]:
            raise ValueError(f"{about['name']} has no time, so it has no time series.")
        record = self.streams[int(stream)]
        y_dim, x_dim = about["horizontal"][-2:]
        index = []
        for dim, size in zip(about["dims"], about["shape"]):
            if dim == about["time_dim"]:
                index.append(None)
            elif dim == about["vertical"]:
                index.append(min(max(int(level), 0), size - 1))
            elif dim == y_dim:
                index.append(int(j))
            elif dim == x_dim:
                index.append(int(i))
            else:
                index.append(0)
        wanted = sorted({at for _, at, _ in record["steps"]})
        tasks = {at: (record["files"][at], names, index) for at in wanted}
        read = {}
        progress(0, len(tasks))

        def keep(batch, results):
            for at, values in zip(batch, results):
                read[at] = values
            progress(len(read), len(tasks))

        parallel = cores > 1 and len(tasks) >= PARALLEL_FROM
        # in this process, one file at a time, so the page can count them; in the pool, batches small
        # enough that every worker has several and the count moves
        size = max(1, min(SERIES_BATCH, len(wanted) // (cores * 4))) if parallel else 1
        batches = [wanted[start:start + size] for start in range(0, len(wanted), size)]
        if parallel:
            try:
                pool = self._pool_of(cores)
                futures = {pool.submit(cell_series.read_files, [tasks[at] for at in batch]): batch
                           for batch in batches}
                try:
                    for future in concurrent.futures.as_completed(futures):
                        if stopped():
                            raise Stopped()
                        keep(futures[future], future.result())
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
            except (concurrent.futures.process.BrokenProcessPool, OSError):
                # workers that could not start (a __main__ they cannot import): the rest are read here
                self._drop_pool()
        for batch in batches:
            if all(at in read for at in batch):
                continue
            if stopped():
                raise Stopped()
            with LOCK:
                results = [cell_series.point_values(*tasks[at]) for at in batch]
            keep(batch, results)

        total = None
        for name in names:
            values = np.full(len(record["steps"]), np.nan)
            for number, (_, at, position) in enumerate(record["steps"]):
                found = missing_values(read[at][name], missing)
                values[number] = found[min(position, found.size - 1)] if found.ndim else found
            total = values if total is None else total + values
        return list(record["times"]), total


def _bound(value, dtype):
    """value as the data's own type, so that 1e20 matches a float32 1e20
    (which is not the float64 1e20), and a value too big for the type is
    infinite instead of an error."""
    if np.issubdtype(dtype, np.floating):
        limit = float(np.finfo(dtype).max)
        if abs(value) > limit:
            return np.inf if value > 0 else -np.inf
        return dtype.type(value)
    return value


def missing_values(values, ranges):
    """values (a numpy array) with the user's missing values (ranges, as in
    missing_mask) set to NaN, as a float array."""
    values = np.asarray(values)
    if values.dtype.kind != "f":
        values = values.astype("float64")
    mask = np.zeros(values.shape, dtype=bool)
    for low, high in ranges:
        if low > high:
            low, high = high, low
        low, high = _bound(low, values.dtype), _bound(high, values.dtype)
        mask |= (values >= low) & (values <= high)
    if mask.any():
        values = np.where(mask, np.nan, values)
    return values


def mercator_to_lonlat(x, y):
    """The longitude and latitude of a point in Web Mercator metres, which
    is how geoviews draws a map in Bokeh."""
    radius = 6378137.0
    lon = np.degrees(x / radius)
    lat = np.degrees(2 * np.arctan(np.exp(y / radius)) - np.pi / 2)
    return float(lon), float(lat)


def missing_mask(data, ranges):
    """A boolean DataArray, True where data (an xarray DataArray) has a value
    the user says is missing: ranges is a list of (low, high), a single
    number being (x, x), and both ends are included. NaN never matches."""
    mask = xr.zeros_like(data, dtype=bool)
    for low, high in ranges:
        if low > high:
            low, high = high, low
        low, high = _bound(low, data.dtype), _bound(high, data.dtype)
        mask = mask | ((data >= low) & (data <= high))
    return mask


def mappable(about):
    """Whether a variable can be drawn as a map: it has two horizontal dimensions."""
    return len(about["horizontal"]) >= 2
