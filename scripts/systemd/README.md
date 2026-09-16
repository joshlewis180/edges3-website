# systemd deployment

Two ways to run the pipeline under systemd.

## Option A — long-running uvicorn with in-process scheduler (simpler)

A single `edges-api.service` runs uvicorn forever. Set
`EDGES_DAEMON_ENABLED=1` in the unit and the in-process scheduler inside
uvicorn fires `run_single_day.py` once per day at `EDGES_DAEMON_HOUR`.

Files: `edges-api.service`

## Option B — systemd timer (more robust, no always-on Python process)

A `edges-pipeline.timer` calls `edges-pipeline.service` once per day,
which in turn runs `scripts/edges-pipeline.sh`. That script scans the
raw data tree for the latest dates, then calls `run_single_day.py`
directly. With this option, set `EDGES_DAEMON_ENABLED=0` in
`edges-api.service` so the in-process scheduler is disabled.

Files: `edges-pipeline.service`, `edges-pipeline.timer`, `../edges-pipeline.sh`

## Install (both options)

```bash
# 1. Create a non-root user for the service
sudo useradd --system --home /opt/edges-interface --shell /bin/bash edges
sudo mkdir -p /opt/edges-interface
sudo chown -R edges:edges /opt/edges-interface
# 2. Clone the repo there
sudo -u edges git clone <repo-url> /opt/edges-interface
# 3. Create the conda env
sudo -u edges conda create -n edges python=3.11 -c conda-forge \
    edges-analysis edges-io pygsdata read-acq astropy
sudo -u edges -H bash -c 'source activate edges && pip install -r /opt/edges-interface/backend/requirements.txt'
# 4. Make sure /data5/... is readable by `edges`
sudo chown -R edges:edges /data5/edges/edges_outputs
# 5. Pick option A or B
sudo cp systemd/edges-api.service           /etc/systemd/system/   # always
sudo cp systemd/edges-pipeline.service      /etc/systemd/system/   # option B only
sudo cp systemd/edges-pipeline.timer        /etc/systemd/system/   # option B only
sudo cp ../edges-pipeline.sh                 /opt/edges-interface/scripts/
sudo chmod +x                                /opt/edges-interface/scripts/edges-pipeline.sh
# 6. Edit env vars in /etc/systemd/system/edges-*.service to match your paths
sudo systemctl daemon-reload
sudo systemctl enable --now edges-api.service
sudo systemctl enable --now edges-pipeline.timer    # option B only
# 7. Inspect
sudo systemctl status edges-api.service
sudo journalctl -u edges-api.service -f
sudo systemctl list-timers --all | grep edges
```

## Environment overrides

For `edges-pipeline.sh`, put overrides in `/etc/default/edges-pipeline`:

```bash
EDGES_RAW_DATA_ROOT=/data5/edges/data/EDGES3_data/MRO
EDGES_OUTPUT_ROOT=/data5/edges/edges_outputs
EDGES_TEMP_LOG_DIR=/data5/edges/data/EDGES3_data/MRO/temperature_logger
EDGES_BEAM_FACTOR_FILE=/data5/edges/e3_beam_factor.hickle
EDGES_PYTHON=/opt/anaconda3/envs/edges/bin/python
EDGES_RUN_HASH=
```

For the systemd units, edit the `Environment=` lines in the unit file
or drop in a `EnvironmentFile=/etc/default/edges-api` line.

## Logs

```bash
sudo journalctl -u edges-api.service -f
sudo journalctl -u edges-pipeline.service -f
sudo journalctl -u edges-pipeline.timer -f
```

## Manually trigger the daily run

```bash
sudo systemctl start edges-pipeline.service
# or
sudo -u edges /opt/edges-interface/scripts/edges-pipeline.sh
```
