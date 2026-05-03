"""Motor Characterization Bench — CLI orchestrator.

Wires all pipeline components end-to-end:
  Motor driver → Synchronizer → SchemaProjector → StateMachine → Logger

All paths are supplied via CLI flags; no path is hardcoded.
All thresholds and column layouts flow from YAML; no constants are embedded here.
Memory footprint is O(1): one row processed at a time through the generator chain.
"""
from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from config.consts import _UNIT_SUFFIX_MAP
from config.loader import ConfigurationError, load_yaml_config
from automation.logger import FIELD_RENAME, Logger
from automation.row_schema import SchemaProjector
from automation.state_machine import StateMachine
from automation.synchronizer import Synchronizer
from drivers.motor import MotorBinaryReader, MotorCSVReader
from drivers.psu import PSUCSVReader
from drivers.sensor import SensorCSVReader

_log = logging.getLogger(__name__)

PROGRESS_EVERY = 10_000


def _cfg_get(cfg: Dict[str, Any], *path: str) -> Any:
    """Walk nested dicts; raise KeyError with full dotted path on miss."""
    d = cfg
    dotted = ".".join(path)
    for k in path:
        if not isinstance(d, dict) or k not in d:
            raise KeyError(f"Missing required config key: {dotted}")
        d = d[k]
    return d


# ---------------------------------------------------------------------------
# YAML key derivation
# ---------------------------------------------------------------------------

def _derive_expected_keys(
    test_cfg: Dict[str, Any],
    motor_proto: Optional[Dict[str, Any]],
    motor_format: str,
) -> Tuple[str, ...]:
    """Return union of all column names across sensor, PSU, and motor streams.

    Derived purely from YAML — no column name is hardcoded here.
    """
    sensor_cols = [
        c["name"]
        for c in test_cfg["data_sources"]["sensor"]["formats"]["csv"]["columns"]
    ]
    psu_cols = [
        c["name"]
        for c in test_cfg["data_sources"]["power_supply"]["formats"]["csv"]["columns"]
    ]

    if motor_format == "csv":
        motor_cols = [
            c["name"]
            for c in test_cfg["data_sources"]["motor"]["formats"]["csv"]["columns"]
        ]
    else:
        # Binary: derive output keys the same way MotorBinaryReader does.
        motor_cols = []
        for resp in (motor_proto or {}).get("responses", []):
            for f in resp["fields"]:
                motor_cols.append(f["name"] + _UNIT_SUFFIX_MAP.get(f.get("unit", ""), ""))

    return tuple(set(sensor_cols) | set(psu_cols) | set(motor_cols))


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    args: argparse.Namespace,
    test_cfg: Dict[str, Any],
    motor_proto: Optional[Dict[str, Any]],
    *,
    status_queue: Optional[queue.Queue] = None,
    abort_event: Optional[threading.Event] = None,
    speed_multiplier: float = 0.0,
) -> None:
    """Instantiate components and run the row loop.  Owns no file handles directly
    — ExitStack manages the output file and Logger context manager."""

    motor_format: str = args.motor_format

    # Build drivers.
    if motor_format == "binary":
        motor_driver = MotorBinaryReader(args.motor, motor_proto)
    else:
        motor_driver = MotorCSVReader(args.motor, test_cfg)

    sensor_driver = SensorCSVReader(args.sensor, test_cfg)
    psu_driver = PSUCSVReader(args.psu, test_cfg)

    synchronizer = Synchronizer(motor_driver, sensor_driver, psu_driver)
    expected_keys = _derive_expected_keys(test_cfg, motor_proto, motor_format)
    projector = SchemaProjector(synchronizer, expected_keys)
    sm = StateMachine(test_cfg)

    driver_label = "MotorBinaryReader" if motor_format == "binary" else "MotorCSVReader"
    _log.info(
        "Pipeline starting | motor_format=%s | output=%s", motor_format, args.output
    )
    _log.info(
        "Wired: %s → Synchronizer → SchemaProjector → StateMachine → Logger",
        driver_label,
    )

    gui_rate_hz = (test_cfg.get("monitoring") or {}).get("gui_update_rate_hz", 20)
    snapshot_period_s = 1.0 / max(1, gui_rate_hz)

    with ExitStack() as stack:
        out_fh = stack.enter_context(open(args.output, "w", newline=""))
        log = stack.enter_context(Logger(test_cfg, out_fh))

        rows = 0
        last_snap_wall = 0.0
        first_motor_t: Optional[float] = None
        wall_start = time.perf_counter()
        # Track time spent in pacing sleeps so throughput reflects parsing work,
        # not 1×-playback wall clock.  At Max (speed_multiplier=0.0) this stays 0.
        total_sleep_s = 0.0

        for raw in projector:
            if abort_event is not None and abort_event.is_set():
                sm.request_abort("manual_abort_gui")

            augmented = sm.process(raw)
            log.write(augmented)
            rows += 1

            # Speed pacing — real-time throttle; skip when speed_multiplier == 0.0 (Max).
            if speed_multiplier and speed_multiplier > 0.0:
                t_data = augmented.get("timestamp_s")
                if t_data is not None:
                    if first_motor_t is None:
                        first_motor_t = t_data
                    target_wall = (t_data - first_motor_t) / speed_multiplier
                    slack = target_wall - (time.perf_counter() - wall_start)
                    if slack > 0.0:
                        time.sleep(slack)
                        total_sleep_s += slack

            # Throttled snapshot — at most gui_update_rate_hz sends per second.
            if status_queue is not None:
                now = time.perf_counter()
                if now - last_snap_wall >= snapshot_period_s:
                    last_snap_wall = now
                    snap = {FIELD_RENAME.get(k, k): v for k, v in augmented.items()}
                    snap["rows"] = rows
                    snap["abort_reason"] = sm.get_stats().get("abort_reason")
                    try:
                        status_queue.put_nowait(snap)
                    except queue.Full:
                        pass  # bounded queue; drop oldest implicitly via GUI drain

            if rows % PROGRESS_EVERY == 0:
                _log.info(
                    "Progress | rows=%d | phase=%s | t=%.3f s",
                    rows,
                    sm.current_phase,
                    augmented["timestamp_s"],
                )

            if sm.is_complete:
                break

    elapsed_wall_s = time.perf_counter() - wall_start
    processing_s = max(elapsed_wall_s - total_sleep_s, 0.0)
    throughput_rows_per_s: Optional[float] = (
        rows / processing_s if processing_s > 0.0 else None
    )

    if status_queue is not None:
        sentinel: Dict[str, Any] = {
            "_done": True,
            "rows": log.rows_written,
            "stats": sm.get_stats(),
            "motor_stats": motor_driver.stats,
            "sensor_stats": sensor_driver.stats,
            "psu_stats": psu_driver.stats,
            "efficiency_mean": log.efficiency_mean,
            "efficiency_peak": log.efficiency_peak,
            "elapsed_wall_s": elapsed_wall_s,
            "processing_s": processing_s,
            "throughput_rows_per_s": throughput_rows_per_s,
        }
        try:
            status_queue.put_nowait(sentinel)
        except queue.Full:
            # Sentinel must reach the GUI — drain one stale snap and retry.
            try:
                status_queue.get_nowait()
                status_queue.put_nowait(sentinel)
            except (queue.Empty, queue.Full):
                pass

    sm_stats = sm.get_stats()
    sync_stats = synchronizer.get_stats()

    _log.info(
        "Pipeline complete | rows_written=%d | end_phase=%s | abort_reason=%s",
        log.rows_written,
        sm_stats.get("end_phase"),
        sm_stats.get("abort_reason"),
    )
    if throughput_rows_per_s is not None:
        _log.info(
            "Throughput   | %.0f rows/s (processing %.3f s, wall %.3f s)",
            throughput_rows_per_s, processing_s, elapsed_wall_s,
        )
    _log.info("Sync stats    | %s", sync_stats)
    _log.info("StateMachine  | %s", sm_stats)
    if motor_format == "binary":
        _log.info("Motor (binary)| %s", motor_driver.stats)
    else:
        _log.info("Motor (csv)   | %s", motor_driver.stats)
    _log.info("Sensor        | %s", sensor_driver.stats)
    _log.info("PSU           | %s", psu_driver.stats)


# ---------------------------------------------------------------------------
# Argument parsing + validation
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m automation.main",
        description="Motor Characterization Bench — end-to-end pipeline runner.",
    )
    p.add_argument("--config", required=True, metavar="PATH",
                   help="Path to test_config.yaml")
    p.add_argument("--motor-protocol", metavar="PATH",
                   help="Path to motor_protocol.yaml (required for --motor-format=binary)")
    p.add_argument("--motor", required=True, metavar="PATH",
                   help="Motor telemetry input file (.bin or .csv)")
    p.add_argument(
        "--motor-format", choices=["binary", "csv"],
        help="Override auto-detection of motor file format (default: infer from extension)",
    )
    p.add_argument("--sensor", required=True, metavar="PATH",
                   help="Sensor CSV input (4800 Hz)")
    p.add_argument("--psu", required=True, metavar="PATH",
                   help="PSU CSV input (10 Hz)")
    p.add_argument("--output", required=True, metavar="PATH",
                   help="Merged output CSV destination")
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help=(
            "Stdlib logging level (default: INFO). "
            "WARNING: DEBUG enables per-row logging inside StateMachine and Logger. "
            "At full motor rate (1000 Hz) this produces millions of log lines and "
            "dominates wall time — use only on small test inputs."
        ),
    )
    return p


def _resolve_motor_format(args: argparse.Namespace) -> str:
    """Return 'binary' or 'csv'; raise SystemExit on unknown extension."""
    if args.motor_format:
        return args.motor_format
    suffix = Path(args.motor).suffix.lower()
    if suffix == ".bin":
        return "binary"
    if suffix == ".csv":
        return "csv"
    _build_parser().error(
        f"Cannot infer motor format from extension '{suffix}'. "
        "Use --motor-format binary|csv explicitly."
    )


def _validate_inputs(args: argparse.Namespace) -> None:
    """Fail fast before opening any output file.  Checks existence of all inputs."""
    for flag, path_str in [
        ("--config", args.config),
        ("--motor", args.motor),
        ("--sensor", args.sensor),
        ("--psu", args.psu),
    ]:
        if not Path(path_str).exists():
            _build_parser().error(f"{flag} path does not exist: {path_str}")

    if args.motor_format == "binary":
        if not args.motor_protocol:
            _build_parser().error("--motor-protocol is required when --motor-format=binary")
        if not Path(args.motor_protocol).exists():
            _build_parser().error(
                f"--motor-protocol path does not exist: {args.motor_protocol}"
            )

    out_parent = Path(args.output).parent
    if not out_parent.exists():
        _build_parser().error(
            f"Output parent directory does not exist: {out_parent}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Step 1: resolve motor format before opening anything.
    args.motor_format = _resolve_motor_format(args)

    # Step 2: validate all inputs (fail fast; no output file opened yet).
    _validate_inputs(args)

    # Step 3: configure logging.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    # Step 4: load YAML configs.
    try:
        test_cfg = load_yaml_config(args.config)
    except ConfigurationError as exc:
        logging.error("Failed to load test config: %s", exc)
        sys.exit(1)

    motor_proto: Optional[Dict[str, Any]] = None
    if args.motor_format == "binary":
        try:
            motor_proto = load_yaml_config(args.motor_protocol)
        except ConfigurationError as exc:
            logging.error("Failed to load motor protocol: %s", exc)
            sys.exit(1)

    # Step 5: run pipeline.
    try:
        run_pipeline(args, test_cfg, motor_proto)
    except KeyboardInterrupt:
        logging.warning("Interrupted by user — output flushed up to last written row")
        sys.exit(130)
    except Exception:
        logging.exception("Pipeline failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
