# Backend (`backend/`)

FastAPI service + daily scheduler + the EDGES-3 calibration/temperature
pipeline.

## Files

| File | Role |
|---|---|
| `config.py` | **All paths and tunable defaults live here.** Env-driven; see the top-level `README.md` for the full table. |
| `backend_api.py` | FastAPI app — REST endpoints (`/manifest.json`, `/latest_run`, `/run_now`, `/save_outputs`, `/download/...`) and a static-file mount at `/` serving `$EDGES_OUTPUT_ROOT`. |
| `daemon.py` | Background scheduler that triggers `run_single_day.py` once per day at `EDGES_DAEMON_HOUR`. |
| `run_single_day.py` | The actual EDGES pipeline: receiver cal, S11 modelling, Dicke + noise-wave calibration, antenna temp, per-load actual temp matching. Writes the manifest + per-run npz/jpg trees. |
| `scan_dates.py` | Walks the raw data tree and writes `available_dates.json` so the UI can list what's runnable. |
| `io_utils.py` | Shared dataclasses (`Plot`), deterministic run-hash, manifest writer. |

## Running locally

```bash
conda activate edges
cd backend
pip install -r requirements.txt

# Start the API + scheduler
python -m uvicorn backend_api:app --host 127.0.0.1 --port 8000 --reload
```

## Running the pipeline manually

```bash
python run_single_day.py \
    --cal-date 2026_227 \
    --s11-date 2026_242_23 \
    --spec-date 2026_244_22_24_54 \
    --source user
```

The script prints a probe survey, then derived calibration temperatures,
then the noise-wave fit summary. The manifest lands in
`$EDGES_OUTPUT_ROOT/{source}/manifest.json`.

## Path configuration cheat sheet

`config.py` resolves every path from environment variables, with
sensible defaults for local development. Override any of these:

```bash
export EDGES_RAW_DATA_ROOT=/path/to/mro
export EDGES_OUTPUT_ROOT=/path/to/outputs
export EDGES_TEMP_LOG_DIR=/path/to/temperature_logger
export EDGES_DAEMON_ENABLED=1
export EDGES_DAEMON_HOUR=2
python -m uvicorn backend_api:app --host 0.0.0.0 --port 8000
```

See the top-level `README.md` for the full env-var table.
