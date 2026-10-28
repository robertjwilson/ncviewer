"""The values of one cell through every file of a stream: the time series
the viewer shows when the map is clicked.

Only that cell is read from each file, and xarray is not used: h5py reads
the cell of a netCDF-4 file (one selection for all the file's times, so each
chunk is read once), netCDF4 a netCDF-3 file, and the values are decoded as
xarray decodes the map's (_FillValue and missing_value missing, then
scale_factor and add_offset). Many files are read in a pool of processes
(see ncviewer.catalogue.Catalogue.series). This module imports nothing else
of ncviewer, as the pool's workers import it.
"""

import os

import numpy as np

try:
    import h5py
except ImportError:  # pragma: no cover
    h5py = None

# read in a pool of processes once there are this many files to read (the scan's times too)
PARALLEL_FROM = 40

# netCDF's default fill value for floats (9.97e36) and larger: never data
_NC_FILL = 9.0e36


def default_cores():
    """Half the cores this process may use, and at least 1."""
    try:
        cores = len(os.sched_getaffinity(0))
    except AttributeError:
        # macOS and Windows
        cores = os.cpu_count() or 1
    return max(1, cores // 2)


def max_cores():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def _first(value):
    return np.asarray(value).ravel()[0]


def decode(raw, attrs):
    """raw (the values as stored) as xarray would give them: NaN where they
    are the _FillValue or missing_value (or netCDF's default fill), then
    scaled. Floats keep their type, so the user's missing values match them
    as they match the map's."""
    raw = np.asarray(raw)
    missing = np.zeros(raw.shape, dtype=bool)
    for key in ("_FillValue", "missing_value"):
        if key in attrs:
            missing |= raw == _first(attrs[key]).astype(raw.dtype)
    if raw.dtype.kind == "f":
        missing |= ~np.isfinite(raw) | (np.abs(raw) >= _NC_FILL)
    scale, offset = attrs.get("scale_factor"), attrs.get("add_offset")
    if scale is not None or offset is not None:
        values = raw.astype(np.result_type(*(_first(v) for v in (scale, offset) if v is not None), np.float32))
        if scale is not None:
            values = values * _first(scale)
        if offset is not None:
            values = values + _first(offset)
    elif raw.dtype.kind == "f":
        values = raw.copy()
    else:
        values = raw.astype("float64")
    values[missing] = np.nan
    return values


def _selection(index, shape):
    return tuple(slice(None) if at is None else min(max(int(at), 0), size - 1) for at, size in zip(index, shape))


def _read_h5py(path, names, index):
    found = {}
    with h5py.File(path, "r") as handle:
        for name in names:
            item = handle[name]
            attrs = {key: item.attrs[key] for key in ("_FillValue", "missing_value", "scale_factor", "add_offset") if key in item.attrs}
            found[name] = decode(item[_selection(index, item.shape)], attrs)
    return found


def _read_netcdf4(path, names, index):
    import netCDF4

    found = {}
    with netCDF4.Dataset(path) as handle:
        for name in names:
            variable = handle.variables[name]
            variable.set_auto_maskandscale(False)
            found[name] = decode(variable[_selection(index, variable.shape)], variable.__dict__)
    return found


def point_values(path, names, index):
    """{name: the values of the cell} of each of names in the file at path,
    index being one entry for each dimension of the variables: None for the
    one read whole (time), a position for the others. A 1-D array if one
    entry is None, a 0-D one otherwise."""
    if h5py is not None:
        try:
            return _read_h5py(path, names, index)
        except OSError:
            # not HDF5: netCDF-3
            pass
    return _read_netcdf4(path, names, index)


def read_files(tasks):
    """point_values of each (path, names, index) in tasks: a pool's task."""
    return [point_values(*task) for task in tasks]
