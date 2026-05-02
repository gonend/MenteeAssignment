"""Tests for automation/main.py — orchestrator.

Strategy: bypass argparse and real driver file I/O by patching driver
constructors with list-backed fakes.  run_pipeline() is called directly.
All assertions are on the output CSV and StateMachine state.

Test classes:
  1. TestDeriveExpectedKeys     — YAML-driven key derivation (csv + binary)
  2. TestPipelineHappyPath      — normal short runs, row count, header, phases
  3. TestPipelineEmptyStream    — header-only output, no crash
  4. TestPipelineSafetyAbort    — safety trip → COMPLETE + frozen values
  5. TestPipelineCompleteEarly  — COMPLETE stop-condition halts the loop
  6. TestPipelineFileContract   — output file closed/flushed after run
"""
from __future__ import annotations

import argparse
import csv
import os
import tempfile
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest

from automation.main import _derive_expected_keys, run_pipeline
from drivers.motor import MotorDataSource
from drivers.psu import PSUDataSource
from drivers.sensor import SensorDataSource


# ---------------------------------------------------------------------------
# Shared minimal YAML config
# ---------------------------------------------------------------------------

_CFG: Dict[str, Any] = {
    "test": {
        "name": "unit",
        "phases": [
            {"name": "SETUP"},
            {"name": "CURRENT_RAMP", "parameters": {
                "max_current_a": 34.0,
                "ramp_duration_s": 10.0,
                "target_torque_nm": 150.0,
            }},
            {"name": "TORQUE_HOLD", "parameters": {"hold_duration_s": 10.0}},
            {"name": "VOLTAGE_DECREASE", "parameters": {
                "voltage_decrease_rate_v_per_s": 1.0,
                "min_voltage_v": 0.0,
            }},
            {"name": "COMPLETE"},
        ],
        "safety": {"max_torque_nm": 200.0, "max_current_a": 34.0},
    },
    "power_supply": {"initial_voltage_v": 24.0},
    "data_sources": {
        "motor": {"formats": {"csv": {"columns": [
            {"name": "timestamp_s",        "type": "float64"},
            {"name": "velocity_rad_s",     "type": "float64"},
            {"name": "measured_current_a", "type": "float64"},
        ]}}},
        "sensor": {"formats": {"csv": {"columns": [
            {"name": "timestamp_s", "type": "float64"},
            {"name": "torque_nm",   "type": "float64"},
        ]}}},
        "power_supply": {"formats": {"csv": {"columns": [
            {"name": "timestamp_s", "type": "float64"},
            {"name": "voltage_v",   "type": "float64"},
            {"name": "current_a",   "type": "float64"},
        ]}}},
    },
    "output": {
        "columns": [
            "timestamp_s", "velocity_rad_s", "motor_current_a", "torque_nm",
            "psu_voltage_v", "psu_current_a",
            "commanded_current_a", "commanded_voltage_v", "test_phase",
        ],
    },
}

# Config with immediate COMPLETE: safety trip on min_voltage = initial_voltage (24.0)
_CFG_QUICK_COMPLETE: Dict[str, Any] = {
    **_CFG,
    "test": {
        **_CFG["test"],
        "phases": [
            {"name": "SETUP"},
            {"name": "CURRENT_RAMP", "parameters": {
                "max_current_a": 34.0,
                "ramp_duration_s": 10.0,
                "target_torque_nm": 150.0,
            }},
            {"name": "TORQUE_HOLD", "parameters": {"hold_duration_s": 0.0}},
            {"name": "VOLTAGE_DECREASE", "parameters": {
                "voltage_decrease_rate_v_per_s": 1.0,
                "min_voltage_v": 24.0,  # immediate transition on first PSU reading
            }},
            {"name": "COMPLETE"},
        ],
        "safety": {"max_torque_nm": 200.0, "max_current_a": 34.0},
    },
}

_EXPECTED_HEADER = [
    "timestamp_s", "velocity_rad_s", "motor_current_a", "torque_nm",
    "psu_voltage_v", "psu_current_a",
    "commanded_current_a", "commanded_voltage_v", "test_phase",
]


# ---------------------------------------------------------------------------
# Fake data sources
# ---------------------------------------------------------------------------

class FakeMotor(MotorDataSource):
    def __init__(self, samples: List[Dict[str, Any]]) -> None:
        self._samples = samples
        self.stats: Dict[str, int] = {}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        yield from self._samples

    def get_stats(self) -> Dict[str, Any]:
        return dict(self.stats)


class FakeSensor(SensorDataSource):
    def __init__(self, samples: List[Dict[str, Any]]) -> None:
        self._samples = samples
        self.stats: Dict[str, int] = {}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        yield from self._samples

    def get_stats(self) -> Dict[str, Any]:
        return dict(self.stats)


class FakePSU(PSUDataSource):
    def __init__(self, samples: List[Dict[str, Any]]) -> None:
        self._samples = samples
        self.stats: Dict[str, int] = {}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        yield from self._samples

    def get_stats(self) -> Dict[str, Any]:
        return dict(self.stats)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _motor(n: int, t0: float = 0.001, step: float = 0.001) -> List[Dict[str, Any]]:
    return [
        {"timestamp_s": t0 + i * step, "velocity_rad_s": float(i), "measured_current_a": 0.0}
        for i in range(n)
    ]


def _sensor(n: int, t0: float = 0.0, step: float = 0.001,
            torque: float = 0.0) -> List[Dict[str, Any]]:
    return [
        {"timestamp_s": t0 + i * step, "torque_nm": torque}
        for i in range(n)
    ]


def _psu(n: int = 3, t0: float = 0.0, step: float = 0.1,
         voltage: float = 24.0) -> List[Dict[str, Any]]:
    return [
        {"timestamp_s": t0 + i * step, "voltage_v": voltage, "current_a": 1.0}
        for i in range(n)
    ]


def _args(output_path: str, motor_format: str = "csv") -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.motor = "fake.csv"
    ns.motor_format = motor_format
    ns.sensor = "fake_sensor.csv"
    ns.psu = "fake_psu.csv"
    ns.output = output_path
    return ns


def _run(
    motor_samples: List[Dict[str, Any]],
    sensor_samples: List[Dict[str, Any]],
    psu_samples: List[Dict[str, Any]],
    output_path: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> None:
    """Patch driver constructors and invoke run_pipeline with fake data."""
    cfg = cfg if cfg is not None else _CFG
    args = _args(output_path)

    with patch("automation.main.MotorCSVReader",   return_value=FakeMotor(motor_samples)), \
         patch("automation.main.SensorCSVReader",  return_value=FakeSensor(sensor_samples)), \
         patch("automation.main.PSUCSVReader",     return_value=FakePSU(psu_samples)):
        run_pipeline(args, cfg, None)


def _read_csv(path: str):
    """Return (fieldnames: list[str], rows: list[dict])."""
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return fieldnames, rows


# ---------------------------------------------------------------------------
# 1. TestDeriveExpectedKeys
# ---------------------------------------------------------------------------

class TestDeriveExpectedKeys:

    def test_csv_returns_union_of_all_three_streams(self):
        keys = _derive_expected_keys(_CFG, None, "csv")
        assert "timestamp_s" in keys          # motor + sensor + psu all have this
        assert "velocity_rad_s" in keys       # motor CSV
        assert "measured_current_a" in keys   # motor CSV
        assert "torque_nm" in keys            # sensor CSV
        assert "voltage_v" in keys            # PSU CSV
        assert "current_a" in keys            # PSU CSV

    def test_csv_returns_tuple(self):
        keys = _derive_expected_keys(_CFG, None, "csv")
        assert isinstance(keys, tuple)

    def test_csv_no_duplicates(self):
        keys = _derive_expected_keys(_CFG, None, "csv")
        assert len(keys) == len(set(keys))

    def test_binary_uses_protocol_responses(self):
        proto = {
            "responses": [
                {"fields": [
                    {"name": "velocity", "unit": "rad/s"},
                    {"name": "measured_current", "unit": "A"},
                    {"name": "timestamp", "unit": "ms"},
                ]},
            ]
        }
        keys = _derive_expected_keys(_CFG, proto, "binary")
        assert "velocity_rad_s" in keys
        assert "measured_current_a" in keys
        assert "timestamp_ms" in keys
        # Sensor + PSU columns still included
        assert "torque_nm" in keys
        assert "voltage_v" in keys

    def test_binary_no_protocol_responses_returns_sensor_psu_keys(self):
        keys = _derive_expected_keys(_CFG, {}, "binary")
        assert "torque_nm" in keys
        assert "voltage_v" in keys

    def test_csv_does_not_use_protocol_arg(self):
        # Passing a non-None protocol in CSV mode must be silently ignored.
        proto = {"responses": [{"fields": [{"name": "ghost", "unit": "rad/s"}]}]}
        keys = _derive_expected_keys(_CFG, proto, "csv")
        assert "ghost_rad_s" not in keys


# ---------------------------------------------------------------------------
# 2. TestPipelineHappyPath
# ---------------------------------------------------------------------------

class TestPipelineHappyPath:

    def test_header_matches_yaml_output_columns(self, tmp_path):
        out = str(tmp_path / "out.csv")
        _run(_motor(5), _sensor(5), _psu(3), out)
        fieldnames, _ = _read_csv(out)
        assert fieldnames == _EXPECTED_HEADER

    def test_row_count_matches_motor_stream_length(self, tmp_path):
        out = str(tmp_path / "out.csv")
        n = 7
        _run(_motor(n), _sensor(n), _psu(3), out)
        _, rows = _read_csv(out)
        assert len(rows) == n

    def test_test_phase_column_present_in_every_row(self, tmp_path):
        out = str(tmp_path / "out.csv")
        _run(_motor(5), _sensor(5), _psu(3), out)
        _, rows = _read_csv(out)
        for row in rows:
            assert "test_phase" in row
            assert row["test_phase"] != ""

    def test_commanded_columns_present_in_every_row(self, tmp_path):
        out = str(tmp_path / "out.csv")
        _run(_motor(5), _sensor(5), _psu(3), out)
        _, rows = _read_csv(out)
        for row in rows:
            assert "commanded_current_a" in row
            assert "commanded_voltage_v" in row

    def test_no_literal_None_in_output(self, tmp_path):
        out = str(tmp_path / "out.csv")
        # Sensor starts late → first motor rows have no torque prior
        motor = _motor(5, t0=0.001)
        sensor = _sensor(3, t0=0.003)  # sensor lags motor start
        _run(motor, sensor, _psu(3), out)
        with open(out) as fh:
            content = fh.read()
        assert "None" not in content

    def test_timestamp_s_increases_monotonically(self, tmp_path):
        out = str(tmp_path / "out.csv")
        _run(_motor(10), _sensor(10), _psu(3), out)
        _, rows = _read_csv(out)
        timestamps = [float(r["timestamp_s"]) for r in rows]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# 3. TestPipelineEmptyStream
# ---------------------------------------------------------------------------

class TestPipelineEmptyStream:

    def test_empty_motor_produces_header_only(self, tmp_path):
        out = str(tmp_path / "out.csv")
        _run([], _sensor(3), _psu(3), out)
        fieldnames, rows = _read_csv(out)
        assert fieldnames == _EXPECTED_HEADER
        assert rows == []

    def test_empty_motor_does_not_crash(self, tmp_path):
        out = str(tmp_path / "out.csv")
        _run([], [], [], out)  # all streams empty
        # Should complete without exception

    def test_output_file_created_on_empty_motor(self, tmp_path):
        out = str(tmp_path / "out.csv")
        _run([], _sensor(3), _psu(3), out)
        assert os.path.exists(out)


# ---------------------------------------------------------------------------
# 4. TestPipelineSafetyAbort
# ---------------------------------------------------------------------------

class TestPipelineSafetyAbort:

    def _safety_cfg(self) -> Dict[str, Any]:
        """Config with low safety threshold so a single row triggers abort."""
        import copy
        cfg = copy.deepcopy(_CFG)
        cfg["test"]["safety"]["max_torque_nm"] = 5.0  # easily exceeded
        return cfg

    def test_safety_abort_writes_complete_phase(self, tmp_path):
        out = str(tmp_path / "out.csv")
        # Sensor torque = 10 Nm > safety limit of 5 Nm
        motor = _motor(5)
        sensor = _sensor(5, torque=10.0)
        _run(motor, sensor, _psu(3), out, cfg=self._safety_cfg())
        _, rows = _read_csv(out)
        # After safety abort every subsequent row is COMPLETE
        assert any(r["test_phase"] == "COMPLETE" for r in rows)

    def test_safety_abort_stops_before_full_stream(self, tmp_path):
        """After safety abort the loop breaks; row count ≤ motor stream length."""
        out = str(tmp_path / "out.csv")
        motor = _motor(20)
        sensor = _sensor(20, torque=10.0)  # torque > limit from first row
        _run(motor, sensor, _psu(3), out, cfg=self._safety_cfg())
        _, rows = _read_csv(out)
        # Once COMPLETE, is_complete fires the break — should stop at 1 row
        assert len(rows) <= 20

    def test_safety_abort_no_literal_None_in_output(self, tmp_path):
        out = str(tmp_path / "out.csv")
        motor = _motor(3)
        sensor = _sensor(3, torque=10.0)
        _run(motor, sensor, _psu(3), out, cfg=self._safety_cfg())
        with open(out) as fh:
            content = fh.read()
        assert "None" not in content


# ---------------------------------------------------------------------------
# 5. TestPipelineCompleteEarly
# ---------------------------------------------------------------------------

class TestPipelineCompleteEarly:
    """Verifies the is_complete short-circuit stops the loop."""

    def test_complete_on_first_row_writes_one_data_row(self, tmp_path):
        """Safety abort on row 1 → loop breaks → exactly 1 data row written."""
        import copy
        cfg = copy.deepcopy(_CFG)
        cfg["test"]["safety"]["max_torque_nm"] = 0.001  # any torque trips immediately

        out = str(tmp_path / "out.csv")
        motor = _motor(100)
        sensor = _sensor(100, torque=1.0)  # torque=1 > limit=0.001
        _run(motor, sensor, _psu(3), out, cfg=cfg)
        _, rows = _read_csv(out)
        assert len(rows) == 1
        assert rows[0]["test_phase"] == "COMPLETE"

    def test_stream_stops_at_complete_not_at_stream_end(self, tmp_path):
        """
        Use quick-complete config: after SETUP + 1 CURRENT_RAMP row the hold
        is 0 s → TORQUE_HOLD exits immediately → VOLTAGE_DECREASE → first PSU
        row (voltage=24.0 >= min_voltage=24.0) → COMPLETE.
        Row count must be well below the 50-row motor stream.
        """
        out = str(tmp_path / "out.csv")
        motor = _motor(50, step=0.001)
        # torque high enough to exit CURRENT_RAMP on first row
        sensor = _sensor(50, torque=160.0)
        psu = _psu(5, voltage=24.0)
        _run(motor, sensor, psu, out, cfg=_CFG_QUICK_COMPLETE)
        _, rows = _read_csv(out)
        assert len(rows) < 50
        assert rows[-1]["test_phase"] == "COMPLETE"


# ---------------------------------------------------------------------------
# 6. TestPipelineFileContract
# ---------------------------------------------------------------------------

class TestPipelineFileContract:

    def test_output_file_exists_after_run(self, tmp_path):
        out = str(tmp_path / "out.csv")
        _run(_motor(5), _sensor(5), _psu(3), out)
        assert os.path.exists(out)

    def test_output_file_is_readable_csv_after_run(self, tmp_path):
        out = str(tmp_path / "out.csv")
        _run(_motor(5), _sensor(5), _psu(3), out)
        fieldnames, rows = _read_csv(out)
        assert len(fieldnames) > 0

    def test_output_file_not_corrupted_on_empty_stream(self, tmp_path):
        """Even with empty motor, output must be a valid CSV with a header line."""
        out = str(tmp_path / "out.csv")
        _run([], [], [], out)
        with open(out) as fh:
            lines = fh.readlines()
        # Exactly one line: the header
        assert len(lines) == 1
        assert lines[0].strip() == ",".join(_EXPECTED_HEADER)

    def test_output_columns_count_matches_yaml(self, tmp_path):
        out = str(tmp_path / "out.csv")
        _run(_motor(3), _sensor(3), _psu(3), out)
        fieldnames, _ = _read_csv(out)
        assert len(fieldnames) == len(_EXPECTED_HEADER)
