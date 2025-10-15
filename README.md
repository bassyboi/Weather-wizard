Hybrid AI + Physics weather engine for Australia.

## Quickstart
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn serve.api_fastapi:app --reload
```

## Run Persistence Nowcast
```bash
# demo mode (no input data needed)
python pipelines/nowcast/persistence_nowcast.py --demo --out outputs/nowcast_60min.nc

# or with real input (NetCDF/Zarr containing 'reflectivity' or 'dbz')
python pipelines/nowcast/persistence_nowcast.py --in /path/to/latest_90min.zarr --out outputs/nowcast_60min.nc

```

## Query Nowcast Metadata via API
```bash
# generate a demo nowcast first
python pipelines/nowcast/persistence_nowcast.py --demo --out outputs/nowcast_60min.nc

# run API
uvicorn serve.api_fastapi:app --reload

# in another terminal
curl "http://127.0.0.1:8000/nowcast/meta?path=outputs/nowcast_60min.nc"
```
