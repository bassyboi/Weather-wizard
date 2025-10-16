from __future__ import annotations
import xarray as xr


def set_compression(ds: xr.Dataset, var_names: list[str], complevel: int = 4) -> dict:
    """Return encoding dict with zlib compression for given var_names."""
    enc = {}
    for v in var_names:
        if v in ds:
            enc[v] = {"zlib": True, "complevel": complevel}
    return enc
