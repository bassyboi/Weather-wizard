import argparse
from pathlib import Path
import numpy as np
import xarray as xr

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
        # translate a gaussian "cell" eastward each step
        xc = 30 + t*3
        yc = 60
        g = 45.0 * np.exp(-(((x - xc)**2 + (y - yc)**2) / (2*12.0**2)))
        cube.append(g)
    cube = np.stack(cube, axis=0).astype(np.float32)
    times = np.array(
        np.datetime64("now") - np.arange(frames-1, -1, -1) * np.timedelta64(5, "m"),
        dtype="datetime64[ns]"
    )
    da = xr.DataArray(cube, dims=("time","y","x"), coords={"time": times})
    da.name = "reflectivity"
    return da

def load_input(inp: str, demo: bool):
    if demo or (inp is None):
        return make_demo_cube()
    p = Path(inp)
    if not p.exists():
        raise FileNotFoundError(f"Input not found: {inp}")
    if inp.endswith(".zarr"):
        ds = xr.open_zarr(inp)
    else:
        ds = xr.open_dataset(inp)
    # pick a reflectivity field
    for k in ("reflectivity","dbz","dBZ"):
        if k in ds:
            return ds[k]
    raise KeyError("Input must contain 'reflectivity' or 'dbz'")

def persistence_forecast(dbz_stack, steps=12, step_minutes=5):
    """Copy the LAST frame forward as a 60-min forecast at 5-min cadence."""
    last_dbz = dbz_stack.isel(time=-1).values
    rain = dbz_to_rainrate(last_dbz)
    fc = np.repeat(rain[None, ...], steps, axis=0)
    t0 = np.datetime64(dbz_stack.time.values[-1]) if "time" in dbz_stack.coords else np.datetime64("now")
    times = t0 + (np.arange(1, steps+1) * np.timedelta64(step_minutes, "m"))
    da = xr.DataArray(fc, dims=("time","y","x"),
                      coords={"time": times,
                              "y": dbz_stack.coords.get("y", np.arange(fc.shape[1])),
                              "x": dbz_stack.coords.get("x", np.arange(fc.shape[2]))},
                      name="rainrate")
    return da.to_dataset()

def main():
    ap = argparse.ArgumentParser(description="Persistence nowcast (60-min @5-min) with optional demo input.")
    ap.add_argument("--in", dest="inp", default=None, help="Input Zarr/NetCDF path containing reflectivity/dbz (optional if --demo).")
    ap.add_argument("--out", dest="out", default="outputs/nowcast_60min.nc", help="Output .nc or .zarr path.")
    ap.add_argument("--region", default="kununurra")
    ap.add_argument("--demo", action="store_true", help="Generate synthetic reflectivity if no input provided.")
    args = ap.parse_args()

    Path(Path(args.out).parent).mkdir(parents=True, exist_ok=True)
    dbz = load_input(args.inp, args.demo)
    ds = persistence_forecast(dbz, steps=12, step_minutes=5)
    if args.out.endswith(".zarr"):
        ds.to_zarr(args.out, mode="w")
    else:
        ds.to_netcdf(args.out)
    print(f"Wrote {args.out} :: vars={list(ds.data_vars)} dims={dict(ds.dims)} region={args.region}")

if __name__ == "__main__":
    main()
