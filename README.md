# EDGES-3 Web Interface

A web UI for inspecting the daily calibration / antenna-temperature pipeline
output from the EDGES-3 instrument. The project is structured so the React
frontend and the FastAPI backend can be deployed together (local
development) or split across machines (production on the ASU enterprise
cluster).

```
edges-interface/
├── backend/          # FastAPI server + daily daemon + EDGES pipeline
├── frontend/         # React + TypeScript + Vite SPA
├── README.md         # ← you are here
├── LICENSE
└── .gitignore
```

The two halves communicate over HTTP; the frontend never reads files from
disk directly — it asks the backend for JSON (`/manifest.json`,
`/latest_run`) and for binary files (`.npz`, `.jpg`) which the backend
serves from its `OUTPUT_ROOT` static mount.

---

## Quick start (local development)

```bash
# 1. Backend
conda activate edges          # python 3.11 with edges-* packages
cd backend
pip install -r requirements.txt
python -m uvicorn backend_api:app --host 127.0.0.1 --port 8000 --reload

# 2. Frontend (in a second terminal)
cd frontend
npm install
npm run dev                   # serves on http://localhost:5173
```

By default the frontend points at `http://127.0.0.1:8000` (see
`frontend/src/utils/baseURL.ts`). With both running you can browse the
output, trigger a manual pipeline run from the UI, and download zips of
the current `daemon/` or `user/` tree.

---

## Layout

| Path | Purpose |
|---|---|
| `backend/` | Python service |
| `backend/config.py` | All env-driven paths & tunable defaults (single source of truth) |
| `backend/backend_api.py` | FastAPI app — REST endpoints + static-file mount |
| `backend/daemon.py` | Background scheduler that re-runs the pipeline daily |
| `backend/run_single_day.py` | The actual EDGES calibration + temperature pipeline |
| `backend/scan_dates.py` | Scans the raw-data tree and writes `available_dates.json` |
| `backend/io_utils.py` | Shared dataclasses (`Plot`), hashing, manifest writing |
| `frontend/` | React + Vite SPA |
| `frontend/src/utils/baseURL.ts` | Where the frontend reads `VITE_API_URL` from |
| `frontend/src/state/RunContext.tsx` | Frontend cache of `/latest_run` and refresh counter |
| `outputs/` | **NOT** checked into git — runtime artefacts (manifest, runs, zip downloads) |

---

## Paths you may want to change

Every path the backend uses is environment-driven. The defaults are in
`backend/config.py`; override any of them with an env var before launching
the backend. The frontend has only one configurable path, set at build
time via Vite.

### Backend (runtime env vars)

| Env var | Default | What it controls |
|---|---|---|
| `EDGES_RAW_DATA_ROOT` | `/Users/joshualewis/EdgesTestData/data5/edges/data/EDGES3_data/MRO` (local) or `/data5/edges/data/EDGES3_data/MRO` (cluster) | Root of the raw `.acq` + `.log` tree the pipeline reads |
| `EDGES_OUTPUT_ROOT` | `/Users/joshualewis/EdgesTestData/outputs` (local) or `/data5/edges/edges_outputs` (cluster) | Where the manifest, daemon/user trees, saved zips, and run history are written |
| `EDGES_TEMP_LOG_FILE` | `$EDGES_RAW_DATA_ROOT/temperature_logger/temperature.log` | Single-file temperature log (legacy) |
| `EDGES_TEMP_LOG_DIR` | `$EDGES_TEMP_LOG_FILE`'s parent | Directory of `*.log` files; every file in here is read by the temperature lookup |
| `EDGES_PYTHON` | current interpreter (`sys.executable`) | Python the daemon shells out to when running the pipeline |
| `EDGES_DAEMON_HOUR` | `2` | Hour of day (0-23) at which the daily daemon fires |
| `EDGES_DAEMON_ENABLED` | `0` | Set to `1` / `true` to start the in-process scheduler on backend startup |
| `EDGES_PROBE_AMBIENT` | `100` | Temperature-log probe for ambient cal |
| `EDGES_PROBE_HOT` | `102` | Temperature-log probe for hot cal |
| `EDGES_PROBE_LNA` | `100` | Temperature-log probe for LNA cal |
| `EDGES_PROBE_COLD_LOAD` | `152` | Temperature-log probe for the cold load (informational) |

Calibration temperatures are now auto-derived from the temperature log at
the matching calibration time and passed straight to the EDGES receiver
calibration — there are no user-tunable `tcold` / `thot` / `tcab` /
`tload` / `tns` setpoints anymore. If no probe reading is available at
the calibration time the pipeline falls back to the internal constants
(`306.5`, `393.22`, `306.5` K) and logs a warning.

The values above are documented programmatically in
`backend/config.py::describe()`. Run `python backend/config.py` to print
the resolved configuration.

### Frontend (build-time env var)

| Env var | Default | What it controls |
|---|---|---|
| `VITE_API_URL` | `http://127.0.0.1:8000` | Base URL the frontend uses for `/manifest.json`, `/latest_run`, `/save_outputs`, and the static-file mount. **Set this at build time** before running `npm run build` when the backend is on a different host (e.g. SSH cluster). |

The frontend reads it in `frontend/src/utils/baseURL.ts`. There is no
runtime override — Vite inlines it during the build.

### What happens when frontend and backend are split (e.g. SSH port)

The two halves have **no shared filesystem requirement** — the only
thing that travels between them is JSON and HTTP-served binary files.
You can host them on the same box, on separate boxes, or one local +
one remote.

#### SSH deployment (backend on the cluster, frontend anywhere)

```text
  ┌──────────────┐         ┌──────────────────────────┐
  │  any browser │ ──HTTP─▶│  cluster host           │
  │  (user)      │         │  ┌────────────────────┐  │
  └──────────────┘         │  │ backend (uvicorn)  │  │
                           │  │  :8000             │  │
                           │  │  serves API +      │  │
                           │  │  OUTPUT_ROOT       │  │
                           │  └────────────────────┘  │
                           │  ┌────────────────────┐  │
                           │  │ $EDGES_RAW_DATA_   │  │
                           │  │   ROOT (.acq+log)  │  │
                           │  └────────────────────┘  │
                           │  ┌────────────────────┐  │
                           │  │ $EDGES_OUTPUT_     │  │
                           │  │   ROOT (manifest,  │  │
                           │  │   runs, saved/)    │  │
                           │  └────────────────────┘  │
                           └──────────────────────────┘
```

Step-by-step:

1. **Clone the repo on the SSH host.** The backend only needs the
   `backend/` directory; the frontend tree can be ignored on this box if
   you prefer.
   ```bash
   git clone <repo-url> edges-interface
   cd edges-interface
   ```

2. **Create the conda env (one-time).**
   ```bash
   conda create -n edges python=3.11 -c conda-forge \
       edges-analysis edges-io pygsdata read-acq astropy
   conda activate edges
   pip install -r backend/requirements.txt
   ```

3. **Point the env vars at the cluster paths.** Add these to your
   `~/.bashrc` (or set them in the systemd unit / launch script — see
   below):
   ```bash
   export EDGES_RAW_DATA_ROOT=/data5/edges/data/EDGES3_data/MRO
   export EDGES_OUTPUT_ROOT=/data5/edges/edges_outputs
   export EDGES_TEMP_LOG_DIR=/data5/edges/data/EDGES3_data/MRO/temperature_logger
   export EDGES_DAEMON_ENABLED=1
   export EDGES_DAEMON_HOUR=2
   ```
   `config.py::_default_raw_root()` already picks `/data5/...` if
   `EDGES_RAW_DATA_ROOT` is unset *and* that path exists, so step 3 is
   technically optional — but being explicit is safer.

4. **Start the backend** so it listens on a public interface (NOT
   `127.0.0.1` — that's loopback only and won't accept external
   connections):
   ```bash
   conda activate edges
   cd edges-interface
   python -m uvicorn backend.backend_api:app --host 0.0.0.0 --port 8000
   ```
   The `--host 0.0.0.0` change is the **only** command-line flag
   difference from local dev.

5. **Build the frontend bundle on any host with Node** (could be your
   laptop, not the SSH machine):
   ```bash
   cd frontend
   npm install
   VITE_API_URL=https://edges.example.com npm run build
   ```
   The resulting `frontend/dist/` is a fully static bundle. Upload it
   to any static host (nginx on the same SSH box, GitHub Pages, S3 +
   CloudFront, …). It needs to be reachable by the user's browser.

6. **CORS is already permissive** (`allow_origins=["*"]` in
   `backend/backend_api.py`) so no extra headers are needed even when
   the frontend and backend live on different origins.

7. **(Recommended, not required) Put HTTPS in front.** Browsers won't
   load `http://` resources from an `https://` page. Either:
   * Terminate TLS at a reverse proxy (nginx, Caddy, traefik) in front
     of uvicorn on port 8000, and have the frontend talk to
     `https://edges.example.com`. Set `VITE_API_URL=https://...` at
     build time.
   * Or use a tunnel / cloudflare / etc.

#### Daemon on SSH (the easy way vs the robust way)

The backend has an **in-process scheduler** that fires
`run_single_day.py` once a day at `EDGES_DAEMON_HOUR`. It's fine for
local dev and for a single-user SSH install. For a long-running
production deployment on SSH use **systemd** — ready-made unit files
live in `scripts/systemd/`:

```text
scripts/
├── edges-pipeline.sh                # wrapper: scan dates + invoke run_single_day.py
└── systemd/
    ├── README.md                    # full install walkthrough
    ├── edges-api.service            # Option A: long-running uvicorn
    ├── edges-pipeline.service       # Option B part 1: oneshot daily run
    └── edges-pipeline.timer         # Option B part 2: calendar trigger
```

**Option A — long-running uvicorn (simpler):** drop in
`edges-api.service` and `systemctl enable --now edges-api.service`.
The in-process scheduler runs inside uvicorn at `EDGES_DAEMON_HOUR`
UTC every day. Set `EDGES_DAEMON_ENABLED=1` in the unit file.

**Option B — systemd timer (more robust):** drop in both
`edges-pipeline.service` and `edges-pipeline.timer`. The timer
triggers the oneshot service once per day, which in turn runs
`edges-pipeline.sh` (scans the raw tree for the latest dates, then
calls `run_single_day.py --source=daemon`). Set
`EDGES_DAEMON_ENABLED=0` in `edges-api.service` so the pipeline
doesn't run twice. Recommended for production because:

* No Python process holding memory 24/7.
* `Persistent=true` catches missed runs after power outages.
* Clean separation between the API and the analysis job.
* Each job is a single `oneshot` whose success/failure is visible
  in `systemctl list-jobs` and `journalctl -u edges-pipeline`.

**Install in one block:**

```bash
sudo useradd --system --home /opt/edges-interface --shell /bin/bash edges
sudo mkdir -p /opt/edges-interface && sudo chown edges:edges /opt/edges-interface
sudo -u edges git clone <repo-url> /opt/edges-interface
sudo -u edges conda create -n edges python=3.11 -c conda-forge \
    edges-analysis edges-io pygsdata read-acq astropy
sudo -u edges bash -c 'source activate edges && pip install -r /opt/edges-interface/backend/requirements.txt'
sudo chown -R edges:edges /data5/edges/edges_outputs

sudo cp scripts/systemd/edges-api.service       /etc/systemd/system/
sudo cp scripts/systemd/edges-pipeline.service  /etc/systemd/system/   # Option B only
sudo cp scripts/systemd/edges-pipeline.timer    /etc/systemd/system/   # Option B only
sudo cp scripts/edges-pipeline.sh               /opt/edges-interface/scripts/
sudo chmod +x                                    /opt/edges-interface/scripts/edges-pipeline.sh

# Edit /etc/systemd/system/edges-api.service to set your raw/output paths.
sudo systemctl daemon-reload
sudo systemctl enable --now edges-api.service
sudo systemctl enable --now edges-pipeline.timer      # Option B only
```

**Useful commands:**

```bash
sudo systemctl status edges-api.service
sudo systemctl status edges-pipeline.timer
sudo systemctl list-timers --all | grep edges
sudo journalctl -u edges-api.service -f
sudo journalctl -u edges-pipeline.service -f
sudo systemctl start edges-pipeline.service       # manually trigger today's run
```

Override defaults via `/etc/default/edges-pipeline` (read by the
wrapper script):

```bash
EDGES_RAW_DATA_ROOT=/data5/edges/data/EDGES3_data/MRO
EDGES_OUTPUT_ROOT=/data5/edges/edges_outputs
EDGES_TEMP_LOG_DIR=/data5/edges/data/EDGES3_data/MRO/temperature_logger
EDGES_PYTHON=/opt/anaconda3/envs/edges/bin/python
```

#### Firewall / ports

The backend listens on **TCP 8000** by default. On the ASU enterprise
cluster this typically needs to go through a reverse proxy; talk to
your sysadmin if `curl http://<host>:8000/latest_run` works from your
laptop. The frontend has **no** listening port of its own — it's a
static bundle.

---

## SSH checklist — what you actually have to change

If the project is working on your local machine, getting it onto SSH
should be **path + host changes only** — no code edits. Here's the
checklist:

| Local default | SSH replacement | Where to change |
|---|---|---|
| `EDGES_RAW_DATA_ROOT=/Users/joshualewis/EdgesTestData/data5/edges/data/EDGES3_data/MRO` | `/data5/edges/data/EDGES3_data/MRO` (or wherever the cluster MRO data lives) | env var, or edit `_default_raw_root()` in `backend/config.py` |
| `EDGES_OUTPUT_ROOT=/Users/joshualewis/EdgesTestData/outputs` | `/data5/edges/edges_outputs` (or any writable scratch path on the cluster) | env var, or `_default_output_root()` |
| `EDGES_TEMP_LOG_DIR=...temperature_logger` | same path, just relocated | env var (defaults from `RAW_DATA_ROOT`) |
| uvicorn `--host 127.0.0.1` | `--host 0.0.0.0` so external hosts can reach it | uvicorn command line |
| Frontend `VITE_API_URL` unset (defaults to `http://127.0.0.1:8000`) | `VITE_API_URL=https://edges.example.com` (or whatever the backend's public URL is) | env var at `npm run build` time |
| conda env auto-detected as `/opt/anaconda3/envs/edges` | whatever the SSH path is (e.g. `$HOME/anaconda3/envs/edges/bin/python`) | usually picked up automatically; set `EDGES_PYTHON` if not |
| None | systemd unit / cron job for the daemon (recommended) | new file on SSH |

Everything else — CORS, all imports, file I/O, the React build — is
identical between local and SSH.

---

## Raw data lives outside this repo

**This repo contains code only.** The raw MRO data (`.acq` files,
temperature logs) is large, environment-specific, and never committed.

On the macOS dev box the raw tree happens to sit one directory above
the repo at `/Users/joshualewis/EdgesTestData/data5/edges/data/EDGES3_data/MRO`
(it is **NOT** inside `EdgesTestData/`). On the ASU cluster it sits
on the shared filesystem at `/data5/edges/data/EDGES3_data/MRO`.
Either way, the backend reaches it via a single env var:

```bash
export EDGES_RAW_DATA_ROOT=/the/place/where/the/raw/data/lives
```

`backend/config.py::_default_raw_root()` already tries these common
locations in order:

1. `/Users/joshualewis/EdgesTestData/data5/edges/data/EDGES3_data/MRO` (mac dev)
2. `/data5/edges/data/EDGES3_data/MRO` (ASU cluster)
3. `/mnt/data5/edges/data/EDGES3_data/MRO`, `/scratch/edges/...`, `~/data5/...` (other)

If none of those exist, set `EDGES_RAW_DATA_ROOT` explicitly and the
pipeline will pick it up. `.gitignore` defensively excludes `/data5/`,
`/data/`, `/raw/`, `/mro/`, `/acq/`, and `*.acq` so the raw tree
can't be accidentally committed.

The pipeline never writes into `RAW_DATA_ROOT`; it is read-only
input. All generated artefacts (`manifest.json`, plot `.npz` / `.jpg`
files, daemon trees, saved zips, run history) go to `OUTPUT_ROOT`
which is separate and gitignored.

---

## Will it work on SSH with only path changes?

**Yes, with the following caveats.** I audited the codebase specifically
for local-only assumptions:

| Concern | Status |
|---|---|
| Hardcoded `/Users/joshualewis/...` paths | Only inside `_default_raw_root()` / `_default_output_root()` in `backend/config.py`, **and each has a cluster fallback** (`/data5/...`). Set `EDGES_RAW_DATA_ROOT` / `EDGES_OUTPUT_ROOT` explicitly to be safe. |
| macOS-only behaviour | None. The code uses `pathlib`, no `os.system` with platform branches. |
| `127.0.0.1` only listens on loopback | Change to `--host 0.0.0.0` (one CLI flag). |
| HTTP-only frontend default | Browser will block mixed content; build with `VITE_API_URL=https://...` and put TLS in front. |
| In-process daemon scheduler dies when uvicorn restarts | Use systemd (`Restart=on-failure`) or cron instead. |
| CORS restricted to localhost | Already permissive (`allow_origins=["*"]`). |
| Filesystem permissions on `OUTPUT_ROOT` | Make sure the user running uvicorn can write to it. |
| Frontend reads files directly | No — it goes through the backend's static-file mount at `/`. Works across machines. |
| Time zone | The pipeline uses UTC throughout (ISO 8601 strings, day-of-year dates). The host's local TZ doesn't affect output. |

If you find anything that doesn't work after pointing it at the cluster
paths, it's a missing env var or a permission issue, not a code change.

---

## What gets written under `OUTPUT_ROOT`

This is **runtime state** and is excluded from git (see `.gitignore`).
If you blow it away, the next daemon run will rebuild it from scratch.

```
$EDGES_OUTPUT_ROOT/
├── manifest.json             # Latest manifest (mirrors daemon/manifest.json or user/manifest.json)
├── latest_run.json           # {source, run_id, dates, generated_at, parameters, has_2d, ...}
├── available_dates.json      # Scanned by scan_dates.py on backend startup
├── daemon/                   # Daily scheduled runs
│   ├── manifest.json
│   └── runs/<run_id>/        # One folder per daemon-triggered run
│       ├── calibration/
│       ├── calibration_s11/
│       ├── calibration_spectra/
│       ├── calibration_coefficients/
│       ├── calibration_temperatures/   # noise-wave fit values per load
│       ├── calibrated_temperature/
│       ├── average_temperature/
│       ├── antenna_s11/
│       ├── raw_spectra/  raw_waterfalls/
│       └── actual_temperature/          # probe readings at each cal time
├── user/                     # Latest manual user run
│   ├── manifest.json
│   └── runs/<run_id>/        # (same layout as daemon/runs/<run_id>)
├── user_cache/<hash>/        # Snapshot of the last few user runs, keyed by parameter hash
├── saved/                    # ZIP archives produced by the Save button in the UI
└── run_history/<hash>.json   # Marker that the latest human run uses run_hash X
```

---

## Development workflow

### Running a pipeline manually

```bash
conda activate edges
cd backend
EDGES_RAW_DATA_ROOT=/path/to/mro \
EDGES_OUTPUT_ROOT=/path/to/outputs \
python run_single_day.py \
    --cal-date 2026_227 --s11-date 2026_242_23 --spec-date 2026_244_22_24_54 \
    --source daemon
```

Run with `--help` to see every tunable (cterms, wterms, fstart/fstop,
wfstart/wfstop, save_2d_npz, --run-hash, …).

### Triggering the pipeline from the UI

* Daily at `EDGES_DAEMON_HOUR` when the daemon is enabled.
* On demand via the "Run now" button on the home page (`POST /daemon/trigger`).

### Dedup behaviour

Both manual and daemon runs are deduped by a hash of
`(cal-date, s11-date, spec-date, save_2d_npz, …)`. Re-running with the
same parameters reuses the previous run's `runs/<run_id>/` directory and
just bumps `latest_run.json`. The hash is also recorded in
`run_history/<hash>.json` so the UI can re-link the latest human run
back to its manifest after a refresh.

---

## Deployment notes

* **Conda env**: `edges` (Python 3.11) with `edges-analysis`, `edges-io`,
  `pygsdata`, `read-acq`, `astropy`. Create with:
  `conda create -n edges python=3.11 -c conda-forge edges-analysis edges-io pygsdata read-acq astropy`.
* **Pip deps**: see `backend/requirements.txt`.
* **Static mount**: the backend mounts `$EDGES_OUTPUT_ROOT` at `/` via
  FastAPI's `StaticFiles`. This MUST be the last route registered so it
  doesn't shadow the API endpoints (see the very end of
  `backend_api.py`).
* **Port**: backend defaults to `8000`. Frontend dev server defaults to
  `5173`.

---

## License

See `LICENSE`.
