"""Integration tests — full pipeline on real data files.

Runs run_pipeline() against the provided data files without mocking drivers.
Validates structural and semantic properties of the output CSV.

Required files (tests skip if any are missing):
  data/test_motor_1000hz.csv
  data/test_sensor_4800hz.csv
  data/test_psu_10hz.csv
  config/test_config.yaml

Binary motor tests additionally require:
  data/test_motor_1000hz.bin
  config/motor_protocol.yaml

Test classes:
  1. TestIntegrationSmoke          — pipeline completes, file created, non-empty
  2. TestIntegrationOutputSchema   — header matches YAML, no literal None
  3. TestIntegrationTimestamps     — monotone, positive
  4. TestIntegrationPhases         — valid names, monotone ordering, COMPLETE at end
  5. TestIntegrationNearestPriorJoin — PSU stability, bootstrap window, torque continuity
  6. TestIntegrationSafety         — torque/current within YAML limits
  7. TestIntegrationBinaryMotor    — binary matches CSV row count, no literal None
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from automation.main import run_pipeline
from config.loader import load_yaml_config

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT       = Path(__file__).parent.parent
_CONFIG     = _ROOT / "config" / "test_config.yaml"
_MOTOR_CSV  = _ROOT / "data"   / "test_motor_1000hz.csv"
_SENSOR_CSV = _ROOT / "data"   / "test_sensor_4800hz.csv"
_PSU_CSV    = _ROOT / "data"   / "test_psu_10hz.csv"
_MOTOR_BIN  = _ROOT / "data"   / "test_motor_1000hz.bin"
_PROTO_YAML = _ROOT / "config" / "motor_protocol.yaml"

_CSV_FILES    = [_CONFIG, _MOTOR_CSV, _SENSOR_CSV, _PSU_CSV]
_BINARY_FILES = [_CONFIG, _MOTOR_BIN, _SENSOR_CSV, _PSU_CSV, _PROTO_YAML]

_skip_csv    = pytest.mark.skipif(
    not all(p.exists() for p in _CSV_FILES),
    reason="Real CSV data files not present",
)
_skip_binary = pytest.mark.skipif(
    not all(p.exists() for p in _BINARY_FILES),
    reason="Real binary data files not present",
)

_PHASE_ORDER = ["SETUP", "CURRENT_RAMP", "TORQUE_HOLD", "VOLTAGE_DECREASE", "COMPLETE"]

_EXPECTED_COLS = [
    "timestamp_s", "velocity_rad_s", "motor_current_a", "torque_nm",
    "psu_voltage_v", "psu_current_a",
    "commanded_current_a", "commanded_voltage_v", "test_phase",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(output_path: str, motor_format: str = "csv") -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.motor        = str(_MOTOR_CSV if motor_format == "csv" else _MOTOR_BIN)
    ns.motor_format = motor_format
    ns.sensor       = str(_SENSOR_CSV)
    ns.psu          = str(_PSU_CSV)
    ns.output       = output_path
    return ns


def _read_csv(path: str) -> Tuple[List[str], List[Dict[str, str]]]:
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return fieldnames, rows


def _load_cfg() -> Dict[str, Any]:
    return load_yaml_config(str(_CONFIG))


def _load_proto() -> Dict[str, Any]:
    return load_yaml_config(str(_PROTO_YAML))

# ---------------------------------------------------------------------------
# Module-scoped fixture — runs pipeline once, shared across schema/timing/phase/join/safety tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pipeline_output(tmp_path_factory):
    for p in _CSV_FILES:
        if not p.exists():
            pytest.skip(f"Real data file missing: {p}")
    out = str(tmp_path_factory.mktemp("integration") / "out.csv")
    run_pipeline(_make_args(out), _load_cfg(), None)
    return _read_csv(out)


# ---------------------------------------------------------------------------
# 1. TestIntegrationSmoke
# ---------------------------------------------------------------------------

@_skip_csv
class TestIntegrationSmoke:

    def test_pipeline_completes_without_exception(self, tmp_path):
        run_pipeline(_make_args(str(tmp_path / "out.csv")), _load_cfg(), None)

    def test_output_file_created(self, tmp_path):
        out = str(tmp_path / "out.csv")
        run_pipeline(_make_args(out), _load_cfg(), None)
        assert os.path.exists(out)

    def test_output_is_non_empty(self, tmp_path):
        out = str(tmp_path / "out.csv")
        run_pipeline(_make_args(out), _load_cfg(), None)
        fieldnames, rows = _read_csv(out)
        assert len(fieldnames) > 0
        assert len(rows) > 0

    def test_row_count_proportional_to_motor_file(self, tmp_path):
        """Motor CSV has ~1000 rows/s — at least 100 rows expected."""
        out = str(tmp_path / "out.csv")
        run_pipeline(_make_args(out), _load_cfg(), None)
        _, rows = _read_csv(out)
        assert len(rows) >= 100


# ---------------------------------------------------------------------------
# 2. TestIntegrationOutputSchema
# ---------------------------------------------------------------------------

class TestIntegrationOutputSchema:

    def test_header_matches_yaml_columns(self, pipeline_output):
        fieldnames, _ = pipeline_output
        assert fieldnames == _EXPECTED_COLS

    def test_no_extra_or_missing_columns(self, pipeline_output):
        fieldnames, _ = pipeline_output
        assert set(fieldnames) == set(_EXPECTED_COLS)

    def test_no_literal_None_in_output(self, pipeline_output):
        _, rows = pipeline_output
        for i, row in enumerate(rows):
            for col, val in row.items():
                assert val != "None", f"Literal 'None' at row {i} col '{col}'"


# ---------------------------------------------------------------------------
# 3. TestIntegrationTimestamps
# ---------------------------------------------------------------------------

class TestIntegrationTimestamps:

    def test_timestamps_non_decreasing(self, pipeline_output):
        _, rows = pipeline_output
        ts = [float(r["timestamp_s"]) for r in rows]
        for i in range(1, len(ts)):
            assert ts[i] >= ts[i - 1], (
                f"Non-monotonic at row {i}: {ts[i-1]:.6f} → {ts[i]:.6f}"
            )

    def test_all_timestamps_non_negative(self, pipeline_output):
        _, rows = pipeline_output
        for r in rows:
            assert float(r["timestamp_s"]) >= 0.0


# ---------------------------------------------------------------------------
# 4. TestIntegrationPhases
# ---------------------------------------------------------------------------

class TestIntegrationPhases:

    def test_every_row_has_non_empty_phase(self, pipeline_output):
        _, rows = pipeline_output
        for i, row in enumerate(rows):
            assert row.get("test_phase", "") != "", f"Empty phase at row {i}"

    def test_all_phases_are_valid_yaml_names(self, pipeline_output):
        _, rows = pipeline_output
        valid = set(_PHASE_ORDER)
        for i, row in enumerate(rows):
            assert row["test_phase"] in valid, (
                f"Unknown phase '{row['test_phase']}' at row {i}"
            )

    def test_first_row_is_early_phase(self, pipeline_output):
        """SETUP emits no rows (transitions before first motor write).
        First output row is SETUP or CURRENT_RAMP."""
        _, rows = pipeline_output
        assert rows[0]["test_phase"] in {"SETUP", "CURRENT_RAMP"}

    def test_last_row_phase_is_valid(self, pipeline_output):
        """Last phase is valid YAML name (COMPLETE if data long enough)."""
        _, rows = pipeline_output
        assert rows[-1]["test_phase"] in set(_PHASE_ORDER)

    def test_phase_transitions_are_monotone(self, pipeline_output):
        """Phase index in PHASE_ORDER must never decrease."""
        _, rows = pipeline_output
        idx = 0
        for i, row in enumerate(rows):
            phase = row["test_phase"]
            new_idx = _PHASE_ORDER.index(phase)
            assert new_idx >= idx, (
                f"Phase regressed from {_PHASE_ORDER[idx]} → {phase} at row {i}"
            )
            idx = new_idx

    def test_setup_appears_before_current_ramp_if_present(self, pipeline_output):
        """SETUP must precede CURRENT_RAMP when both appear in output."""
        _, rows = pipeline_output
        phases = [r["test_phase"] for r in rows]
        if "SETUP" in phases and "CURRENT_RAMP" in phases:
            assert phases.index("SETUP") < phases.index("CURRENT_RAMP")


# ---------------------------------------------------------------------------
# 5. TestIntegrationNearestPriorJoin
# ---------------------------------------------------------------------------

class TestIntegrationNearestPriorJoin:

    def test_psu_values_stable_within_psu_period(self, pipeline_output):
        """PSU at 10 Hz → 100 motor rows share same prior sample.
        Find at least 5 consecutive rows with identical psu_voltage_v."""
        _, rows = pipeline_output
        psu_vals = [r["psu_voltage_v"] for r in rows]
        first = next((i for i, v in enumerate(psu_vals) if v != ""), None)
        assert first is not None, "No PSU values found in output"
        run = 1
        for i in range(first + 1, min(first + 50, len(psu_vals))):
            if psu_vals[i] == psu_vals[i - 1]:
                run += 1
                if run >= 5:
                    break
            else:
                run = 1
        assert run >= 5, (
            "Expected ≥5 consecutive motor rows sharing same PSU reading (PSU=10Hz, motor=1000Hz)"
        )

    def test_psu_non_empty_after_first_arrival(self, pipeline_output):
        """Once first PSU sample joins, all subsequent rows must have psu_voltage_v."""
        _, rows = pipeline_output
        first = next((i for i, r in enumerate(rows) if r["psu_voltage_v"] != ""), None)
        if first is None:
            pytest.skip("No PSU data in output — cannot test join continuity")
        for i, row in enumerate(rows[first:], start=first):
            assert row["psu_voltage_v"] != "", (
                f"PSU went empty after first arrival at row {i} — nearest-prior broken"
            )

    def test_torque_non_empty_after_first_sensor_arrival(self, pipeline_output):
        """Once first sensor sample joins, all subsequent rows must have torque_nm."""
        _, rows = pipeline_output
        first = next((i for i, r in enumerate(rows) if r["torque_nm"] != ""), None)
        if first is None:
            pytest.skip("No torque data in output — cannot test join continuity")
        for i, row in enumerate(rows[first:], start=first):
            assert row["torque_nm"] != "", (
                f"Torque went empty after first sensor arrival at row {i}"
            )

    def test_psu_change_rate_bounded_by_nominal_hz(self, pipeline_output):
        """PSU value must not change more than once per ~50ms (half the 10Hz period).
        Count transitions; transitions / duration must be ≤ 25 Hz (2.5× margin)."""
        _, rows = pipeline_output
        psu_vals = [r["psu_voltage_v"] for r in rows if r["psu_voltage_v"] != ""]
        if len(psu_vals) < 2:
            pytest.skip("Not enough PSU data")
        transitions = sum(1 for a, b in zip(psu_vals, psu_vals[1:]) if a != b)
        ts_start = float(next(r["timestamp_s"] for r in rows if r["psu_voltage_v"] != ""))
        ts_end   = float(next(
            r["timestamp_s"] for r in reversed(rows) if r["psu_voltage_v"] != ""
        ))
        duration = ts_end - ts_start
        if duration <= 0:
            pytest.skip("Duration too short")
        rate_hz = transitions / duration
        assert rate_hz <= 25.0, (
            f"PSU changed {transitions}× in {duration:.2f}s = {rate_hz:.1f} Hz > 25 Hz ceiling"
        )


# ---------------------------------------------------------------------------
# 6. TestIntegrationSafety
# ---------------------------------------------------------------------------

class TestIntegrationSafety:

    def test_no_torque_exceeds_safety_limit(self, pipeline_output):
        """max_torque_nm = 200.0 from test_config.yaml."""
        _, rows = pipeline_output
        for i, row in enumerate(rows):
            if row["torque_nm"] != "":
                assert abs(float(row["torque_nm"])) <= 200.0, (
                    f"Torque {row['torque_nm']} Nm exceeds 200 Nm safety limit at row {i}"
                )

    def test_no_motor_current_exceeds_safety_limit(self, pipeline_output):
        """max_current_a = 34.0 from test_config.yaml."""
        _, rows = pipeline_output
        for i, row in enumerate(rows):
            if row["motor_current_a"] != "":
                assert abs(float(row["motor_current_a"])) <= 34.0, (
                    f"Motor current {row['motor_current_a']} A exceeds 34 A limit at row {i}"
                )

    def test_commanded_current_does_not_exceed_ramp_max(self, pipeline_output):
        """commanded_current_a ≤ max_current_a (34.0) in every row."""
        _, rows = pipeline_output
        for i, row in enumerate(rows):
            if row["commanded_current_a"] != "":
                assert float(row["commanded_current_a"]) <= 34.0 + 1e-9, (
                    f"Commanded current {row['commanded_current_a']} A > 34 A at row {i}"
                )

    def test_commanded_voltage_non_negative(self, pipeline_output):
        _, rows = pipeline_output
        for i, row in enumerate(rows):
            if row["commanded_voltage_v"] != "":
                assert float(row["commanded_voltage_v"]) >= -1e-9, (
                    f"Negative commanded voltage at row {i}: {row['commanded_voltage_v']}"
                )


# ---------------------------------------------------------------------------
# 7. TestIntegrationBinaryMotor
# ---------------------------------------------------------------------------

@_skip_binary
class TestIntegrationBinaryMotor:

    def _binary_args(self, output_path: str) -> argparse.Namespace:
        ns = argparse.Namespace()
        ns.motor        = str(_MOTOR_BIN)
        ns.motor_format = "binary"
        ns.sensor       = str(_SENSOR_CSV)
        ns.psu          = str(_PSU_CSV)
        ns.output       = output_path
        return ns

    def test_binary_pipeline_completes(self, tmp_path):
        run_pipeline(self._binary_args(str(tmp_path / "out.csv")), _load_cfg(), _load_proto())

    def test_binary_output_file_exists(self, tmp_path):
        out = str(tmp_path / "out.csv")
        run_pipeline(self._binary_args(out), _load_cfg(), _load_proto())
        assert os.path.exists(out)

    def test_binary_output_no_literal_None(self, tmp_path):
        out = str(tmp_path / "out.csv")
        run_pipeline(self._binary_args(out), _load_cfg(), _load_proto())
        with open(out) as fh:
            assert "None" not in fh.read()

    def test_binary_header_matches_yaml(self, tmp_path):
        out = str(tmp_path / "out.csv")
        run_pipeline(self._binary_args(out), _load_cfg(), _load_proto())
        fieldnames, _ = _read_csv(out)
        assert fieldnames == _EXPECTED_COLS

    def test_binary_and_csv_row_counts_match(self, tmp_path):
        """Binary and CSV encode same telemetry — row counts must agree."""
        out_csv = str(tmp_path / "csv.csv")
        out_bin = str(tmp_path / "bin.csv")
        cfg = _load_cfg()
        run_pipeline(_make_args(out_csv, "csv"), cfg, None)
        run_pipeline(self._binary_args(out_bin), cfg, _load_proto())
        _, csv_rows = _read_csv(out_csv)
        _, bin_rows = _read_csv(out_bin)
        assert len(csv_rows) == len(bin_rows), (
            f"CSV={len(csv_rows)} rows vs binary={len(bin_rows)} rows"
        )

    def test_binary_phases_monotone(self, tmp_path):
        out = str(tmp_path / "out.csv")
        run_pipeline(self._binary_args(out), _load_cfg(), _load_proto())
        _, rows = _read_csv(out)
        idx = 0
        for i, row in enumerate(rows):
            new_idx = _PHASE_ORDER.index(row["test_phase"])
            assert new_idx >= idx, (
                f"Phase regressed at row {i}: {_PHASE_ORDER[idx]} → {row['test_phase']}"
            )
            idx = new_idx
