"""
EDGES-3 Web API
===============

Endpoints
---------
GET  /available_dates        List of dates per category (cached on disk)
GET  /manifest.json          The manifest for the currently displayed run
GET  /latest_run             JSON pointer to the currently displayed run
GET  /daemon/status          Whether the in-process scheduler is enabled / active
POST /daemon/trigger         Run the daemon pipeline once (admin)
POST /run_pipeline           Trigger a user-run with custom dates / parameters
POST /save_outputs           Bundle the current outputs into a downloadable zip
GET  /download/<name>        Download a previously saved zip
GET  /health                 Liveness probe

Static files
------------
The output root (and its ``daemon/`` and ``user/`` subdirs) are mounted under
``/`` so that ``/manifest.json``, ``/daemon/runs/<id>/...`` etc. are directly
fetchable by the browser.

Concurrency
-----------
A single ``RunLock`` serialises pipeline runs so that two simultaneous user
clicks (or a user click during a daemon tick) cannot clobber each other.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import daemon as daemon_mod  # noqa: E402
import scan_dates  # noqa: E402
from io_utils import compute_run_hash  # noqa: E402


log = logging.getLogger("edges.api")

# Ensure output directories exist before StaticFiles mounts at import time.
config.ensure_dirs()


# ---------------------------------------------------------------------------
# Pipeline parameter schema (mirrors Select.tsx)
# ---------------------------------------------------------------------------
PIPELINE_DEFAULTS: Dict[str, Any] = {
    "cterms": 6,
    "wterms": 5,
    "tcold": 306.5,
    "thot": 393.22,
    "tcab": 306.5,
    "tload": 300.0,
    "tns": 1000.0,
    "fstart": 50.0,
    "fstop": 190.0,
    "wfstart": 50.0,
    "wfstop": 190.0,
    "save_2d_npz": False,
}

NUMERIC_PIPELINE_KEYS = (
    "cterms", "wterms", "tcold", "thot", "tcab",
    "tload", "tns", "fstart", "fstop", "wfstart", "wfstop",
)


class RunRequest(BaseModel):
    dates: Dict[str, str] = Field(
        default_factory=lambda: {"cal": "Latest", "s11": "Latest", "raw": "Latest"}
    )
    parameters: Dict[str, Any] = Field(default_factory=dict)


class SaveRequest(BaseModel):
    include_2d: bool = False
    label: Optional[str] = None


# ---------------------------------------------------------------------------
# Single-process run lock
# ---------------------------------------------------------------------------
class RunLock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: Optional[str] = None

    def acquire(self, holder: str, timeout: float = 0.0) -> bool:
        if timeout <= 0:
            got = self._lock.acquire(blocking=False)
        else:
            got = self._lock.acquire(timeout=timeout)
        if got:
            self._holder = holder
        return got

    def release(self, holder: str) -> None:
        if self._holder == holder:
            self._holder = None
            self._lock.release()

    def status(self) -> Dict[str, Any]:
        return {
            "busy": self._holder is not None,
            "holder": self._holder,
        }


run_lock = RunLock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def _resolve_dates(dates_in: Dict[str, str], available: Dict[str, List[str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, list_key in (("cal", "calibration"), ("s11", "s11"), ("raw", "raw")):
        v = dates_in.get(key, "Latest") or "Latest"
        if v == "Latest":
            choices = available.get(list_key, [])
            if not choices:
                raise HTTPException(status_code=400, detail=f"No {list_key} dates available")
            out[key] = choices[-1]
        else:
            if v not in available.get(list_key, []):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown {list_key} date: {v!r}",
                )
            out[key] = v
    return out


def _ensure_dates_scanned(force: bool = False) -> Dict[str, List[str]]:
    """Refresh the on-disk dates cache if it's missing or older than 1 hour."""
    cache = config.AVAILABLE_DATES_FILE
    stale = True
    if cache.exists() and not force:
        age = time.time() - cache.stat().st_mtime
        stale = age > 3600  # 1 hour
    if stale:
        dates = scan_dates.scan_all(config.RAW_DATA_ROOT)
        scan_dates.write_results(dates, cache)
    payload = _read_json(cache, {})
    return {
        "calibration": payload.get("calibration", []),
        "s11": payload.get("s11", []),
        "raw": payload.get("raw", []),
    }


def _wipe_user_outputs() -> int:
    """Delete everything under ``OUTPUT_ROOT/user`` except saved zips."""
    removed = 0
    if not config.USER_DIR.exists():
        return 0
    for entry in config.USER_DIR.iterdir():
        if entry.name == "saved":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed += 1
    return removed


# A small cache that holds the previous human-done run so a follow-up click
# with the same parameters can be deduped against it. Only the most recent
# user run for any given hash is preserved here. (Directory location lives in
# config.USER_CACHE_DIR.)
USER_CACHE_DIR = config.USER_CACHE_DIR


def _stash_previous_user_run(current_hash: str) -> Optional[str]:
    """Copy the current ``user/runs/<run_id>/`` directory into
    ``user_cache/<previous_hash>/`` so a later identical click can dedup
    against it.

    Returns the previous hash (or None if there was nothing to stash).

    The cache is intentionally small (a few most-recent hashes); older
    entries are evicted. That keeps the on-disk footprint bounded while
    still letting the common "click the same thing twice" pattern dedup.
    """
    runs_root = config.USER_DIR / "runs"
    if not runs_root.exists():
        return None
    # Pick the most recently modified run directory as "the current user run".
    candidates = sorted(
        (p for p in runs_root.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    previous = candidates[0]

    # Determine the hash of this previous run from its run_history marker.
    prev_hash: Optional[str] = None
    for marker in config.RUN_HISTORY_DIR.glob("*.json"):
        try:
            with open(marker, "r") as f:
                meta = json.load(f)
        except Exception:
            continue
        if meta.get("run_id") == previous.name and meta.get("source") == "user":
            prev_hash = marker.stem
            break

    if prev_hash is None:
        return None

    USER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target_dir = USER_CACHE_DIR / prev_hash
    # If we already have a fresh cache entry for this hash, leave it alone.
    if not target_dir.exists():
        shutil.copytree(previous, target_dir)
        # Also copy the per-source manifest so the cache entry is
        # self-contained. Without this, ``_find_existing_run`` cannot tell
        # whether the cached run is still valid after the next wipe
        # (which removes ``OUTPUT_ROOT/user/manifest.json``).
        src_manifest = config.USER_DIR / "manifest.json"
        if src_manifest.exists():
            shutil.copy(src_manifest, target_dir / "manifest.json")
        with open(target_dir / ".cache_meta.json", "w") as f:
            json.dump({
                "hash": prev_hash,
                "original_run_id": previous.name,
                "stashed_at": datetime.now().isoformat(),
            }, f, indent=2)

    # Evict the oldest cache entries until we are within the cap.
    _evict_old_cache_entries(_USER_CACHE_CAP)
    return prev_hash


# Cap on the number of user_cache entries kept on disk.
_USER_CACHE_CAP = 5


def _evict_old_cache_entries(cap: int) -> None:
    """Keep at most ``cap`` entries in ``user_cache``, evicting oldest first."""
    if not USER_CACHE_DIR.exists():
        return
    entries = [p for p in USER_CACHE_DIR.iterdir() if p.is_dir()]
    if len(entries) <= cap:
        return
    entries.sort(key=lambda p: p.stat().st_mtime)
    for stale in entries[: len(entries) - cap]:
        shutil.rmtree(stale, ignore_errors=True)


def _find_existing_run(
    run_hash: str,
    want_2d: bool,
) -> Optional[Tuple[str, Path, Dict[str, Any]]]:
    """Look for a previous run with matching hash that can be reused.

    Returns ``(source, source_path, meta)`` or None.
    Matching rules:
      * Daemon runs never include 2D data, so they can only be reused when
        ``want_2d`` is False.
      * User runs (either from the live ``user/`` tree or the
        ``user_cache/`` shadow) match whenever their recorded ``save_2d_npz``
        equals the request.
      * ``run_history`` markers whose ``source_path`` no longer exist are
        ignored.
    """
    marker = config.RUN_HISTORY_DIR / f"{run_hash}.json"
    if not marker.exists():
        return None
    try:
        with open(marker, "r") as f:
            meta = json.load(f)
    except Exception:
        return None

    source = meta.get("source")
    recorded_2d = bool(meta.get("params", {}).get("save_2d_npz"))
    if want_2d != recorded_2d:
        return None  # e.g. user wants 2D but only a daemon (no 2D) run exists
    if source not in ("daemon", "user"):
        return None

    # Priority: user cache > recorded source path. The user cache is the
    # most recent user run with this hash; if it exists, prefer it so we
    # stay aligned with what the user saw last.
    candidates: List[Tuple[str, Path]] = []
    cached = USER_CACHE_DIR / run_hash
    if cached.exists() and (cached / "manifest.json").exists():
        candidates.append(("user", cached))
    src_path = Path(meta.get("source_path", ""))
    if src_path.exists():
        candidates.append((source, src_path))

    for cand_source, cand in candidates:
        # The manifest lives at the per-source root (``OUTPUT_ROOT/<source>/manifest.json``),
        # two levels above ``runs/<id>/``. Accept either location so the dedup check works
        # regardless of which level the caller recorded.
        # For the user_cache shadow the per-source manifest is at
        # ``OUTPUT_ROOT/user/manifest.json`` (the user's most recent run).
        manifest_in_run = cand / "manifest.json"
        if cand_source == "user" and cand.parent.name == "user_cache":
            # ``cand`` is e.g. ``OUTPUT_ROOT/user_cache/<hash>/``; the matching
            # per-source manifest is at ``OUTPUT_ROOT/user/manifest.json``.
            manifest_in_source = cand.parent.parent / "user" / "manifest.json"
        else:
            manifest_in_source = cand.parent.parent / "manifest.json"
        if manifest_in_run.exists() or manifest_in_source.exists():
            return cand_source, cand, meta
    return None


def _write_latest(source: str, run_id: str, dates: Dict[str, str]) -> None:
    # Extract per-load actual temperatures and calibration parameters from
    # the per-source manifest so the top banner can show "ambient @ time: X
    # K" and the actual tcold/thot/tcab values used by the analysis, all
    # without each plot needing to repeat the information in its title.
    actual_temps: Dict[str, Dict[str, Any]] = {}
    parameters: Dict[str, Any] = {}
    source_root = config.USER_DIR if source == "user" else config.DAEMON_DIR
    src_manifest = source_root / "manifest.json"
    if src_manifest.exists():
        try:
            with open(src_manifest, "r") as f:
                m = json.load(f)
            for plot in m.get("plots", []):
                if plot.get("type") == "multi" and plot.get("id", "").endswith("_vs_actual"):
                    load = plot["id"].removesuffix("_vs_actual")
                    actual_temps[load] = {
                        "time": plot.get("time"),
                        "temperature_k": plot.get("temperature_k"),
                    }
            raw_params = m.get("parameters", {})
            if isinstance(raw_params, dict):
                # Reformat for the UI: flatten the value_k field and keep
                # the probe provenance next to it.
                for k, v in raw_params.items():
                    if not isinstance(v, dict):
                        continue
                    parameters[k] = {
                        "value_k": v.get("value_k"),
                        "source": v.get("source"),
                        "probe": v.get("probe"),
                        "time": v.get("time"),
                    }
        except Exception:
            pass
    payload: Dict[str, Any] = {
        "source": source,
        "run_id": run_id,
        "dates": dates,
        "generated_at": datetime.now().isoformat(),
    }
    if actual_temps:
        payload["actual_temperatures"] = actual_temps
    if parameters:
        payload["parameters"] = parameters
    # Flag whether any heatmap (``_2d.npz``) files exist in the current
    # source tree. The frontend uses this to disable the "Include 2D
    # heatmaps" checkbox on the save form when there's nothing to include.
    payload["has_2d"] = any(source_root.rglob("*_2d.npz"))
    _write_json(config.LATEST_RUN_FILE, payload)


def _read_actual_temperatures() -> Dict[str, Dict[str, Any]]:
    """Read the per-load actual temperatures from the latest_run file."""
    data = _read_json(config.LATEST_RUN_FILE, {})
    temps = data.get("actual_temperatures", {})
    return temps if isinstance(temps, dict) else {}


def _promote_to_top_level(source_root: Path) -> Optional[Path]:
    """Copy the latest manifest from source_root to OUTPUT_ROOT/manifest.json.

    The per-source manifest stores ``filePath`` values relative to its own
    root (e.g. ``runs/<id>/foo.npz`` for ``OUTPUT_ROOT/user/manifest.json``).
    Once the manifest lives at ``OUTPUT_ROOT/manifest.json`` the paths must
    be prefixed with the source name (``user/runs/<id>/foo.npz``) so the
    static mount can serve them. This rewrite happens here.
    """
    src = source_root / "manifest.json"
    if not src.exists():
        return None
    dst = config.OUTPUT_ROOT / "manifest.json"
    try:
        with open(src, "r") as f:
            manifest = json.load(f)
    except Exception:
        shutil.copy(src, dst)
        return dst

    source_name = source_root.name  # "daemon" or "user"
    rewritten: List[Dict[str, Any]] = []
    for plot in manifest.get("plots", []):
        plot = dict(plot)
        for key in ("filePath", "filePath1", "filePath2"):
            v = plot.get(key)
            if isinstance(v, str):
                if v.startswith("runs/"):
                    plot[key] = f"{source_name}/{v}"
                elif v.startswith(f"{source_name}/runs/"):
                    # Already prefixed; leave it.
                    pass
        rewritten.append(plot)
    manifest["plots"] = rewritten

    with open(dst, "w") as f:
        json.dump(manifest, f, indent=2)
    return dst


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------
def _execute_subprocess(cmd: List[str]) -> None:
    log.info("Running pipeline: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("Pipeline failed: %s", result.stderr[-4000:])
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline failed (exit {result.returncode}): {result.stderr[-2000:]}",
        )


def _build_pipeline_cmd(
    resolved: Dict[str, str],
    merged: Dict[str, Any],
    output_root: Path,
    run_dir: Path,
    source: str,
    run_hash: str,
) -> List[str]:
    cmd = [
        config.PYTHON, str(config.RUN_SCRIPT),
        "--cal_date", resolved["cal"],
        "--s11_date", resolved["s11"],
        "--spec_date", resolved["raw"],
        "--rawdata_root", str(config.RAW_DATA_ROOT),
        "--output_root", str(output_root),
        "--run_dir", str(run_dir),
        "--temperature_log", str(config.TEMPERATURE_LOG_FILE),
        "--source", source,
        "--run_hash", run_hash,
    ]
    for k in NUMERIC_PIPELINE_KEYS:
        if k in merged:
            cmd.extend([f"--{k}", str(merged[k])])
    if merged.get("save_2d_npz"):
        cmd.append("--save_2d_npz")
    return cmd


def _copy_run_to(src: Path, dst: Path) -> None:
    """Copy a previously-produced run directory to a new location."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _build_run_id(source: str, run_hash: str) -> str:
    return f"{source}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{run_hash}"


def run_pipeline(
    dates: Dict[str, str],
    parameters: Dict[str, Any],
    source: str,
    output_root: Path,
    wipe_target_first: bool = False,
) -> Dict[str, Any]:
    """Run the pipeline for ``source`` ('user' or 'daemon') with dedup.

    Dedup rules:
      * Hash from ``(dates, parameters)`` is the dedup key.
      * Daemon runs only match user requests when ``save_2d_npz`` is False.
      * A previous user run is preserved in ``OUTPUT_ROOT/user_cache/`` so it
        can be reused by a later identical click.
      * Reusing copies the previous outputs into ``output_root/runs/<id>/``
        under a new timestamped run id, then rewrites the manifest pointing
        at that run.
    """
    available = _ensure_dates_scanned()
    resolved = _resolve_dates(dates, available)
    merged = {**PIPELINE_DEFAULTS, **parameters}

    # Hash includes dates and every parameter (including save_2d_npz) so a
    # user click for 2D data never matches a daemon (2D-free) run.
    run_hash = compute_run_hash(resolved, merged)
    want_2d = bool(merged.get("save_2d_npz"))

    output_root.mkdir(parents=True, exist_ok=True)

    # ---- Dedup ----------------------------------------------------------
    if source == "user":
        # Stash the previous user run before wiping, so we can reuse it later
        # if the user clicks again with the same parameters.
        _stash_previous_user_run(current_hash=run_hash)
        _wipe_user_outputs()

    existing = _find_existing_run(run_hash, want_2d) if source == "user" else None

    run_id = _build_run_id(source, run_hash)
    run_dir = output_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    reused_from: Optional[str] = None
    if existing is not None:
        existing_source, existing_path, _meta = existing
        log.info(
            "Reusing existing %s run for hash=%s from %s",
            existing_source, run_hash, existing_path,
        )
        _copy_run_to(existing_path, run_dir)
        # Rewrite the manifest so the new run_id appears in it.
        # The manifest lives at the per-source root (one or two levels
        # above ``existing_path``); never inside the run directory. Try
        # both candidate locations so a stale cached copy is also found.
        old_manifest: Dict[str, Any] = {}
        for candidate in (
            existing_path / "manifest.json",
            existing_path.parent / "manifest.json",
            existing_path.parent.parent / "manifest.json",
        ):
            try:
                with open(candidate, "r") as f:
                    old_manifest = json.load(f)
                if old_manifest.get("plots"):
                    break
            except Exception:
                continue
        # Re-emit manifest by re-running the manifest writer. We need to
        # rebuild the plots list, but since the source dir is canonical we
        # can just copy the old manifest with the new run_id baked in.
        new_manifest = dict(old_manifest)
        new_manifest["latest_run"] = run_dir.name
        new_manifest["source"] = source
        new_manifest["dates"] = old_manifest.get("dates", resolved)
        new_manifest["generated_at"] = datetime.now().isoformat()
        new_manifest["reused_from"] = {
            "source": existing_source,
            "path": str(existing_path),
        }
        # Rewrite plot filePaths so they point into the new run dir.
        new_plot_prefix = f"runs/{run_dir.name}"
        old_prefix = f"runs/{existing_path.name}"
        for plot in new_manifest.get("plots", []):
            for key in ("filePath", "filePath1", "filePath2"):
                v = plot.get(key)
                if isinstance(v, str) and v.startswith(old_prefix):
                    plot[key] = new_plot_prefix + v[len(old_prefix):]
        out_manifest = output_root / "manifest.json"
        with open(out_manifest, "w") as f:
            json.dump(new_manifest, f, indent=2)
        log.info("Reused manifest written to %s", out_manifest)

        # Re-point the run_history marker at the new user-owned copy. Without
        # this, every subsequent click would keep matching the original
        # daemon/user source instead of this user's latest version. The
        # previous user run is then stashed into user_cache before the next
        # click wipes it.
        if source == "user":
            marker_path = config.RUN_HISTORY_DIR / f"{run_hash}.json"
            marker_meta: Dict[str, Any] = dict(_meta or {})
            marker_meta["source"] = "user"
            marker_meta["run_id"] = run_id
            marker_meta["source_path"] = str(run_dir)
            marker_meta["dates"] = resolved
            marker_meta["params"] = merged
            marker_meta["saved_at"] = datetime.now().isoformat()
            marker_meta.pop("hash", None)
            marker_meta["hash"] = run_hash
            with open(marker_path, "w") as f:
                json.dump(marker_meta, f, indent=2)
            log.info("Re-pointed marker %s -> user run %s", run_hash, run_id)

        reused_from = existing_source
    else:
        # ---- Fresh pipeline run -----------------------------------------
        cmd = _build_pipeline_cmd(resolved, merged, output_root, run_dir, source, run_hash)
        _execute_subprocess(cmd)

    _write_latest(source, run_id, resolved)

    if source == "user":
        _promote_to_top_level(output_root)
    elif source == "daemon":
        if not (config.USER_DIR / "manifest.json").exists():
            _promote_to_top_level(output_root)

    return {
        "success": True,
        "source": source,
        "run_id": run_id,
        "dates": resolved,
        "parameters": merged,
        "manifest": f"{source}/manifest.json",
        "run_hash": run_hash,
        "reused_from": reused_from,
    }


# ---------------------------------------------------------------------------
# Saving outputs as a downloadable zip
# ---------------------------------------------------------------------------
def _zip_directory(root: Path, include_2d: bool) -> io.BytesIO:
    """Create an in-memory zip of ``root`` (recursive). If ``include_2d`` is
    False, files ending in ``_2d.npz`` are skipped."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if not include_2d and path.name.endswith("_2d.npz"):
                continue
            arcname = str(path.relative_to(root))
            zf.write(path, arcname)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="EDGES-3 Web API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    _ensure_dates_scanned()
    if config.DAEMON_ENABLED:
        daemon_mod.start_scheduler_in_background()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": datetime.now().isoformat()}


@app.get("/available_dates")
def available_dates(force: bool = False) -> Dict[str, List[str]]:
    return _ensure_dates_scanned(force=force)


@app.get("/latest_run")
def latest_run() -> Dict[str, Any]:
    return _read_json(config.LATEST_RUN_FILE, {
        "source": None, "run_id": None, "dates": {}, "generated_at": None,
    })


@app.get("/daemon/status")
def daemon_status() -> Dict[str, Any]:
    return {
        "enabled": config.DAEMON_ENABLED,
        "hour": config.DAEMON_HOUR,
        "raw_data_root_exists": config.RAW_DATA_ROOT.exists(),
        "lock": run_lock.status(),
    }


@app.post("/daemon/trigger")
def daemon_trigger() -> Dict[str, Any]:
    if not run_lock.acquire(holder="daemon-trigger"):
        raise HTTPException(status_code=409, detail="Pipeline already running")
    try:
        result = run_pipeline(
            dates={"cal": "Latest", "s11": "Latest", "raw": "Latest"},
            parameters={"save_2d_npz": False},
            source="daemon",
            output_root=config.DAEMON_DIR,
        )
        return result
    finally:
        run_lock.release(holder="daemon-trigger")


@app.post("/run_pipeline")
def run_pipeline_endpoint(req: RunRequest) -> Dict[str, Any]:
    if not run_lock.acquire(holder="user"):
        raise HTTPException(status_code=409, detail="Pipeline already running; try again later")
    try:
        return run_pipeline(
            dates=req.dates,
            parameters=req.parameters,
            source="user",
            output_root=config.USER_DIR,
            wipe_target_first=True,
        )
    finally:
        run_lock.release(holder="user")


@app.post("/save_outputs")
def save_outputs(req: SaveRequest) -> Dict[str, Any]:
    """Zip the currently displayed outputs (prefers the user tree; falls back
    to the daemon tree) and write to ``OUTPUT_ROOT/saved/<label>.zip``."""
    source_root = config.USER_DIR if (config.USER_DIR / "manifest.json").exists() else config.DAEMON_DIR
    label = (req.label or datetime.now().strftime("run_%Y%m%d_%H%M%S")).strip()
    safe_label = re.sub(r"[^A-Za-z0-9._-]", "_", label)
    out_path = config.SAVED_DIR / f"{safe_label}.zip"
    config.SAVED_DIR.mkdir(parents=True, exist_ok=True)

    buf = _zip_directory(source_root, include_2d=req.include_2d)
    with open(out_path, "wb") as f:
        f.write(buf.getvalue())

    latest = _read_json(config.LATEST_RUN_FILE, {})
    return {
        "success": True,
        "label": safe_label,
        "include_2d": req.include_2d,
        "size_bytes": out_path.stat().st_size,
        "download_url": f"/download/{safe_label}.zip",
        "source_root": source_root.name,
        "latest_run": latest,
    }


@app.get("/download/{name}")
def download(name: str) -> FileResponse:
    safe = (config.SAVED_DIR / name).resolve()
    if config.SAVED_DIR.resolve() not in safe.parents and safe != config.SAVED_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid path")
    if not safe.exists() or not safe.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(safe, filename=name)


# ---------------------------------------------------------------------------
# Static file serving — MUST come last so it doesn't shadow the API routes.
# ---------------------------------------------------------------------------
# Serve OUTPUT_ROOT at "/" so /manifest.json is reachable.
app.mount("/", StaticFiles(directory=str(config.OUTPUT_ROOT), html=False), name="outputs")
