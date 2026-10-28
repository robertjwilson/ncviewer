"""Finding a simulation's output files: how many directories down they are,
which of them pass the file filters, and which stream (file type) each is.

Copied from OceanVal's create_recipes (folder_layout, simulation_paths,
_file_pattern, _passes_filters), which ncviewer does not import.
"""

import glob
import os
import re

_YEAR_DIR = re.compile(r"\d{4}")
_MONTH_DIRS = [f"{month:02d}" for month in range(1, 13)]


def file_pattern(path):
    """A file name with its dates blanked out, so one stream groups together."""
    name = os.path.basename(path)
    name = re.sub(r"\d{4,}", "**", name)
    return re.sub(r"\d{2,}", "**", name)


def passes_filters(path, exclude=None, require=None):
    """Whether a file is used, given exclude and require.

    Only the file's name is checked: it is left out if it contains any word
    in exclude, or if it lacks any word in require.
    """
    name = os.path.basename(path)
    return not any(word in name for word in exclude or ()) and all(
        word in name for word in require or ()
    )


def simulation_paths(simdir, ndown, exclude=None, require=None):
    """Every output file ndown directories below simdir, leaving out restart
    files and those the filters leave out."""
    pattern = os.path.join(glob.escape(simdir), *(["*"] * ndown), "*.nc")
    return [
        path
        for path in sorted(glob.glob(pattern))
        if "restart" not in os.path.basename(path)
        and passes_filters(path, exclude, require)
    ]


def _has_nc(directory):
    """Whether directory holds an output file (restart files do not count)."""
    with os.scandir(directory) as entries:
        return any(
            entry.name.endswith(".nc") and "restart" not in entry.name
            for entry in entries
            if entry.is_file()
        )


def folder_layout(simdir):
    """The layout of the output in simdir that tells how many directories
    down its files are, or None if it is not one ncviewer knows.

    {"pattern": "YYYY/MM/**.nc", "ndown": 2}: a year directory (four digits) with all
    twelve month directories, 01 to 12, one of which has output files.
    {"pattern": "MM/*.nc", "ndown": 1}: the twelve month directories, 01 to 12, in
    simdir itself, one of which has output files.
    {"pattern": "*.nc", "ndown": 0}: no subdirectories (those starting with a dot
    are not counted) and output files in the directory itself.

    Only a few directories are listed, as simdir may be on a network drive.
    Never raises.
    """
    try:
        with os.scandir(simdir) as entries:
            entries = sorted(entries, key=lambda entry: entry.name)
        directories = [
            entry for entry in entries if entry.is_dir() and not entry.name.startswith(".")
        ]
        if not directories:
            return {"pattern": "*.nc", "ndown": 0} if _has_nc(simdir) else None
        if set(_MONTH_DIRS) <= {entry.name for entry in directories}:
            if any(_has_nc(os.path.join(simdir, month)) for month in _MONTH_DIRS):
                return {"pattern": "MM/*.nc", "ndown": 1}
        for year in directories:
            if not _YEAR_DIR.fullmatch(year.name):
                continue
            with os.scandir(year.path) as inside:
                months = {entry.name for entry in inside if entry.is_dir()}
            if not set(_MONTH_DIRS) <= months:
                continue
            if any(_has_nc(os.path.join(year.path, month)) for month in _MONTH_DIRS):
                return {"pattern": "YYYY/MM/**.nc", "ndown": 2}
    except OSError:
        pass
    return None


def probe(simdir, ndown, exclude="", require=""):
    """What the files page says about the directory as it is typed in:
    whether it is there, the layout of its folders (whatever depth is typed),
    and how many output files are ndown directories below it that pass the
    file filters (exclude and require, as words separated by spaces), and
    how many there are without them."""
    filters = {"exclude": exclude.split() or None, "require": require.split() or None}
    filtered = bool(filters["exclude"] or filters["require"])
    found = {"directory": "empty", "ndown": None, "files": None, "all_files": None, "layout": None}
    if not simdir.strip():
        return found
    path = os.path.abspath(os.path.expanduser(simdir.strip()))
    if not os.path.isdir(path):
        found["directory"] = "missing"
        return found
    found["directory"] = "found"
    found["layout"] = folder_layout(path)
    try:
        depth = int(ndown)
        if depth < 0:
            raise ValueError
    except (TypeError, ValueError):
        depth = None
    found["ndown"] = depth
    if depth is not None:
        paths = simulation_paths(path, depth, **filters)
        found["files"] = len(paths)
        found["all_files"] = len(simulation_paths(path, depth)) if filtered else len(paths)
    return found
