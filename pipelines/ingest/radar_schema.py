"""
Radar schema helpers:
- Normalize typical radar inputs to a standard layout:
  dims: (time, y, x)
  variables: "reflectivity" (dBZ)
  coords: time [ns], y [index], x [index], optional lat[y,x], lon[y,x]
"""
from __future__ import annotations
import xarray as xr

REFL_CANDIDATES = ("reflectivity","dbz","dBZ","DBZ","Z")

def normalize(ds: xr.Dataset | xr.DataArray) -> xr.DataArray:
    """Return a DataArray named 'reflectivity' with dims (time,y,x)."""
    if isinstance(ds, xr.DataArray):
        da = ds
        name = ds.name or ""
        if name not in REFL_CANDIDATES:
            da = da.rename("reflectivity")
        else:
            da = da.rename("reflectivity")
        return _ensure_dims(da)

    for k in REFL_CANDIDATES:
        if k in ds:
            return _ensure_dims(ds[k].rename("reflectivity"))

    # If only one data_var, take it and rename
    if len(ds.data_vars) == 1:
        k = next(iter(ds.data_vars))
        return _ensure_dims(ds[k].rename("reflectivity"))

    raise KeyError("Could not find a reflectivity-like variable to normalize")

def _ensure_dims(da: xr.DataArray) -> xr.DataArray:
    # Try to coerce dims into (time, y, x)
    dims = list(da.dims)
    wanted = ["time","y","x"]
    # Insert missing dims
    if "time" not in dims:
        da = da.expand_dims({"time":[0]})
    dims = list(da.dims)

    # Simple reordering heuristics
    order = []
    for key in ("time","y","x"):
        if key in dims:
            order.append(key)
    for d in dims:
        if d not in order:
            order.append(d)
    da = da.transpose(*order)
    # Finally ensure the three core dims exist
    for key in ("time","y","x"):
        if key not in da.dims:
            raise ValueError(f"normalized reflectivity must include dim '{key}'")
    return da
