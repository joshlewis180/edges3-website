#!/usr/bin/env bash
# Daily EDGES-3 pipeline driver, intended to be called by systemd.
#
# Forwards to ``python backend/daemon.py --run-now``, which picks the
# latest available cal/s11/spec dates from the raw data tree, invokes
# run_single_day.py, and updates latest_run.json so the frontend
# reflects the new run on next page-load.
#
# Exit codes:
#   0  - run finished cleanly
#   1  - run_once() returned None (no data or pipeline failed)

set -euo pipefail

# --- Configuration (override via /etc/default/edges-pipeline if present) ---
: "${EDGES_REPO:=/opt/edges-interface}"
: "${EDGES_PYTHON:=/opt/anaconda3/envs/edges/bin/python}"
: "${EDGES_RAW_DATA_ROOT:=/data5/edges/data/EDGES3_data/MRO}"
: "${EDGES_OUTPUT_ROOT:=/data5/edges/edges_outputs}"
: "${EDGES_TEMP_LOG_DIR:=/data5/edges/data/EDGES3_data/MRO/temperature_logger}"
: "${EDGES_BEAM_FACTOR_FILE:=/data5/edges/e3_beam_factor.hickle}"
: "${EDGES_DAEMON_ENABLED:=0}"

if [[ -f /etc/default/edges-pipeline ]]; then
    # shellcheck disable=SC1091
    source /etc/default/edges-pipeline
fi

export EDGES_RAW_DATA_ROOT EDGES_OUTPUT_ROOT EDGES_TEMP_LOG_DIR \
       EDGES_BEAM_FACTOR_FILE EDGES_DAEMON_ENABLED

# Make the conda env's Python the explicit one so the unit file's
# ExecStart is the same as this script's.
export PYTHON="$EDGES_PYTHON"

echo "[edges-pipeline] starting daemon.run_once() via $EDGES_PYTHON"
exec "$EDGES_PYTHON" "$EDGES_REPO/backend/daemon.py" --run-now \
    --log-level INFO
