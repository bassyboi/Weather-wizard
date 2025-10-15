from fastapi import FastAPI, Query
from pathlib import Path
import xarray as xr
import os

app = FastAPI(title="Weather Wizard", version="0.0.2")

@app.get("/")
def root():
    return {"message": "Weather Wizard base running"}

@app.get("/health")
def health():
    return {"status": "ok", "data_dir": os.environ.get("DATA_DIR","unset")}

@app.get("/nowcast/meta")
def nowcast_meta(path: str = Query("outputs/nowcast_60min.nc", description="Path to .nc or .zarr nowcast")):
    p = Path(path)
    if not p.exists():
        return {"exists": False, "path": path}
    if path.endswith(".zarr"):
        ds = xr.open_zarr(path)
    else:
        ds = xr.open_dataset(path)
    return {
        "exists": True,
        "path": path,
        "vars": list(ds.data_vars),
        "dims": {k:int(v) for k,v in ds.dims.items()},
        "coords": list(ds.coords)
    }
