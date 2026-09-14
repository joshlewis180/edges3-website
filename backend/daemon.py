#!/usr/bin/env python3
"""
EDGES-3 Background Daemon
==========================

Runs ``scan_dates`` once a day, picks the latest compatible dates, and invokes
``run_single_day`` to produce the default set of plots. No 2D heatmap data is
saved — only the JPEGs.

Two modes of operation:
  * ``--run-now``        Run once and exit (for systemd timers / cron).
  * (no flag, default)   Start the in-process scheduler that fires every day at
                         ``EDGES_DAEMON_HOUR`` (default 02:00).

Outputs are written under ``OUTPUT_ROOT/daemon/runs/<run_id>/...``.
If no user run exists yet, the daemon manifest is copied to the top-level
``OUTPUT_ROOT/manifest.json`` so it is the default view.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import scan_dates  # noqa: E402
from io_utils import compute_run_hash, write_manifest  # noqa: E402


log = logging.getLogger("edges.daemon")


# ---------------------------------------------------------------------------
# Core "run once" logic — used by both scheduler and --run-now
# ---------------------------------------------------------------------------
def run_once(now: Optional[datetime] = None) -> Optional[Path]:
    """Pick the latest dates and run the pipeline. Returns the manifest path."""
    config.ensure_dirs()
    now = now or datetime.now()

    log.info("Scanning for available dates under %s", config.RAW_DATA_ROOT)
    dates = scan_dates.scan_all(config.RAW_DATA_ROOT)
    scan_dates.write_results(dates, config.AVAILABLE_DATES_FILE)
    log.info(
        "Scan results: %d cal, %d s11, %d raw",
        len(dates["calibration"]), len(dates["s11"]), len(dates["raw"]),
    )

    cal = dates["calibration"][-1] if dates["calibration"] else None
    s11 = dates["s11"][-1] if dates["s11"] else None
    raw = dates["raw"][-1] if dates["raw"] else None
    if not (cal and s11 and raw):
        log.error("Cannot run daemon: missing at least one of cal/s11/raw dates")
        return None

    run_id = f"daemon_{now.strftime('%Y%m%d_%H%M%S')}"
    run_dir = config.DAEMON_DIR / "runs" / run_id
    log.info("Daemon run %s using cal=%s s11=%s raw=%s", run_id, cal, s11, raw)

    # Compute the dedup hash so the backend can match a user click against
    # this run later. Daemon always uses defaults and save_2d_npz=False.
    daemon_defaults = {
        "cterms": 6, "wterms": 5,
        "tcold": 306.5, "thot": 393.22, "tcab": 306.5,
        "tload": 300.0, "tns": 1000.0,
        "fstart": 50.0, "fstop": 190.0,
        "wfstart": 50.0, "wfstop": 190.0,
        "save_2d_npz": False,
    }
    run_hash = compute_run_hash({"cal": cal, "s11": s11, "raw": raw}, daemon_defaults)

    import subprocess
    cmd = [
        config.PYTHON, str(config.RUN_SCRIPT),
        "--cal_date", cal,
        "--s11_date", s11,
        "--spec_date", raw,
        "--rawdata_root", str(config.RAW_DATA_ROOT),
        "--output_root", str(config.DAEMON_DIR),
        "--run_dir", str(run_dir),
        "--temperature_log", str(config.TEMPERATURE_LOG_FILE),
        "--source", "daemon",
        "--run_hash", run_hash,
    ]
    log.info("Executing: %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        log.error("Daemon run failed: %s", res.stderr[-4000:])
        return None
    log.info("Daemon run succeeded: %s", run_dir)

    # Update latest_run.json to point at the daemon run
    _write_latest(source="daemon", run_id=run_id, dates={
        "cal": cal, "s11": s11, "raw": raw,
    })

    # If there is no user run yet, surface the daemon manifest at the top level
    user_manifest = config.USER_DIR / "manifest.json"
    top_manifest = config.OUTPUT_ROOT / "manifest.json"
    if not user_manifest.exists():
        shutil.copy(config.DAEMON_DIR / "manifest.json", top_manifest)
        log.info("Copied daemon manifest to %s (no user run yet)", top_manifest)

    return config.DAEMON_DIR / "manifest.json"


def _write_latest(source: str, run_id: str, dates: Dict[str, str]) -> None:
    """Write ``latest_run.json`` enriched with per-load actual temperatures
    and calibration parameters extracted from the per-source manifest.

    The frontend's top banner reads this file on every page-load, so it
    needs to include everything the banner displays. Delegates to
    ``backend_api._write_latest`` (which is the canonical implementation
    shared with the /daemon/trigger and /run API endpoints) so the
    daemon-mode and user-mode output stays in sync.
    """
    try:
        from backend_api import _write_latest as api_write_latest
    except ImportError:
        # Fall back to the legacy minimal payload if backend_api can't be
        # imported (e.g. daemon running standalone in an older deploy).
        payload = {
            "source": source,
            "run_id": run_id,
            "dates": dates,
            "generated_at": datetime.now().isoformat(),
        }
        config.LATEST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(config.LATEST_RUN_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        return
    api_write_latest(source=source, run_id=run_id, dates=dates)


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------
_scheduler_started = False
_scheduler_lock = threading.Lock()


def _seconds_until_next_hour(hour: int, now: Optional[datetime] = None) -> float:
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _scheduler_loop(stop_event: threading.Event) -> None:
    log.info("Daemon scheduler loop started, target hour=%d", config.DAEMON_HOUR)
    while not stop_event.is_set():
        wait_s = _seconds_until_next_hour(config.DAEMON_HOUR)
        log.info("Next daemon run in %.0f s (at hour %d)", wait_s, config.DAEMON_HOUR)
        # Sleep in small increments so SIGTERM / Ctrl-C is responsive.
        slept = 0.0
        while slept < wait_s and not stop_event.is_set():
            time.sleep(min(5.0, wait_s - slept))
            slept += 5.0
        if stop_event.is_set():
            break
        try:
            run_once()
        except Exception:
            log.exception("Daemon run_once failed")


def start_scheduler_in_background() -> Optional[threading.Thread]:
    """Start the daemon scheduler thread. Returns the thread (or None)."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return None
        if not config.DAEMON_ENABLED:
            log.info("Scheduler not started: EDGES_DAEMON_ENABLED is false")
            return None
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_scheduler_loop, args=(stop_event,), daemon=True, name="edges-daemon",
        )
        thread.start()
        _scheduler_started = True
        log.info("Background daemon scheduler started (thread=%s)", thread.name)
        return thread


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="EDGES-3 background daemon")
    parser.add_argument("--run-now", action="store_true",
                        help="Run once immediately and exit")
    parser.add_argument("--serve", action="store_true",
                        help="Start the in-process scheduler and stay alive")
    parser.add_argument("--hour", type=int, default=config.DAEMON_HOUR,
                        help="Override EDGES_DAEMON_HOUR for this process")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config.DAEMON_HOUR = args.hour
    config.ensure_dirs()

    if args.run_now:
        result = run_once()
        return 0 if result else 1

    if args.serve:
        # First do an immediate run so the website has data right away.
        run_once()
        start_scheduler_in_background()
        log.info("Daemon server running. Press Ctrl-C to exit.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            log.info("Shutting down")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
