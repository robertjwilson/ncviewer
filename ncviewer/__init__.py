"""ncviewer: a browser window for looking at the contents of model simulation output.

Kept light: the workers that read the files' times import this package.
"""

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("ncviewer")
except PackageNotFoundError:  # pragma: no cover - not installed
    __version__ = "0+unknown"
