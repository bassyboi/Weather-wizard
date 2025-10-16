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

## Run with Docker
```bash
# build & run with docker-compose
docker compose up --build

# or plain docker
docker build -f ops/docker/Dockerfile.api -t weather-wizard/api:dev .
docker run --rm -p 8000:8000 -e DATA_DIR=/data -v $(pwd)/outputs:/app/outputs -v $(pwd)/data:/data weather-wizard/api:dev

# health check
curl http://127.0.0.1:8000/health

CI (GitHub Actions)
	•	ci.yml installs Python deps, caches pip, runs import tests, and boots the API to verify /health.
	•	container-build.yml builds the container image on demand. Uncomment GHCR steps to push.
```

## Georeferenced Nowcast (CF-lite)
The persistence nowcast can attach lat/lon and CF-like metadata (no GDAL required).

```bash
# Demo (no input data): writes outputs/nowcast_60min.nc with lat/lon
python pipelines/nowcast/persistence_nowcast.py --demo --out outputs/nowcast_60min.nc

# Custom grid center / spacing (approximate degrees from km; lon scaled by cos(lat))
python pipelines/nowcast/persistence_nowcast.py --demo \
  --center-lat -27.62 --center-lon 151.77 --dx-km 2.0 --dy-km 2.0 \
  --out outputs/nowcast_ddowns_60min.nc

# Disable CF-lite geo metadata if you only need raw grids
python pipelines/nowcast/persistence_nowcast.py --demo --no-geo --out outputs/nowcast_raw_60min.nc

API tip: After generating the file, query metadata:

uvicorn serve.api_fastapi:app --reload
curl "http://127.0.0.1:8000/nowcast/meta?path=outputs/nowcast_60min.nc"

