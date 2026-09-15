#!/usr/bin/env python3
"""
EDGES-3 Single-Day Analysis Pipeline
=====================================

Reads raw calibration and antenna .acq files, runs receiver calibration,
computes the antenna S11, applies Dicke and noise-wave calibration, and writes
all required outputs (.npz for 1D, JPEG for 2D waterfalls, .npz for heatmaps).

Used in two contexts:
  * Daemon — runs once a day with the latest dates, no 2D heatmap data
  * User   — runs from the website when the user changes dates / parameters

All paths come from ``config.py`` so the same script works on macOS and on the
enterprise cluster (via environment variables).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from astropy import units as un
from astropy.time import Time

# Allow ``python run_single_day.py`` to import config.py / io_utils.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from io_utils import (  # noqa: E402
    PAGE_CALIBRATED,
    PAGE_CALIBRATION,
    PAGE_RAW,
    Plot,
    compute_run_hash,
    parse_timestamp,
    parse_yyyy_ddd,
    save_1d_npz,
    save_heatmap_npz,
    save_waterfall_jpeg,
    write_manifest,
)

from pygsdata import GSData  # noqa: E402
from read_acq.gsdata import fast_lst_setter, read_acq_to_gsdata  # noqa: E402
from edges.const import KNOWN_TELESCOPES  # noqa: E402
from edges.alanmode import (  # noqa: E402
    Edges3CalobsParams,
    EdgesScriptParams,
    ACQPlot7aMoonParams,
    read_specal,
)
from edges.alanmode.cli import AlanCalOpts, alancal_edges3  # noqa: E402
from edges.cal.apply import approximate_temperature  # noqa: E402
from edges.cal.dicke import dicke_calibration  # noqa: E402
from edges.analysis.calibrate import apply_noise_wave_calibration  # noqa: E402
from edges.cal import ReflectionCoefficient, S11ModelParams, sparams as sp  # noqa: E402
from edges.frequencies import get_mask  # noqa: E402
import edges.io as io  # noqa: E402
import edges.modeling as mdl  # noqa: E402


# ---------------------------------------------------------------------------
# Defaults — match the originals so behaviour is preserved when no flags set
# ---------------------------------------------------------------------------
DEFAULT_CTERMS = 6
DEFAULT_WTERMS = 5
DEFAULT_FSTART = 50.0
DEFAULT_FSTOP = 190.0
DEFAULT_WFSTART = 50.0
DEFAULT_WFSTOP = 190.0

CAL_LOADS = ("amb", "hot", "open", "short")

# ---------------------------------------------------------------------------
# Calibration temperature derivation
# ---------------------------------------------------------------------------
# All calibration temperatures are auto-derived from the temperature log.
# Probe numbers are the ``offset_s`` values in the on-site temperature
# log file. There are no user-tunable setpoints anymore — whatever the
# probe says is what the analysis uses.
PROBE_AMBIENT = 100.0       # ambient sensor (used for ambient + LNA cals)
PROBE_HOT = 102.0           # hot load sensor
PROBE_LNA = 100.0           # LNA / cable temp (same physical probe)
PROBE_COLD_LOAD = 152.0     # cold load sensor (informational)


def compute_calibration_temps(
    cal_data: Dict[str, Any],
    blocks: List[Dict[str, Any]],
    obs_time: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """Derive the per-load calibration temperatures from probe readings.

    Returns a dict keyed by ``"ambient"``, ``"hot"``, ``"lna"``. Each
    value is a dict with ``temperature_k``, ``temperature_c``,
    ``probe``, ``time`` and ``source`` (``"probe"`` or ``"default"``).
    Falls back to the configured defaults if no probe reading is found.

    For ``lna`` there is no separate calibration file — the LNA cal
    happens at the raw observation time. If ``obs_time`` is supplied we
    use it as the LNA lookup timestamp; otherwise we fall back to the
    default.
    """
    from config import (
        TCOLD_FALLBACK_K, THOT_FALLBACK_K, TCAB_FALLBACK_K,
        PROBE_AMBIENT, PROBE_HOT, PROBE_LNA,
    )
    defaults = {"ambient": TCOLD_FALLBACK_K, "hot": THOT_FALLBACK_K, "lna": TCAB_FALLBACK_K}
    cal_keys = {"ambient": "amb", "hot": "hot", "lna": "lna"}
    probes = {"ambient": PROBE_AMBIENT, "hot": PROBE_HOT, "lna": PROBE_LNA}

    result: Dict[str, Dict[str, Any]] = {}
    for display, cal_key in cal_keys.items():
        default_k = defaults[display]
        probe = probes[display]
        entry: Dict[str, Any] = {
            "temperature_k": default_k,
            "temperature_c": default_k - 273.15,
            "probe": probe,
            "time": None,
            "source": "default",
        }
        # Pick the timestamp we look up against. LNA has no cal file, so
        # use the observation time when available.
        lookup_time: Optional[datetime] = None
        if cal_key in cal_data:
            try:
                lookup_time = cal_data[cal_key].times[0, 0].to_datetime()
            except Exception:
                lookup_time = None
        elif display == "lna" and obs_time is not None:
            lookup_time = obs_time
        if not blocks or lookup_time is None:
            print(
                f"[run] cal temp {display}: {default_k:.2f} K "
                f"(default — no log / no {cal_key} cal data)"
            )
            result[display] = entry
            continue
        t_c = get_temperature_at_time(blocks, lookup_time, probe=probe)
        if t_c is None:
            print(
                f"[run] WARN: probe {probe} has no data at {display} "
                f"cal time {lookup_time.isoformat()}; using default {default_k:.2f} K"
            )
            result[display] = entry
            continue
        entry["temperature_c"] = float(t_c)
        entry["temperature_k"] = float(t_c) + 273.15
        entry["time"] = lookup_time.isoformat()
        entry["source"] = "probe"
        print(
            f"[run] cal temp {display}: {entry['temperature_k']:.2f} K "
            f"(from probe {probe} at {lookup_time.isoformat()})"
        )
        result[display] = entry
    return result


def print_probe_survey(blocks: List[Dict[str, Any]]) -> None:
    """Print a summary of every probe seen in the temperature log.

    Useful for figuring out which probe number maps to which physical
    sensor (ambient / hot load / cold load / cable). Skipped silently
    when no log data is loaded.
    """
    if not blocks:
        print("[run] probe survey: no temperature log data")
        return
    samples: Dict[float, List[float]] = {}
    for b in blocks:
        for off_s, temp in b.get("readings", []):
            try:
                p = float(off_s)
                samples.setdefault(p, []).append(float(temp))
            except (TypeError, ValueError):
                continue
    print(f"[run] probe survey ({len(blocks)} blocks):")
    for p in sorted(samples):
        s = samples[p]
        print(
            f"  probe {p:>5.0f}: {len(s):>5} samples  "
            f"min {min(s):>7.2f} C  max {max(s):>7.2f} C  "
            f"mean {sum(s) / len(s):>7.2f} C"
        )


# ---------------------------------------------------------------------------
# Run identification
# ---------------------------------------------------------------------------
def generate_run_id(params: Dict[str, Any]) -> str:
    """Deterministic 16-char id from a parameter dict (sorted JSON)."""
    run_str = json.dumps(params, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(run_str).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Temperature log parsing
# ---------------------------------------------------------------------------
def parse_temperature_log_dir(log_dir: Path) -> List[Dict[str, Any]]:
    """Load every ``*.log`` file in ``log_dir`` and return their merged blocks.

    The on-site logger writes a new file every so often (per session,
    per day, or per sensor). We treat them all as a single timeline so the
    nearest-in-time lookup sees the full history regardless of which file
    a particular reading lives in.
    """
    merged: List[Dict[str, Any]] = []
    if not log_dir.exists():
        return merged
    paths = sorted(log_dir.glob("*.log"))
    if not paths:
        # Fall back to a single explicit file in case the directory layout
        # differs (e.g. the user is still pointing at a specific file).
        paths = [log_dir]
    for p in paths:
        if p.is_file():
            merged.extend(parse_temperature_log(p))
    # Sort blocks by their start timestamp so callers see a coherent timeline.
    merged.sort(key=lambda b: b["start"] or b.get("readings", [("", 0.0)])[0])
    return merged


def parse_temperature_log(log_path: Path) -> List[Dict[str, Any]]:
    """Parse the on-site thermostat log into a list of measurement blocks."""
    blocks: List[Dict[str, Any]] = []
    if not log_path.exists():
        return blocks

    current: Optional[Dict[str, Any]] = None
    with open(log_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if re.match(r"^\d{4}_\d{3}_\d+$", line):
                if current is not None:
                    blocks.append(current)
                current = {"start": None, "readings": []}
                continue
            if current is None:
                continue
            if current["start"] is None:
                try:
                    current["start"] = datetime.strptime(
                        line, "%a %b %d %H:%M:%S %Z %Y"
                    )
                except ValueError:
                    pass
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    current["readings"].append((float(parts[0]), float(parts[1])))
                except ValueError:
                    pass
    if current is not None:
        blocks.append(current)
    return blocks


def get_temperature_at_time(
    blocks: List[Dict[str, Any]], target_time: datetime,
    probe: float = 100.0,
) -> Optional[float]:
    """Find the reading from probe ``probe`` closest in time to ``target_time``.

    The on-site temperature log records several probes per block with
    different physical roles:

      * ``0`` is always exactly 25.00 (it's the setpoint, not a measurement)
      * ``100`` is the antenna ambient temperature (the one we want)
      * ``102`` is the hot load (~110 °C)
      * ``152`` is the cold load (~1 °C)

    We default to ``ambient_probe=100`` and find its reading nearest in
    time. If no reading from that probe is available we fall back to the
    other ambient-shaped probes (101, 103), and finally to whichever
    reading is closest in absolute time.

    Each ``block`` has a wall-clock ``start`` and a list of ``(offset_s,
    temp)`` readings where ``offset_s`` is seconds since ``start``. We
    compute the absolute reading time of every sample as
    ``start + offset_s``.
    """
    target_ts = target_time.timestamp()
    # Fallback probes we try if the requested probe isn't available.
    # For ambient-shaped probes (100/101/103) we substitute each other.
    ambient_aliases: Dict[float, Tuple[float, ...]] = {
        100.0: (100.0, 101.0, 103.0),
        101.0: (101.0, 100.0, 103.0),
        103.0: (103.0, 100.0, 101.0),
    }
    aliases = ambient_aliases.get(probe, (probe,))
    for pref in aliases:
        best: Optional[Tuple[float, float]] = None
        for block in blocks:
            start = block.get("start")
            if start is None:
                continue
            for offset_s, temp in block["readings"]:
                if float(offset_s) != pref:
                    continue
                try:
                    reading_ts = start.timestamp() + float(offset_s)
                    temp_val = float(temp)
                except (TypeError, ValueError):
                    continue
                if best is None or abs(reading_ts - target_ts) < best[0]:
                    best = (abs(reading_ts - target_ts), temp_val)
        if best is not None:
            return best[1]
    # Final fallback: absolute nearest reading, any probe.
    for block in blocks:
        start = block.get("start")
        if start is None:
            continue
        for offset_s, temp in block["readings"]:
            try:
                reading_ts = start.timestamp() + float(offset_s)
                temp_val = float(temp)
            except (TypeError, ValueError):
                continue
            fallback.append((abs(reading_ts - target_ts), temp_val))
    if not fallback:
        return None
    fallback.sort(key=lambda pair: pair[0])
    return fallback[0][1]  # type: ignore[no-any-return]  # nearest sample


# ---------------------------------------------------------------------------
# Raw data readers
# ---------------------------------------------------------------------------
def read_calibration_acq(root: Path, cal_date: str) -> Dict[str, GSData]:
    year = cal_date.split("_")[0]
    cal_data: Dict[str, GSData] = {}
    for load in CAL_LOADS:
        acq_dir = root / "mro" / load / year
        files = sorted(acq_dir.glob(f"{cal_date}_*_{load}.acq"))
        if not files:
            print(f"[run] WARN: no {load} calibration file for {cal_date}")
            continue
        cal_data[load] = read_acq_to_gsdata(
            files[0],
            telescope=KNOWN_TELESCOPES["edges3"],
            name=f"cal_{load}_{cal_date}",
            lst_setter=fast_lst_setter,
        )
    return cal_data


def read_antenna_acq(root: Path, spec_timestamp: str) -> GSData:
    year = spec_timestamp.split("_")[0]
    acq_dir = root / "mro" / "ant" / year
    files = sorted(acq_dir.glob(f"{spec_timestamp}_ant.acq"))
    if not files:
        files = sorted(acq_dir.glob(f"{spec_timestamp}*_ant.acq"))
    if not files:
        raise FileNotFoundError(f"No antenna file for {spec_timestamp}")
    return read_acq_to_gsdata(
        files[0],
        telescope=KNOWN_TELESCOPES["edges3"],
        name=f"ant_{spec_timestamp}",
        lst_setter=fast_lst_setter,
    )


# ---------------------------------------------------------------------------
# Receiver calibration
# ---------------------------------------------------------------------------
def run_receiver_calibration(
    root: Path,
    cal_date: str,
    s11_run: str,
    outdir: Path,
    cterms: int,
    wterms: int,
    ambient_temp_k: float,
    hot_temp_k: float,
    cable_temp_k: float,
    fstart: float,
    fstop: float,
    wfstart: float,
    wfstop: float,
) -> Tuple[Path, Path]:
    """Run the EDGES receiver calibration.

    ``ambient_temp_k`` / ``hot_temp_k`` / ``cable_temp_k`` are the
    PROBE-MEASURED temperatures of the ambient load, hot load, and LNA
    cable at the time of each calibration. EDGES uses these as the
    *known* reference temperatures in the noise-wave fit; whatever we
    pass becomes the value reported in ``calibrated_temps.txt``. So we
    must pass the actual probe readings (not hardcoded setpoints) to
    keep the noise-wave model honest.
    """
    year, day = parse_yyyy_ddd(cal_date)
    outdir.mkdir(parents=True, exist_ok=True)

    alancal_edges3(
        data=Edges3CalobsParams(
            specyear=year,
            specday=day,
            s11date=s11_run,
            datadir=root,
            match_resistance=49.8,
            calkit_delays=33,
            lna_cable_length=4.26,
            lna_cable_loss=-91.5,
            lna_cable_dielectric=-1.24,
        ),
        opts=AlanCalOpts(
            avg=ACQPlot7aMoonParams(
                fstart=fstart,
                fstop=fstop,
                delaystart=0,
                smooth=8,
                tload=300,
                tcal=1000,
            ),
            cal=EdgesScriptParams(
                wfstart=wfstart,
                wfstop=wfstop,
                Lh=-1,
                thot=hot_temp_k,
                tcold=ambient_temp_k,
                tcab=cable_temp_k,
                cfit=cterms,
                wfit=wterms,
                nfit2=27,
                nfit3=10,
            ),
            plot=False,
            out=outdir,
            redo_spectra=False,
            redo_cal=True,
        ),
    )

    specal = outdir / "specal.txt"
    s11_modelled = outdir / "s11_modelled.txt"
    if not specal.exists():
        raise FileNotFoundError("specal.txt was not created by alancal")
    if not s11_modelled.exists():
        raise FileNotFoundError("s11_modelled.txt was not created by alancal")
    return specal, s11_modelled


# ---------------------------------------------------------------------------
# Antenna S11
# ---------------------------------------------------------------------------
def compute_antenna_s11(
    root: Path,
    s11_run: str,
    target_freqs: Optional[Any] = None,
) -> Tuple[ReflectionCoefficient, np.ndarray, np.ndarray]:
    """Return ``(ant_s11_model, freqs_MHz, real, imag)`` evaluated at target freqs."""
    base = root / s11_run

    ck = sp.CalkitReadings.from_filespec(
        io.CalkitFileSpec(
            open=f"{base}_O.s1p",
            short=f"{base}_S.s1p",
            match=f"{base}_L.s1p",
        )
    )
    calkit = sp.get_calkit(
        sp.AGILENT_ALAN,
        resistance_of_match=49.930 * un.ohm,
        short={"offset_delay": 33 * un.ps},
        open={"offset_delay": 33 * un.ps},
        match={"offset_delay": 33 * un.ps},
    )

    ant_s11_raw = sp.ReflectionCoefficient.from_s1p(f"{base}_ant.s1p")
    gamma_ant = sp.calibrate_gamma_src(
        gamma_src=ant_s11_raw,
        internal_calkit=calkit,
        internal_osl=ck,
    )

    f_low, f_high = 58.0, 105.0
    mask = get_mask(gamma_ant.freqs, f_low * un.MHz, f_high * un.MHz)
    ants11_raw = ReflectionCoefficient(
        reflection_coefficient=gamma_ant.reflection_coefficient[mask],
        freqs=gamma_ant.freqs[mask],
    )

    common_kwargs = dict(
        params=S11ModelParams(
            model=mdl.Polynomial(
                n_terms=12,
                transform=mdl.Log10Transform(scale=(f_low + f_high) / 2),
            ),
            complex_model_type=mdl.ComplexRealImagModel,
            set_transform_range=True,
            fit_method="alan-qrd",
            find_model_delay=True,
        )
    )
    if target_freqs is not None:
        ants11_model = ants11_raw.smoothed(freqs=target_freqs, **common_kwargs)
    else:
        ants11_model = ants11_raw.smoothed(**common_kwargs)

    freqs_mhz = ants11_model.freqs.to_value("MHz")
    real = ants11_model.reflection_coefficient.real
    imag = ants11_model.reflection_coefficient.imag
    return ants11_model, freqs_mhz, real, imag


# ---------------------------------------------------------------------------
# 1D saving helpers (wrap io_utils with domain-specific defaults)
# ---------------------------------------------------------------------------
def _save_1d(out_dir: Path, fname: str, x: np.ndarray, y: np.ndarray) -> Path:
    return save_1d_npz(out_dir, fname, x=x, y=y)


def _save_freq_y(
    out_dir: Path,
    fname: str,
    freqs: np.ndarray,
    y: np.ndarray,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    return save_1d_npz(out_dir, fname, x=freqs, y=y, metadata=metadata)


def _save_freq_realimag(
    out_dir: Path, fname: str, freqs: np.ndarray, real: np.ndarray, imag: np.ndarray
) -> Path:
    """S11 .npz with keys frequency, real, imag (matches s11Loader.ts)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / fname
    np.savez(path, frequency=np.asarray(freqs), real=np.asarray(real), imag=np.asarray(imag))
    return path


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def process_single_day(
    raw_root: Path,
    output_root: Path,
    run_dir: Path,
    cal_date: str,
    s11_date: str,
    spec_date: str,
    *,
    cterms: int = DEFAULT_CTERMS,
    wterms: int = DEFAULT_WTERMS,
    fstart: float = DEFAULT_FSTART,
    fstop: float = DEFAULT_FSTOP,
    wfstart: float = DEFAULT_WFSTART,
    wfstop: float = DEFAULT_WFSTOP,
    save_2d_npz: bool = False,
    temperature_log: Optional[Path] = None,
    source: str = "user",
    run_hash: Optional[str] = None,
) -> Path:
    """Run the full pipeline for a single day. Returns the manifest path.

    If ``run_hash`` is supplied the caller is responsible for dedup; this
    function just uses the value for the provenance marker.
    """
    raw_root = Path(raw_root)
    output_root = Path(output_root)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    temperature_log = Path(temperature_log) if temperature_log else config.TEMPERATURE_LOG_FILE

    dates = {"cal": cal_date, "s11": s11_date, "raw": spec_date}

    # ---- 0. Calibration temperature derivation ------------------------------
    # Load the calibration .acq and the temperature log up front so we can
    # derive the per-load calibration temperatures from probe readings.
    # These probe readings are passed straight to the EDGES receiver
    # calibration, so ``calibrated_temps.txt`` will reflect the actual
    # physical temperatures rather than echoing back hardcoded setpoints.
    print(f"[run] Loading calibration .acq + temperature log from "
          f"{config.TEMPERATURE_LOG_DIR} ...")
    cal_data = read_calibration_acq(raw_root, cal_date)
    # Find the raw observation .acq just to learn its timestamp; the LNA
    # cal doesn't have a separate file, so we use the obs time for the
    # ``tns`` lookup.
    obs_time: Optional[datetime] = None
    try:
        raw_for_time = read_antenna_acq(raw_root, spec_date)
        obs_time = raw_for_time.times[0, 0].to_datetime()
        del raw_for_time  # we'll re-read it below; cheap, just want the timestamp
    except Exception as exc:
        print(f"[run] WARN: could not read obs time ({exc}); tns will fall back to default")
    blocks: List[Dict[str, Any]] = []
    if config.TEMPERATURE_LOG_DIR.exists():
        blocks = parse_temperature_log_dir(config.TEMPERATURE_LOG_DIR)
        if not blocks:
            print(f"[run] WARN: no temperature log files under {config.TEMPERATURE_LOG_DIR}")
    else:
        print(f"[run] WARN: temperature log directory missing ({config.TEMPERATURE_LOG_DIR})")
    print_probe_survey(blocks)
    cal_temps = compute_calibration_temps(cal_data, blocks, obs_time=obs_time)
    # All calibration temperatures come from probe readings. The probe
    # at the ambient cal time is the *ambient* temperature (which is
    # also a good proxy for the cable temperature since the cable sits
    # at ambient); the probe at the hot cal time is the *hot load*
    # temperature; the probe at the LNA obs time is what we use for
    # ``tns`` (cable / noise source).
    ambient_k = cal_temps["ambient"]["temperature_k"]
    hot_k = cal_temps["hot"]["temperature_k"]
    lna_k = cal_temps["lna"]["temperature_k"]
    print(f"[run] probe-derived calibration temperatures: "
          f"ambient={ambient_k:.2f} K  hot={hot_k:.2f} K  lna/cable={lna_k:.2f} K")

    # ---- 1. Receiver calibration --------------------------------------------
    # The EDGES noise-wave fit needs the *known* load temperatures.
    # Whatever we pass here becomes the value reported back in
    # ``calibrated_temps.txt``. We pass the probe readings so the fit
    # converges on the actual physical temperatures rather than echoing
    # back hardcoded setpoints.
    print("[run] Receiver calibration ...")
    calib_dir = run_dir / "calibration"
    specal_file, s11_modelled_file = run_receiver_calibration(
        raw_root, cal_date, s11_date, calib_dir,
        cterms, wterms,
        ambient_temp_k=ambient_k,
        hot_temp_k=hot_k,
        cable_temp_k=ambient_k,   # cable sits in ambient
        fstart=fstart, fstop=fstop, wfstart=wfstart, wfstop=wfstop,
    )
    # ``tload`` (hot-load reference for Dicke switching) and ``tns``
    # (cable / noise source temperature) are derived from the same
    # probe readings.
    tload = hot_k
    tns = lna_k
    calobs = read_specal(specal_file, t_load=tload, t_load_ns=tns)

    # ---- 2. Antenna data ----------------------------------------------------
    print(f"[run] Reading antenna .acq for {spec_date}")
    raw_data = read_antenna_acq(raw_root, spec_date)
    data_freqs_mhz = raw_data.freqs.to_value("MHz")
    target_freqs = un.Quantity(data_freqs_mhz, "MHz")

    # ---- 3. Antenna S11 at the data frequencies -----------------------------
    print("[run] Computing antenna S11 ...")
    ant_s11_model, ant_freqs_mhz, ant_real, ant_imag = compute_antenna_s11(
        raw_root, s11_date, target_freqs=target_freqs,
    )

    # ---- 4. P0/P1/P2/Q and raw spectra --------------------------------------
    p0 = raw_data.data[0, 0]
    p1 = raw_data.data[1, 0]
    p2 = raw_data.data[2, 0]
    with np.errstate(divide="ignore", invalid="ignore"):
        q = (p0 - p1) / (p2 - p1)
    lsts = raw_data.lsts[:, 0].to_value("hourangle").flatten()

    for name, arr in (("P0", p0), ("P1", p1), ("P2", p2), ("Q", q)):
        avg = np.nanmean(arr, axis=0)
        _save_freq_y(run_dir / "raw_spectra", f"{spec_date}_{name}.npz", data_freqs_mhz, avg)
        save_waterfall_jpeg(
            arr, data_freqs_mhz, lsts, run_dir / "raw_waterfalls",
            f"{spec_date}_{name}.jpg", title=f"{name} {spec_date}",
        )
        if save_2d_npz:
            save_heatmap_npz(
                run_dir / "raw_waterfalls", f"{spec_date}_{name}_2d.npz",
                x=data_freqs_mhz, y=lsts, z=arr,
            )

    # ---- 5. Calibration S11s ------------------------------------------------
    print("[run] Saving calibration S11s ...")
    s11m = np.genfromtxt(s11_modelled_file, comments="#", names=True)
    for load in CAL_LOADS + ("lna",):
        _save_freq_realimag(
            run_dir / "calibration_s11", f"{s11_date}_{load}_S11.npz",
            s11m["freq"], s11m[f"{load}_real"], s11m[f"{load}_imag"],
        )

    # ---- 6. Calibration spectra (images + 1D averages) -----------------------
    print("[run] Saving calibration spectra ...")
    cal_spec_lsts: Dict[str, np.ndarray] = {}
    for load, gs in cal_data.items():
        spec = gs.data[0, 0]
        freqs_cal = gs.freqs.to_value("MHz")
        lsts_cal = gs.lsts[:, 0].to_value("hourangle").flatten()
        cal_spec_lsts[load] = lsts_cal
        save_waterfall_jpeg(
            spec, freqs_cal, lsts_cal,
            run_dir / "calibration_spectra", f"{cal_date}_{load}.jpg",
            title=f"{load} calibration spectrum {cal_date}",
        )
        spec_avg = np.nanmean(spec, axis=0)
        _save_freq_y(
            run_dir / "calibration_spectra", f"{cal_date}_{load}_avg.npz",
            freqs_cal, spec_avg,
        )
        if save_2d_npz:
            save_heatmap_npz(
                run_dir / "calibration_spectra", f"{cal_date}_{load}_2d.npz",
                x=freqs_cal, y=lsts_cal, z=spec,
            )

    # ---- 7. TNW coefficients -----------------------------------------------
    print("[run] Saving TNW coefficients ...")
    coeff_dir = run_dir / "calibration_coefficients"
    coeff_dir.mkdir(parents=True, exist_ok=True)
    cal_freqs_mhz = calobs.freqs.to_value("MHz")
    _save_freq_y(coeff_dir, f"{cal_date}_scale.npz", cal_freqs_mhz, np.asarray(calobs.Tsca))
    _save_freq_y(coeff_dir, f"{cal_date}_offset.npz", cal_freqs_mhz, np.asarray(calobs.Toff))
    _save_freq_y(coeff_dir, f"{cal_date}_unc.npz", cal_freqs_mhz, np.asarray(calobs.Tunc))
    _save_freq_y(coeff_dir, f"{cal_date}_cos.npz", cal_freqs_mhz, np.asarray(calobs.Tcos))
    _save_freq_y(coeff_dir, f"{cal_date}_sin.npz", cal_freqs_mhz, np.asarray(calobs.Tsin))
    # T0 and T1 — kept as separate semantic files
    T0 = getattr(calobs, "T0", None)
    T1 = getattr(calobs, "T1", None)
    if T0 is None:
        # Fallback: T0 = offset for stability if backend doesn't expose it
        T0 = np.asarray(calobs.Toff)
    if T1 is None:
        # Fallback: T1 = scale / tns
        T1 = np.asarray(calobs.Tsca) / tns
    _save_freq_y(coeff_dir, f"{cal_date}_T0.npz", cal_freqs_mhz, np.asarray(T0))
    _save_freq_y(coeff_dir, f"{cal_date}_T1.npz", cal_freqs_mhz, np.asarray(T1))

    # ---- 8. Calibration temperatures derived from the analysis ------------
    # The receiver calibration writes two relevant files:
    #   * ``calibrated_temps.txt`` — per-frequency load temperatures
    #     (ambient / hot_load / open / short) solved for by the noise-wave
    #     model fit.
    #   * ``specal.txt`` — the full per-frequency noise-wave model
    #     parameters, including ``Tunc`` = the LNA's noise temperature.
    # These are the *actual* values the analysis determined, NOT the
    # setpoints. The comparison plot (Calibration vs Actual Temp) plots
    # these against the probe-measured actual temperature.
    print("[run] Saving calibration temperatures (from analysis) ...")
    cal_temp_dir = run_dir / "calibration_temperatures"
    cal_temp_dir.mkdir(parents=True, exist_ok=True)

    cal_fit: Dict[str, np.ndarray] = {}
    cal_fit_freqs: Optional[np.ndarray] = None
    cal_fit_file = calib_dir / "calibrated_temps.txt"
    if cal_fit_file.exists():
        try:
            arr = np.genfromtxt(cal_fit_file, comments="#", names=True)
            cal_fit_freqs = np.asarray(arr["freq"])
            cal_fit = {
                "ambient": np.asarray(arr["ambient"], dtype=float),
                "hot":     np.asarray(arr["hot_load"], dtype=float),
                "open":    np.asarray(arr["open"], dtype=float),
                "short":   np.asarray(arr["short"], dtype=float),
            }
            print(f"[run] loaded {cal_fit_file.name} ({len(cal_fit_freqs)} freq bins)")
        except Exception as exc:
            print(f"[run] WARN: failed to parse {cal_fit_file}: {exc}")
    else:
        print(f"[run] WARN: {cal_fit_file} not found")

    # LNA noise temperature from specal.txt (column ``Tunc``). The file
    # uses a token-based format with column labels interleaved with the
    # values, so we parse it explicitly rather than via genfromtxt.
    specal_file = calib_dir / "specal.txt"
    lna_freqs: Optional[np.ndarray] = None
    lna_temp: Optional[np.ndarray] = None
    if specal_file.exists():
        try:
            rows: List[List[str]] = []
            with open(specal_file, "r") as fh:
                for line in fh:
                    toks = line.strip().split()
                    if not toks or toks[0] != "freq":
                        continue
                    # Tokens: freq F s11lna R I sca S ofs O tlnau T tlnac C tlnas N wtcal W cal_data
                    rows.append(toks)
            lna_freqs = np.array([float(r[1]) for r in rows])
            lna_temp = np.array([float(r[10]) for r in rows])
            print(f"[run] loaded {specal_file.name}: LNA Tunc range "
                  f"{lna_temp.min():.2f}..{lna_temp.max():.2f} K")
        except Exception as exc:
            print(f"[run] WARN: failed to parse {specal_file}: {exc}")
    else:
        print(f"[run] WARN: {specal_file} not found; LNA will fall back to setpoint")

    def _save_cal_temp(
        npz_key: str,
        analysis_key: Optional[str],
        fallback_k: float,
    ) -> None:
        if analysis_key == "lna" and lna_temp is not None and lna_freqs is not None:
            _save_freq_y(cal_temp_dir, f"{cal_date}_{npz_key}.npz", lna_freqs, lna_temp)
            return
        if (
            analysis_key is not None
            and analysis_key in cal_fit
            and cal_fit_freqs is not None
        ):
            _save_freq_y(
                cal_temp_dir, f"{cal_date}_{npz_key}.npz",
                cal_fit_freqs, cal_fit[analysis_key],
            )
            return
        # Fall back: write a horizontal line at the configured default
        # (setpoint). Only happens if the receiver cal didn't run.
        _save_freq_y(
            cal_temp_dir, f"{cal_date}_{npz_key}.npz",
            data_freqs_mhz, np.full_like(data_freqs_mhz, fallback_k),
        )

    _save_cal_temp("ambient", "ambient", ambient_k)
    _save_cal_temp("hot",     "hot",     hot_k)
    _save_cal_temp("lna",     "lna",     lna_k)
    _save_cal_temp("open",    "open",    np.nan)
    _save_cal_temp("short",   "short",   np.nan)

    # ---- 9. Dicke + noise-wave calibration ---------------------------------
    print("[run] Dicke calibration ...")
    dicke_data = dicke_calibration(raw_data)
    approx_temp = approximate_temperature(dicke_data, tload=tload, tns=tns)

    print("[run] Noise-wave calibration ...")
    cal_temp = apply_noise_wave_calibration(
        approx_temp,
        calibrator=calobs,
        antenna_s11=ant_s11_model,
        tload=tload,
        tns=tns,
    )

    avg_temp_data = np.nanmean(approx_temp.data[0, 0], axis=0)
    cal_temp_data = np.nanmean(cal_temp.data[0, 0], axis=0)
    _save_freq_y(
        run_dir / "average_temperature", f"{spec_date}_avg_temp.npz",
        data_freqs_mhz, avg_temp_data,
    )
    _save_freq_y(
        run_dir / "calibrated_temperature", f"{spec_date}_cal_temp.npz",
        data_freqs_mhz, cal_temp_data,
    )

    calibrated_lsts = cal_temp.lsts.to_value("hourangle").flatten()
    save_waterfall_jpeg(
        cal_temp.data[0, 0], data_freqs_mhz, calibrated_lsts,
        run_dir / "calibrated_waterfalls", f"{spec_date}_calibrated.jpg",
        title=f"Calibrated temperature {spec_date}",
    )
    if save_2d_npz:
        save_heatmap_npz(
            run_dir / "calibrated_waterfalls", f"{spec_date}_calibrated_2d.npz",
            x=data_freqs_mhz, y=calibrated_lsts, z=cal_temp.data[0, 0],
        )

    # ---- 10. Antenna S11 in its own folder --------------------------------
    _save_freq_realimag(
        run_dir / "antenna_s11", f"{s11_date}_antenna_S11.npz",
        ant_freqs_mhz, ant_real, ant_imag,
    )

    # ---- 11. Actual temperature -------------------------------------------
    actual_temp_dir = run_dir / "actual_temperature"
    actual_temp_dir.mkdir(parents=True, exist_ok=True)
    actual_temp_c = np.nan  # Celsius, from the temperature log
    try:
        obs_time = raw_data.times[0, 0].to_datetime()
    except Exception:
        obs_time = None
    # ``blocks`` was already loaded in step 0; just look up the probe now.
    if obs_time is not None and blocks:
        t = get_temperature_at_time(blocks, obs_time)
        if t is not None:
            actual_temp_c = t
        else:
            print("[run] WARN: temperature not found for observation time")
    else:
        print("[run] WARN: obs_time unavailable or no blocks parsed")
    # The temperature log records Celsius. Convert to Kelvin so it matches
    # the rest of the analysis (calibration temperatures, EDGES fit output).
    actual_temp_k = actual_temp_c + 273.15 if np.isfinite(actual_temp_c) else np.nan
    obs_time_iso = obs_time.isoformat() if obs_time is not None else None
    _save_freq_y(
        actual_temp_dir, f"{spec_date}_actual_temp.npz",
        data_freqs_mhz, np.full_like(data_freqs_mhz, actual_temp_k),
        metadata={
            "time": obs_time_iso,
            "temperature_k": float(actual_temp_k) if np.isfinite(actual_temp_k) else None,
            "temperature_c": float(actual_temp_c) if np.isfinite(actual_temp_c) else None,
        },
    )
    print(f"[run] actual temperature (raw obs @ {obs_time_iso}): {actual_temp_c:.2f}°C  =  {actual_temp_k:.2f} K")

    # ---- 11b. Per-load actual temperature (matched by timestamp) ----------
    # For each calibration measurement we save a separate npz whose y is
    # the actual temperature at the moment THAT calibration was taken.
    # That way the "ambient vs actual", "hot vs actual" and "LNA vs
    # actual" plots line up correctly in time.
    PER_LOAD_CALIBRATION = {
        "ambient": ("amb", 100.0),    # ambient probe (~25 °C / 298 K)
        "hot":     ("hot", 102.0),    # hot load probe (~110 °C / 383 K)
        "lna":     (None, 100.0),     # ambient probe, raw obs time
    }
    per_load_meta: Dict[str, Dict[str, Any]] = {}
    for display_name, (cal_key, probe) in PER_LOAD_CALIBRATION.items():
        load_time: Optional[datetime] = None
        if cal_key is not None and cal_key in cal_data:
            try:
                load_time = cal_data[cal_key].times[0, 0].to_datetime()
            except Exception:
                load_time = None
        else:
            load_time = obs_time  # LNA falls back to raw obs time
        load_actual_c = np.nan
        if load_time is not None and blocks:
            t = get_temperature_at_time(blocks, load_time, probe=probe)
            if t is not None:
                load_actual_c = t
            else:
                print(f"[run] WARN: no temperature for {display_name} (probe {probe}) at {load_time}")
        else:
            print(f"[run] WARN: missing timestamp or blocks for {display_name}")
        load_actual_k = load_actual_c + 273.15 if np.isfinite(load_actual_c) else np.nan
        load_time_iso = load_time.isoformat() if load_time is not None else None
        # Cache for the manifest so the per-load multi plots can carry the
        # sampling time + temperature in their metadata.
        per_load_meta[display_name] = {
            "time": load_time_iso,
            "temperature_k": float(load_actual_k) if np.isfinite(load_actual_k) else None,
        }
        _save_freq_y(
            actual_temp_dir, f"{spec_date}_{display_name}_actual_temp.npz",
            data_freqs_mhz, np.full_like(data_freqs_mhz, load_actual_k),
            metadata={
                "time": load_time_iso,
                "temperature_k": float(load_actual_k) if np.isfinite(load_actual_k) else None,
                "temperature_c": float(load_actual_c) if np.isfinite(load_actual_c) else None,
            },
        )
        print(f"[run] actual temperature ({display_name} @ {load_time_iso}): {load_actual_c:.2f}°C  =  {load_actual_k:.2f} K")

    # ---- 12. Build manifest -------------------------------------------------
    plots: List[Plot] = []
    rel = lambda *parts: f"runs/{run_dir.name}/" + "/".join(parts)

    # Calibration page
    for load in CAL_LOADS + ("lna",):
        plots.append(Plot(
            page=PAGE_CALIBRATION, id=f"{load}_s11", type="s11",
            title=f"{load.upper()} S11",
            filePath=rel("calibration_s11", f"{s11_date}_{load}_S11.npz"),
        ))
    for load in CAL_LOADS:
        plots.append(Plot(
            page=PAGE_CALIBRATION, id=f"{load}_cal_spectrum", type="image",
            title=f"{load.capitalize()} Calibration Spectrum  (Frequency [MHz] vs LST [hr])",
            filePath=rel("calibration_spectra", f"{cal_date}_{load}.jpg"),
        ))
        if save_2d_npz:
            plots.append(Plot(
                page=PAGE_CALIBRATION, id=f"{load}_waterfall", type="heatmap",
                title=f"{load.capitalize()} Calibration Waterfall  (Frequency [MHz] vs LST [hr])",
                filePath=rel("calibration_spectra", f"{cal_date}_{load}_2d.npz"),
            ))
    for coeff in ("scale", "offset", "unc", "cos", "sin", "T0", "T1"):
        title_map = {
            "scale": "Scale TNW  [K]",
            "offset": "Offset TNW  [K]",
            "unc": "Unc TNW  [K]",
            "cos": "Cos TNW  [K]",
            "sin": "Sin TNW  [K]",
            "T0": "T0  [K]",
            "T1": "T1  [K]",
        }
        plots.append(Plot(
            page=PAGE_CALIBRATION, id=coeff, type="single",
            title=title_map[coeff],
            filePath=rel("calibration_coefficients", f"{cal_date}_{coeff}.npz"),
            xKey="x", yKey="y",
            axisx="Frequency [MHz]",
            axisy="Temperature [K]",
        ))
    for load in ("ambient", "hot", "lna"):
        meta = per_load_meta.get(load, {})
        meta_time = meta.get("time")
        meta_temp = meta.get("temperature_k")
        # ``calibration_temperatures/{date}_{load}.npz`` holds the value
        # the noise-wave model fit against amb/hot/open/short spectra +
        # LNA S11 produced for this load. For ambient/hot it's the
        # physically expected load temperature; for lna it's the LNA's
        # noise temperature. ``actual_temperature/..._{load}_actual_temp.npz``
        # is the on-site temperature-log probe reading at the matching
        # time.
        if load == "ambient":
            title = "Ambient Calibration: Noise-wave Fit vs Ambient Probe  [K]"
            label1, label2 = "Noise-wave fit (load temp) [K]", "Ambient probe (probe 100) [K]"
        elif load == "hot":
            title = "Hot Calibration: Noise-wave Fit vs Hot-Load Probe  [K]"
            label1, label2 = "Noise-wave fit (load temp) [K]", "Hot-load probe (probe 102) [K]"
        else:  # lna
            title = "LNA Calibration: Noise-wave Fit (Tunc) vs Ambient Probe  [K]"
            label1, label2 = "Noise-wave fit (LNA Tunc) [K]", "Ambient probe (probe 100) [K]"
        plots.append(Plot(
            page=PAGE_CALIBRATION, id=f"{load}_vs_actual", type="multi",
            title=title,
            name1=label1, name2=label2,
            filePath1=rel("calibration_temperatures", f"{cal_date}_{load}.npz"),
            filePath2=rel("actual_temperature", f"{spec_date}_{load}_actual_temp.npz"),
            axisx="Frequency [MHz]",
            axisy="Temperature [K]",
            time=meta_time,
            temperature_k=meta_temp,
        ))

    # Calibrated page
    plots.append(Plot(
        page=PAGE_CALIBRATED, id="antenna_s11", type="s11",
        title="Antenna S11",
        filePath=rel("antenna_s11", f"{s11_date}_antenna_S11.npz"),
    ))
    plots.append(Plot(
        page=PAGE_CALIBRATED, id="calibrated_temperature", type="single",
        title="Calibrated Temperature  [K]",
        filePath=rel("calibrated_temperature", f"{spec_date}_cal_temp.npz"),
        xKey="x", yKey="y",
        axisx="Frequency [MHz]",
        axisy="Temperature [K]",
    ))
    if save_2d_npz:
        plots.append(Plot(
            page=PAGE_CALIBRATED, id="calibrated_waterfall", type="heatmap",
            title="Calibrated Temperature Waterfall  (Frequency [MHz] vs LST [hr])",
            filePath=rel("calibrated_waterfalls", f"{spec_date}_calibrated_2d.npz"),
        ))

    # Raw page
    for name, ylabel, axisy in (
        ("P0", "Antenna P0  [arb. units]", "Power [arb. units]"),
        ("P1", "Antenna P1  [arb. units]", "Power [arb. units]"),
        ("P2", "Antenna P2  [arb. units]", "Power [arb. units]"),
        ("Q",  "Antenna Q  (unitless)",   "Q (unitless)"),
    ):
        plots.append(Plot(
            page=PAGE_RAW, id=name, type="single",
            title=ylabel,
            filePath=rel("raw_spectra", f"{spec_date}_{name}.npz"),
            xKey="x", yKey="y",
            axisx="Frequency [MHz]",
            axisy=axisy,
        ))
        if save_2d_npz:
            plots.append(Plot(
                page=PAGE_RAW, id=f"{name}_waterfall", type="heatmap",
                title=f"{name} Waterfall  (Frequency [MHz] vs LST [hr])",
                filePath=rel("raw_waterfalls", f"{spec_date}_{name}_2d.npz"),
            ))
    plots.append(Plot(
        page=PAGE_RAW, id="avg_temp", type="single",
        title="Average Temperature  [K]",
        filePath=rel("average_temperature", f"{spec_date}_avg_temp.npz"),
        xKey="x", yKey="y",
        axisx="Frequency [MHz]",
        axisy="Temperature [K]",
    ))

    manifest_path = write_manifest(
        output_root=output_root,
        run_dir=run_dir,
        source=source,
        plots=plots,
        dates=dates,
    )
    print(f"[run] Manifest written: {manifest_path}")

    # Write the shared provenance marker so future calls can dedup against
    # this run. The marker is keyed by the parameter hash (or a fallback
    # derived from the timestamps when the caller didn't pass one).
    params_for_hash = {
        "cterms": cterms, "wterms": wterms,
        "ambient_k": ambient_k, "hot_k": hot_k, "lna_k": lna_k,
        "fstart": fstart, "fstop": fstop,
        "wfstart": wfstart, "wfstop": wfstop,
        "save_2d_npz": bool(save_2d_npz),
    }
    h = run_hash or compute_run_hash(dates, params_for_hash)
    marker = config.RUN_HISTORY_DIR / f"{h}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    with open(marker, "w") as f:
        json.dump({
            "hash": h,
            "source": source,
            "run_id": run_dir.name,
            "dates": dates,
            "params": params_for_hash,
            "source_path": str(run_dir),
            "saved_at": datetime.now().isoformat(),
        }, f, indent=2)
    print(f"[run] Provenance marker: {marker}")
    return manifest_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="EDGES-3 single-day analysis")
    p.add_argument("--cal_date", required=True, help="YYYY_DDD calibration date")
    p.add_argument("--s11_date", required=True, help="YYYY_DDD_RUN S11 stem")
    p.add_argument("--spec_date", required=True, help="YYYY_DDD_HH_MM_SS antenna timestamp")
    p.add_argument("--rawdata_root", default=str(config.RAW_DATA_ROOT))
    p.add_argument("--output_root", required=True,
                   help="Display root (e.g. OUTPUT_ROOT/daemon or OUTPUT_ROOT/user)")
    p.add_argument("--run_dir", required=True,
                   help="Full path of the run directory (e.g. OUTPUT_ROOT/daemon/runs/<id>)")
    p.add_argument("--temperature_log", default=str(config.TEMPERATURE_LOG_FILE))
    p.add_argument("--cterms", type=int, default=DEFAULT_CTERMS)
    p.add_argument("--wterms", type=int, default=DEFAULT_WTERMS)
    p.add_argument("--fstart", type=float, default=DEFAULT_FSTART)
    p.add_argument("--fstop", type=float, default=DEFAULT_FSTOP)
    p.add_argument("--wfstart", type=float, default=DEFAULT_WFSTART)
    p.add_argument("--wfstop", type=float, default=DEFAULT_WFSTOP)
    p.add_argument("--save_2d_npz", action="store_true",
                   help="Also save heatmap .npz data (off by default)")
    p.add_argument("--source", default="user",
                   help="Provenance label stored in the manifest (daemon|user)")
    p.add_argument("--run_hash", default=None,
                   help="Optional pre-computed parameter hash (for dedup).")
    args = p.parse_args(argv)

    config.ensure_dirs()
    process_single_day(
        raw_root=Path(args.rawdata_root),
        output_root=Path(args.output_root),
        run_dir=Path(args.run_dir),
        cal_date=args.cal_date,
        s11_date=args.s11_date,
        spec_date=args.spec_date,
        cterms=args.cterms, wterms=args.wterms,
        fstart=args.fstart, fstop=args.fstop,
        wfstart=args.wfstart, wfstop=args.wfstop,
        save_2d_npz=args.save_2d_npz,
        temperature_log=Path(args.temperature_log),
        source=args.source,
        run_hash=args.run_hash,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
