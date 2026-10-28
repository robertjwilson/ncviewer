"""Small simulations written for the tests: NEMO-like output, a file per
stream per month, on a curvilinear grid whose land cells have 0 longitude
and latitude, and a regular lon-lat one."""

import os

import numpy as np
import xarray as xr


def _times(year, month, per_month, calendar):
    days = 30 if calendar == "360_day" else [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    start = (year - 2000) * (360 if calendar == "360_day" else 365) + (
        (month - 1) * 30 if calendar == "360_day"
        else sum([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][: month - 1])
    )
    if per_month == 1:
        return np.array([start + days / 2])
    return start + np.arange(per_month) + 0.5


def nemo_month(path_t, path_p, year, month, per_month=1, calendar="noleap", ny=12, nx=10, nz=4):
    time = _times(year, month, per_month, calendar)
    y, x = np.meshgrid(np.arange(ny), np.arange(nx), indexing="ij")
    lon = -10 + x * 0.5 + y * 0.05
    lat = 48 + y * 0.4 + x * 0.02
    land = (x < 2) & (y < 3)
    lon = np.where(land, 0.0, lon).astype("float32")
    lat = np.where(land, 0.0, lat).astype("float32")
    depth = np.array([1.0, 5.0, 20.0, 50.0][:nz])
    base = 10 + month + (year - 2000) * 0.1
    temp = base + 0.1 * y[None, None] - 0.05 * depth[None, :, None, None] + time[:, None, None, None] * 0
    temp = np.where(land[None, None], np.nan, temp).astype("float32")
    time_attrs = {"units": "days since 2000-01-01 00:00:00", "calendar": calendar, "standard_name": "time", "axis": "T"}
    coords = {
        "nav_lon": (("y", "x"), lon, {"standard_name": "longitude", "units": "degrees_east"}),
        "nav_lat": (("y", "x"), lat, {"standard_name": "latitude", "units": "degrees_north"}),
        "deptht": ("deptht", depth, {"units": "m", "positive": "down", "long_name": "Vertical T levels"}),
        "time_counter": ("time_counter", time, time_attrs),
    }
    grid_t = xr.Dataset(
        {
            "votemper": (("time_counter", "deptht", "y", "x"), temp,
                         {"long_name": "Temperature", "units": "degC", "coordinates": "nav_lon nav_lat"}),
            "sossheig": (("time_counter", "y", "x"), temp[:, 0] * 0.01,
                         {"long_name": "sea surface height", "units": "m"}),
            "time_counter_bnds": (("time_counter", "nb"), np.stack([time - 0.5, time + 0.5], axis=1)),
        },
        coords=coords,
    )
    grid_t["time_counter"].attrs["bounds"] = "time_counter_bnds"
    ptrc = xr.Dataset(
        {
            "chl": (("time_counter", "deptht", "y", "x"), temp / 10,
                    {"long_name": "Chlorophyll", "units": "mg m-3"}),
            "votemper": (("time_counter", "deptht", "y", "x"), temp,
                         {"long_name": "Temperature", "units": "degC"}),
        },
        coords=coords,
    )
    encoding = {"time_counter": {"dtype": "float64"}}
    grid_t.to_netcdf(path_t, encoding=encoding)
    ptrc.to_netcdf(path_p, encoding=encoding)


def nemo_simulation(root, years=(2000, 2001), layout="YYYY/MM", per_month=1, calendar="noleap"):
    """A NEMO-like simulation under root, its files laid out as layout says:
    "YYYY/MM" (2 down), "MM" (1 down) or "flat" (0 down). Returns root."""
    for year in years:
        for month in range(1, 13):
            if layout == "YYYY/MM":
                folder = os.path.join(root, str(year), f"{month:02d}")
            elif layout == "MM":
                folder = os.path.join(root, f"{month:02d}")
            else:
                folder = root
            os.makedirs(folder, exist_ok=True)
            stamp = f"{year}{month:02d}01_{year}{month:02d}28"
            nemo_month(
                os.path.join(folder, f"amm7_1m_{stamp}_grid_T.nc"),
                os.path.join(folder, f"amm7_1m_{stamp}_ptrc_T.nc"),
                year, month, per_month=per_month, calendar=calendar,
            )
    return root


def regular_file(path, nt=3):
    lon = np.linspace(-20, 10, 31)
    lat = np.linspace(40, 65, 26)
    time = np.arange(nt) * 30.0 + 15
    values = np.sin(lon[None, None] / 10) + np.cos(lat[None, :, None] / 10) + time[:, None, None] / 100
    ds = xr.Dataset(
        {"sst": (("time", "lat", "lon"), values.astype("float32"), {"long_name": "Sea surface temperature", "units": "K"}),
         "station_temp": (("time",), values[:, 5, 5], {"units": "K"})},
        coords={
            "lon": ("lon", lon, {"units": "degrees_east"}),
            "lat": ("lat", lat, {"units": "degrees_north"}),
            "time": ("time", time, {"units": "days since 1990-01-01", "calendar": "standard"}),
        },
    )
    ds.to_netcdf(path)
    return path
