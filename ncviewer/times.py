"""The times of each output file, read from its time axis alone.

As OceanVal's output_years does it: h5py reads only the time variable of a
netCDF-4 file (netCDF4 reads every variable's metadata as it opens one), and
netCDF4 reads netCDF-3 files. The times are decoded with cftime, so any
calendar works. This module imports nothing else of ncviewer, as the pool's
workers import it.

What was read is kept in a cache (see load_cache/save_cache), checked by
each file's size and modification time, so a directory opened again reads
only its new or changed files.
"""

import contextlib
import os
import pickle
import re
import tempfile

import cftime
import numpy as np

try:
    import h5py
except ImportError:  # pragma: no cover
    h5py = None

# netCDF's default fill value for doubles, and larger: never a time
_NC_FILL = 9.0e36

# the attributes of a variable that say how to read its times
_ATTRIBUTES = (
    "units", "calendar", "axis", "standard_name", "CLASS",
    "_FillValue", "missing_value", "scale_factor", "add_offset",
)

CACHE_VERSION = 1


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, np.ndarray) and value.size == 1:
        return _text(value.item())
    return str(value)


def natural_key(path):
    """path in the order of the numbers in it, so 10 follows 9."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path)]


def _score(name, attrs, dimensions):
    """How likely a 1-D variable with units "... since ..." is the file's time
    axis: a coordinate variable first, then axis T, then standard_name time."""
    return (
        2 * (name in dimensions)
        + (_text(attrs.get("axis", "")) == "T")
        + (_text(attrs.get("standard_name", "")) == "time")
    )


def _decode(name, values, attrs):
    """{"name", "calendar", "times"}: the times in values, as ISO strings
    (which sort as the times do), decoded with attrs' units and calendar.
    Missing values are left out."""
    values = np.asarray(values, dtype="float64").ravel()
    keep = np.isfinite(values) & (np.abs(values) < _NC_FILL)
    for key in ("_FillValue", "missing_value"):
        if key in attrs:
            keep &= values != float(np.asarray(attrs[key]).ravel()[0])
    if "scale_factor" in attrs:
        values = values * float(np.asarray(attrs["scale_factor"]).ravel()[0])
    if "add_offset" in attrs:
        values = values + float(np.asarray(attrs["add_offset"]).ravel()[0])
    calendar = _text(attrs.get("calendar", "standard")) or "standard"
    # the index in the file of each time, as missing ones are left out
    index = np.flatnonzero(keep)
    dates = cftime.num2date(values[keep], _text(attrs["units"]), calendar) if index.size else []
    return {
        "name": name,
        "calendar": calendar,
        "times": [date.isoformat(sep=" ") for date in dates],
        "index": [int(i) for i in index],
    }


def _read_h5py(path):
    with h5py.File(path, "r") as handle:
        best = None
        for name, item in handle.items():
            if not isinstance(item, h5py.Dataset) or item.ndim != 1:
                continue
            # only these: a coordinate's REFERENCE_LIST, say, need not be read
            attrs = {key: item.attrs[key] for key in _ATTRIBUTES if key in item.attrs}
            if " since " not in _text(attrs.get("units", "")):
                continue
            # a netCDF coordinate variable is a dimension scale
            dimensions = {name} if _text(attrs.get("CLASS", "")) == "DIMENSION_SCALE" else set()
            score = _score(name, attrs, dimensions)
            if best is None or score > best[0]:
                best = (score, name, item, attrs)
        if best is None:
            return {"name": None, "calendar": None, "times": [], "index": []}
        return _decode(best[1], best[2][()], best[3])


def _read_netcdf4(path):
    import netCDF4

    with netCDF4.Dataset(path) as handle:
        best = None
        for name, variable in handle.variables.items():
            if variable.ndim != 1 or " since " not in str(getattr(variable, "units", "")):
                continue
            score = _score(name, variable.__dict__, variable.dimensions)
            if best is None or score > best[0]:
                best = (score, name, variable)
        if best is None:
            return {"name": None, "calendar": None, "times": [], "index": []}
        variable = best[2]
        variable.set_auto_maskandscale(False)
        return _decode(best[1], variable[:], variable.__dict__)


def file_times(path):
    """The times of a file's time axis (see _decode), with an "error" if it
    could not be read. Never raises."""
    try:
        if h5py is not None:
            try:
                return _read_h5py(path)
            except OSError:
                # not HDF5
                pass
        return _read_netcdf4(path)
    except Exception as error:  # noqa: BLE001 - a file that cannot be read is reported
        return {"name": None, "calendar": None, "times": [], "index": [], "error": str(error)}


def _stat(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns)


def cache_path():
    """Where what was read is kept: $NCVIEWERCACHE, else ~/.ncviewercache."""
    return os.environ.get("NCVIEWERCACHE") or os.path.join(os.path.expanduser("~"), ".ncviewercache")


def load_cache(paths):
    """{path: times} kept for those of paths whose size and modification time
    are unchanged since. Never raises."""
    try:
        with open(cache_path(), "rb") as handle:
            kept = pickle.load(handle)
        if kept.get("version") != CACHE_VERSION:
            return {}
        files = kept["files"]
    except Exception:  # noqa: BLE001 - a cache must not stop anything
        return {}
    found = {}
    for path in paths:
        entry = files.get(path)
        if entry and tuple(entry["stat"]) == _stat(path):
            found[path] = entry["times"]
    return found


def save_cache(read):
    """Add read ({path: times}) to the cache, keeping what else is in it.
    Written whole to a file beside it first, so a reader never sees half of
    it. Never raises."""
    if not read:
        return
    path = cache_path()
    try:
        with open(path, "rb") as handle:
            kept = pickle.load(handle)
        if kept.get("version") != CACHE_VERSION:
            kept = None
    except Exception:  # noqa: BLE001
        kept = None
    kept = kept or {"version": CACHE_VERSION, "files": {}}
    for name, times in read.items():
        if times.get("error"):
            continue
        stat = _stat(name)
        if stat is not None:
            kept["files"][name] = {"stat": stat, "times": times}
    with contextlib.suppress(Exception):
        folder = os.path.dirname(path) or "."
        handle, partial = tempfile.mkstemp(dir=folder, prefix=".ncviewercache.")
        try:
            with os.fdopen(handle, "wb") as out:
                pickle.dump(kept, out)
            os.replace(partial, path)
        finally:
            with contextlib.suppress(OSError):
                os.remove(partial)
