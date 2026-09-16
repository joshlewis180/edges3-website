"""
Central configuration for the EDGES-3 web interface.

All paths are environment-driven so that the same code runs:
  * Locally on macOS / Linux
  * On the ASU enterprise cluster

Environment variables (all optional, with sensible defaults for local dev):
  EDGES_RAW_DATA_ROOT       Raw MRO data root (default: local test root)
  EDGES_OUTPUT_ROOT         Where outputs and the manifest live
  EDGES_TEMP_LOG_FILE       Path to a specific temperature.log (legacy)
  EDGES_TEMP_LOG_DIR        Directory containing one or more temperature log files;
                             every ``*.log`` (plus ``*.backup`` and ``*.txt``)
                             in this directory is read. Defaults to the parent
                             directory of ``EDGES_TEMP_LOG_FILE``.
  EDGES_BEAM_FACTOR_FILE    Path to the EDGES-3 antenna beam factor file
                             (``e3_beam_factor.hickle``). Required for the
                             absolute temperature calibration; location
                             differs between local dev and the SSH cluster.
  EDGES_PYTHON              Python interpreter to use (default: current 'python')
  EDGES_DAEMON_HOUR         Hour of day (0-23) to run the daily daemon (default: 2)
  EDGES_DAEMON_ENABLED      "1"/"true" to enable the in-process scheduler
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Raw data
# ---------------------------------------------------------------------------
def _default_raw_root() -> Path:
    """Pick a sensible default for whichever machine we are on.

    The raw data is NEVER part of this repository — it lives somewhere
    else on the filesystem. We try a couple of common locations and
    fall back to the local path even if it doesn't exist, so downstream
    code can raise a clear error.
    """
    # Local mac test setup
    local = Path("/Users/joshualewis/EdgesTestData/data5/edges/data/EDGES3_data/MRO")
    if local.exists():
        return local
    # Enterprise cluster
    cluster = Path("/data5/edges/data/EDGES3_data/MRO")
    if cluster.exists():
        return cluster
    # Linux dev box — anything the user mounts
    for guess in (
        Path("/mnt/data5/edges/data/EDGES3_data/MRO"),
        Path("/scratch/edges/data/EDGES3_data/MRO"),
        Path.home() / "data5/edges/data/EDGES3_data/MRO",
    ):
        if guess.exists():
            return guess
    # Fall back to the local path even if it doesn't exist; let downstream code
    # raise a clear error.
    return local


RAW_DATA_ROOT: Path = Path(
    os.environ.get("EDGES_RAW_DATA_ROOT", str(_default_raw_root()))
).expanduser().resolve()

TEMPERATURE_LOG_FILE: Path = Path(
    os.environ.get(
        "EDGES_TEMP_LOG_FILE",
        str(RAW_DATA_ROOT / "temperature_logger" / "temperature.log"),
    )
).expanduser().resolve()

# Directory containing one or more temperature logs. Every ``*.log`` file in
# this directory is read, so multiple log files (one per session, day, or
# sensor) all contribute to the lookup. Defaults to the parent directory of
# ``TEMPERATURE_LOG_FILE`` so the legacy single-file layout still works.
TEMPERATURE_LOG_DIR: Path = Path(
    os.environ.get(
        "EDGES_TEMP_LOG_DIR",
        str(TEMPERATURE_LOG_FILE.parent),
    )
).expanduser().resolve()


# ---------------------------------------------------------------------------
# Beam factor file
# ---------------------------------------------------------------------------
# The EDGES-3 antenna beam factor lives in a single ``.hickle`` file that
# is read by the absolute calibration. The on-disk location differs
# between local mac dev and the SSH cluster, so it is env-driven. We
# default to a sibling of RAW_DATA_ROOT's parent (the `edges/` directory)
# and fall back to common cluster locations.
def _default_beam_factor_file() -> Path:
    """Pick a sensible default for whichever machine we are on.

    The beam factor lives in the ``edges/`` directory that is the
    great-grandparent of ``temperature.log``
    (``data5/edges/data/EDGES3_data/MRO/temperature_logger/temperature.log``
    → great-grandparent is ``data5/edges/``). Tries, in order:

      1. Great-grandparent of the live ``temperature.log``
      2. The cluster mount point
      3. Linux dev mounts
      4. ``$HOME/edges/...``
    """
    # 1. walk up 4 levels from temperature.log:  log → temperature_logger/ → MRO/ → EDGES3_data/ → data/ → edges/
    sibling = TEMPERATURE_LOG_FILE.parents[4] / "e3_beam_factor.hickle"
    if sibling.exists():
        return sibling
    # 2. enterprise cluster
    cluster = Path("/data5/edges/e3_beam_factor.hickle")
    if cluster.exists():
        return cluster
    # 3. linux dev mounts
    for guess in (
        Path("/mnt/data5/edges/e3_beam_factor.hickle"),
        Path("/scratch/edges/e3_beam_factor.hickle"),
        Path.home() / "edges" / "e3_beam_factor.hickle",
    ):
        if guess.exists():
            return guess
    return sibling  # may not exist; downstream code will report a clear error


BEAM_FACTOR_FILE: Path = Path(
    os.environ.get("EDGES_BEAM_FACTOR_FILE", str(_default_beam_factor_file()))
).expanduser().resolve()


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
def _default_output_root() -> Path:
    """Pick a sensible output root for local vs cluster."""
    if RAW_DATA_ROOT == Path("/data5/edges/data/EDGES3_data/MRO").resolve():
        # Cluster: write outputs to a project directory
        return Path("/data5/edges/edges_outputs")
    return Path("/Users/joshualewis/EdgesTestData/outputs")


OUTPUT_ROOT: Path = Path(
    os.environ.get("EDGES_OUTPUT_ROOT", str(_default_output_root()))
).expanduser().resolve()

# Subdirectories within OUTPUT_ROOT. The daemon owns its own subtree, the user
# owns theirs. The manifest and latest_run pointer live at the top level.
DAEMON_DIR: Path = OUTPUT_ROOT / "daemon"
USER_DIR: Path = OUTPUT_ROOT / "user"
USER_CACHE_DIR: Path = OUTPUT_ROOT / "user_cache"
SAVED_DIR: Path = OUTPUT_ROOT / "saved"
RUN_HISTORY_DIR: Path = OUTPUT_ROOT / "run_history"

MANIFEST_FILE: Path = OUTPUT_ROOT / "manifest.json"
LATEST_RUN_FILE: Path = OUTPUT_ROOT / "latest_run.json"
AVAILABLE_DATES_FILE: Path = OUTPUT_ROOT / "available_dates.json"


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------
SCRIPTS_DIR: Path = Path(__file__).resolve().parent

RUN_SCRIPT: Path = SCRIPTS_DIR / "run_single_day.py"
SCAN_SCRIPT: Path = SCRIPTS_DIR / "scan_dates.py"
DAEMON_SCRIPT: Path = SCRIPTS_DIR / "daemon.py"


# ---------------------------------------------------------------------------
# Python interpreter
# ---------------------------------------------------------------------------
def _detect_python() -> str:
    """Prefer ``EDGES_PYTHON`` if set, then the current interpreter
    (``sys.executable``), then any ``python`` on PATH. The current
    interpreter wins over PATH lookups so that an active conda env is
    preserved across subprocess invocations.
    """
    candidates = [
        os.environ.get("EDGES_PYTHON"),
        sys.executable,
        shutil.which("python"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return sys.executable


PYTHON: str = _detect_python()


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------
DAEMON_HOUR: int = int(os.environ.get("EDGES_DAEMON_HOUR", "2"))
DAEMON_ENABLED: bool = os.environ.get("EDGES_DAEMON_ENABLED", "0").lower() in (
    "1", "true", "yes"
)


# ---------------------------------------------------------------------------
# Calibration temperature probes
# ---------------------------------------------------------------------------
# The pipeline auto-derives every per-load calibration temperature from
# the temperature log. There are no user-tunable setpoints anymore —
# whatever the probe says is what the EDGES receiver calibration sees,
# which in turn is what ``calibrated_temps.txt`` reports back.
#
# Probe numbers are the ``offset_s`` values found in the on-site
# temperature log file (see ``temperature_logger/*.log``). Override with
# EDGES_PROBE_* if your hardware uses a different sensor layout.
PROBE_AMBIENT: float = float(os.environ.get("EDGES_PROBE_AMBIENT", "100"))
PROBE_HOT: float = float(os.environ.get("EDGES_PROBE_HOT", "102"))
PROBE_LNA: float = float(os.environ.get("EDGES_PROBE_LNA", "100"))
PROBE_COLD_LOAD: float = float(os.environ.get("EDGES_PROBE_COLD_LOAD", "152"))

# Fallback values used when no probe reading is found at the calibration
# time. These are NOT exposed as env vars — they're internal constants.
TCOLD_FALLBACK_K = 306.5
THOT_FALLBACK_K = 393.22
TCAB_FALLBACK_K = 306.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ensure_dirs() -> None:
    """Make sure every output subdirectory exists."""
    for d in (OUTPUT_ROOT, DAEMON_DIR, USER_DIR, USER_CACHE_DIR, SAVED_DIR, RUN_HISTORY_DIR):
        d.mkdir(parents=True, exist_ok=True)


def describe() -> str:
    return (
        f"RAW_DATA_ROOT      = {RAW_DATA_ROOT}\n"
        f"TEMPERATURE_LOG_DIR= {TEMPERATURE_LOG_DIR}\n"
        f"TEMPERATURE_LOG    = {TEMPERATURE_LOG_FILE}\n"
        f"BEAM_FACTOR_FILE   = {BEAM_FACTOR_FILE}\n"
        f"OUTPUT_ROOT        = {OUTPUT_ROOT}\n"
        f"DAEMON_DIR         = {DAEMON_DIR}\n"
        f"USER_DIR           = {USER_DIR}\n"
        f"SAVED_DIR          = {SAVED_DIR}\n"
        f"PYTHON             = {PYTHON}\n"
        f"DAEMON_HOUR        = {DAEMON_HOUR}\n"
        f"DAEMON_ENABLED     = {DAEMON_ENABLED}\n"
        f"TCOLD_FALLBACK_K   = {TCOLD_FALLBACK_K} K\n"
        f"THOT_FALLBACK_K    = {THOT_FALLBACK_K} K\n"
        f"TCAB_FALLBACK_K    = {TCAB_FALLBACK_K} K\n"
        f"PROBE_AMBIENT      = {PROBE_AMBIENT}\n"
        f"PROBE_HOT          = {PROBE_HOT}\n"
        f"PROBE_LNA          = {PROBE_LNA}\n"
        f"PROBE_COLD_LOAD    = {PROBE_COLD_LOAD}\n"
    )


if __name__ == "__main__":
    ensure_dirs()
    print(describe())
