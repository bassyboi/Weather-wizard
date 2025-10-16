import argparse
from pathlib import Path
import numpy as np
import xarray as xr

from pipelines.ingest.radar_schema import normalize
from pipelines.preprocess.geo import make_latlon, attach_cf
from pipelines.preprocess.cf_utils import set_compression

def dbz_to_rainrate(dbz):
    # Marshall–Palmer: Z = 200 R^1.6, with Z = 10^(dBZ/10)
    Z = np.power(10.0, dbz/10.0)
    R = np.power(np.maximum(Z/200.0, 0.0), 1.0/1.6)
    return R.astype(np.float32)

def make_demo_cube(ny=120, nx=160, frames=12):
    """Generate a synthetic 60-min reflectivity stack (dBZ) at 5-min steps."""
    y, x = np.mgrid[0:ny, 0:nx]
    cube = []
    for t in range(frames):
        xc = 30 + t*3
        yc = 60
        g = 45.0 * np.exp(-(((x - xc)**2 + (y - yc)**2) / (2*12.0**2)))
        cube.append(g)
    cube = np.stack(cube, axis=0).astype(np.float32)
    times = np.array(
        np.datetime64("now") - np.arange(frames-1, -1, -1) * np.timedelta64(5, "m"),
        dtype="datetime64[ns]"
    )
    da = xr.DataArray(cube, dims=("time","y","x"), coords={"time": times}, name="reflectivity")
    return da

def load_input(inp: str | None, demo: bool) -> xr.DataArray:
    if demo or (inp is None):
        return make_demo_cube()
    p = Path(inp)
    if not p.exists():
        raise FileNotFoundError(f"Input not found: {inp}")
    ds = xr.open_zarr(inp) if inp.endswith(".zarr") else xr.open_dataset(inp)
    return normalize(ds)

def persistence_forecast(dbz_stack: xr.DataArray, steps=12, step_minutes=5) -> xr.Dataset:
    """Copy the LAST frame forward as a 60-min forecast at 5-min cadence."""
    last_dbz = dbz_stack.isel(time=-1).values
    rain = dbz_to_rainrate(last_dbz)
    fc = np.repeat(rain[None, ...], steps, axis=0)
    t0 = np.datetime64(dbz_stack.time.values[-1]) if "time" in dbz_stack.coords else np.datetime64("now")
    times = t0 + (np.arange(1, steps+1) * np.timedelta64(step_minutes, "m"))
    da = xr.DataArray(fc, dims=("time","y","x"), coords={"time": times}, name="rainrate")
    return da.to_dataset()

def main():
    ap = argparse.ArgumentParser(description="Persistence nowcast → CF-lite georeferenced output.")
    ap.add_argument("--in", dest="inp", default=None, help="Input Zarr/NetCDF with reflectivity/dbz (optional if --demo).")
    ap.add_argument("--out", dest="out", default="outputs/nowcast_60min.nc", help="Output .nc or .zarr")
    ap.add_argument("--region", default="kununurra")
    ap.add_argument("--demo", action="store_true", help="Use synthetic reflectivity if no input provided.")
    ap.add_argument("--geo", dest="geo", action="store_true", default=True, help="Attach lat/lon grid and CF-lite metadata.")
    ap.add_argument("--no-geo", dest="geo", action="store_false", help="Disable lat/lon coords and CF-lite metadata.")
    ap.add_argument("--center-lat", type=float, default=-15.77, help="Grid center latitude (deg).")
    ap.add_argument("--center-lon", type=float, default=128.74, help="Grid center longitude (deg).")
    ap.add_argument("--dx-km", type=float, default=1.5, help="Grid spacing east-west (km).")
    ap.add_argument("--dy-km", type=float, default=1.5, help="Grid spacing north-south (km).")
    args = ap.parse_args()

    Path(Path(args.out).parent).mkdir(parents=True, exist_ok=True)
    dbz = load_input(args.inp, args.demo)
    ds = persistence_forecast(dbz, steps=12, step_minutes=5)

    if args.geo:
        ny = ds.dims.get("y", None)
        nx = ds.dims.get("x", None)
        if ny is None or nx is None:
            raise ValueError("Expected dims (y,x) in forecast output")
        lats, lons = make_latlon(ny, nx, args.center_lat, args.center_lon, args.dx_km, args.dy_km)
        ds = attach_cf(ds, lats, lons, var_name="rainrate")

    # Write with light compression for NetCDF
    if args.out.endswith(".zarr"):
        ds.to_zarr(args.out, mode="w")
    else:
        enc = set_compression(ds, ["rainrate"])
        ds.to_netcdf(args.out, encoding=enc)
    print(f"Wrote {args.out} :: vars={list(ds.data_vars)} dims={dict(ds.dims)} region={args.region} geo={args.geo}")

if __name__ == "__main__":
    main()
