# Frontend (`frontend/`)

React + TypeScript + Vite single-page app for browsing the EDGES-3
calibration/temperature outputs. Talks to the backend over HTTP only —
it never reads files from disk directly.

## Files

| Path | Role |
|---|---|
| `src/utils/baseURL.ts` | The **only** configurable connection point. Reads `VITE_API_URL` at build time; defaults to `http://127.0.0.1:8000`. |
| `src/state/RunContext.tsx` | Frontend cache of `/latest_run`. Polls after a new run is triggered. |
| `src/components/LatestRunBanner.tsx` | Top-of-page banner: source, dates, per-load probe readings, calibration parameters, save controls. |
| `src/components/MultiPlotter.tsx` | Two-line overlay plot used for the "Calibration vs Actual Temp" comparisons. |
| `src/components/SinglePlotter.tsx` | One-line plot used for spectra and TNW coefficients. |
| `src/components/S11Plotter.tsx` | S11 plot with Re/Im ↔ Mag/Phase toggle. |
| `src/components/HeatmapPlotter.tsx` | Waterfall / heatmap plot with down-sampling. |
| `src/types/manifest.ts` | Types matching the manifest the backend emits. |

## Running locally

```bash
npm install
npm run dev          # http://localhost:5173
```

The dev server proxies nothing — it serves the React app and the app
fetches the API from `VITE_API_URL` (default `http://127.0.0.1:8000`).

## Building for production

When the backend lives on a different host (e.g. SSH cluster), set
`VITE_API_URL` at build time:

```bash
VITE_API_URL=https://edges.example.com npm run build
```

The resulting `dist/` is a fully static bundle. Upload it to any static
host (nginx, GitHub Pages, S3, …). No backend filesystem access
required.

## Path configuration cheat sheet

There is only one frontend-side path: `VITE_API_URL`.

| Build env var | Default | When to override |
|---|---|---|
| `VITE_API_URL` | `http://127.0.0.1:8000` | Any time the backend isn't on localhost. Set this before `npm run build`. |

Everything else (manifest, npz files, jpegs, the latest-run JSON) is
fetched relative to that base URL, so it follows automatically.
