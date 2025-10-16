"""
Geo helpers to attach CF-lite georeferencing without GDAL.
We build a simple regular lat/lon grid centered near a given lat/lon with dx/dy in km.
This is adequate for quick-look mapping and tiles; refine later with a proper CRS if needed.
"""
from __future__ import annotations
import numpy as np
import xarray as xr
from typing import Tuple

EARTH_KM_PER_DEG_LAT = 111.0

def make_latlon(ny: int, nx: int, center_lat: float, center_lon: float, dx_km: float, dy_km: float) -> Tuple[np.ndarray,np.ndarray]:
    """
    Create 2D lat/lon arrays for a regular grid centered at (center_lat, center_lon).
    dx_km, dy_km are nominal grid spacings (approx; lon scaled by cos(lat)).
    """
    lat = float(center_lat)
    lon = float(center_lon)
    dlat_deg = dy_km / EARTH_KM_PER_DEG_LAT
    dlon_deg = dx_km / (EARTH_KM_PER_DEG_LAT * max(np.cos(np.deg2rad(lat)), 1e-6))

    y_idx = np.arange(ny) - (ny - 1)/2.0
    x_idx = np.arange(nx) - (nx - 1)/2.0
    yy, xx = np.meshgrid(y_idx, x_idx, indexing="ij")
    lats = lat + yy * dlat_deg
    lons = lon + xx * dlon_deg
    return lats.astype(np.float32), lons.astype(np.float32)

def attach_cf(ds: xr.Dataset, lats: np.ndarray, lons: np.ndarray, var_name: str = "rainrate") -> xr.Dataset:
    """
    Attach lat/lon coords and minimal CF-like attributes.
    """
    if "time" not in ds[var_name].dims or "y" not in ds[var_name].dims or "x" not in ds[var_name].dims:
        raise ValueError("Expected dims (time,y,x) on target variable")

    ds = ds.copy()
    ds = ds.assign_coords(lat=(("y","x"), lats), lon=(("y","x"), lons))
    ds[var_name].attrs.update({
        "standard_name": "rainfall_rate",
        "long_name": "Rain rate",
        "units": "mm hr-1",
        "grid_mapping": "crs",
    })
    # minimal grid mapping variable (CF-lite)
    ds["crs"] = xr.DataArray(0, attrs={
        "grid_mapping_name": "latitude_longitude",
        "longitude_of_prime_meridian": 0.0,
        "semi_major_axis": 6378137.0,
        "inverse_flattening": 298.257223563,
    })
    # coordinate metadata
    ds["lat"].attrs.update({"standard_name":"latitude", "units":"degrees_north"})
    ds["lon"].attrs.update({"standard_name":"longitude", "units":"degrees_east"})

    # convenience global attrs
    ds.attrs.update({
        "Conventions": "CF-1.8 (lite)",
        "title": "Weather Wizard Nowcast (Persistence)",
        "institution": "Crop Crusaders / Weather Wizard",
        "source": "Persistence nowcast demo",
        "history": "Created by weather-wizard pipelines/preprocess/geo.py",
        "reference": "https://github.com/<your-org>/weather-wizard",
    })
    return ds
