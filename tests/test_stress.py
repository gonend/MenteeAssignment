"""Stress tests — robustness proofs for the Motor Characterization Bench.

Five scenarios:
  1. TestPoisonedData              — malformed CSV + corrupt binary; readers recover.
  2. TestSchemaDriftRaisesKeyError — fail-fast init when YAML key missing.
  3. TestDivByZeroEfficiency       — Logger writes "" not crash on div/zero.
  4. TestImpatientOperatorAbort    — request_abort + abort_event honored on next row.
  5. TestO1MemoryUnderInfiniteStream — 500k-row stream stays under 500 KB peak growth.
"""
from __future__ import annotations

import argparse
import copy
import csv
import gc
import io
import logging
import os
import struct
import threading
import tracemalloc
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import patch

import pytest
import yaml

from automation.logger import Logger
from automation.main import _cfg_get, run_pipeline
from automation.row_schema import SchemaProjector
from automation.state_machine import StateMachine
from drivers.motor import MotorBinaryReader, MotorDataSource
from drivers.psu import PSUDataSource
from drivers.sensor import SensorCSVReader, SensorDataSource


# ---------------------------------------------------------------------------
# Shared minimal YAML config (mirrors tests/test_main.py::_CFG)
# ---------------------------------------------------------------------------

_BASE_CFG: Dict[str, Any] = {
    "test": {
        "name": "stress",
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


def _cfg() -> Dict[str, Any]:
    return copy.deepcopy(_BASE_CFG)


# ---------------------------------------------------------------------------
# Fake drivers (copied shape from tests/test_main.py)
# ---------------------------------------------------------------------------

class _FakeMotor(MotorDataSource):
    def __init__(self, samples: List[Dict[str, Any]]) -> None:
        self._samples = samples
        self.stats: Dict[str, int] = {}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        yield from self._samples

    def get_stats(self) -> Dict[str, Any]:
        return dict(self.stats)


class _FakeSensor(SensorDataSource):
    def __init__(self, samples: List[Dict[str, Any]]) -> None:
        self._samples = samples
        self.stats: Dict[str, int] = {}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        yield from self._samples

    def get_stats(self) -> Dict[str, Any]:
        return dict(self.stats)


class _FakePSU(PSUDataSource):
    def __init__(self, samples: List[Dict[str, Any]]) -> None:
        self._samples = samples
        self.stats: Dict[str, int] = {}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        yield from self._samples

    def get_stats(self) -> Dict[str, Any]:
        return dict(self.stats)


def _args(output_path: str) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.motor = "fake.csv"
    ns.motor_format = "csv"
    ns.sensor = "fake_sensor.csv"
    ns.psu = "fake_psu.csv"
    ns.output = output_path
    return ns


# ---------------------------------------------------------------------------
# Binary-packet builder (mirrors tests/test_motor_binary.py::_make_packet)
# ---------------------------------------------------------------------------

def _make_packet(
    timestamp_ms: int,
    velocity: float,
    current: float,
    *,
    corrupt_checksum: bool = False,
) -> bytes:
    start = bytes([0xAA, 0x55])
    header = struct.pack("<BBI", 0x42, 9, timestamp_ms)
    payload = struct.pack("<Bff", 0x0E, velocity, current)
    cs = 0
    for b in start + header + payload:
        cs ^= b
    if corrupt_checksum:
        cs ^= 0xFF
    return start + header + payload + bytes([cs]) + bytes([0x55, 0xAA])


@pytest.fixture
def protocol_config():
    cfg_path = Path(__file__).parent.parent / "config" / "motor_protocol.yaml"
    with open(cfg_path, "r") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# 1. Poisoned Data — Hardware Failure
# ---------------------------------------------------------------------------

class TestPoisonedData:
    """Malformed text + corrupt bytes must be skipped, never raised."""

    def test_sensor_csv_skips_malformed_rows_and_logs_warnings(
        self, tmp_path, caplog
    ):
        csv_file = tmp_path / "poisoned_sensor.csv"
        # Row 2 valid; row 3 wrong field count; row 4 non-numeric; row 5 valid.
        csv_file.write_text(
            "timestamp_s,torque_nm\n"
            "0.001,0.5\n"
            "GARBAGE,WITH,TOO,MANY,FIELDS\n"
            "0.002,not_a_number\n"
            "0.003,1.5\n"
        )
        reader = SensorCSVReader(str(csv_file), _cfg())

        with caplog.at_level(logging.WARNING, logger="drivers.sensor"):
            rows = list(reader)  # must not raise

        assert len(rows) == 2, "only the two well-formed rows should survive"
        assert reader.stats["malformed_rows"] >= 2
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_motor_binary_recovers_from_garbage_and_corrupt_checksum(
        self, tmp_path, caplog, protocol_config
    ):
        # Layout: [8 bytes garbage][corrupt-checksum packet][valid packet]
        # Reader must byte-scan past garbage, drop the corrupt packet,
        # and still emit the trailing valid one.
        garbage = b"\x00\x11\x22\x33\x44\x55\x66\x77"
        bad = _make_packet(1000, 1.0, 0.5, corrupt_checksum=True)
        good = _make_packet(2000, 2.5, 1.5)

        bin_file = tmp_path / "poisoned_motor.bin"
        bin_file.write_bytes(garbage + bad + good)

        reader = MotorBinaryReader(str(bin_file), protocol_config)

        with caplog.at_level(logging.WARNING, logger="drivers.motor"):
            records = list(reader)  # must not raise

        # The valid trailing packet must come through.
        assert len(records) >= 1
        valid_records = [r for r in records if r["timestamp_s"] == pytest.approx(2.0)]
        assert valid_records, "the well-formed packet must survive"
        assert valid_records[0]["velocity_rad_s"] == pytest.approx(2.5, rel=1e-5)

        # At least one of: checksum reject or resync warning was emitted.
        assert reader.stats["checksum_errors"] >= 1
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)


# ---------------------------------------------------------------------------
# 2. Schema Drift — Config Failure
# ---------------------------------------------------------------------------

class TestSchemaDriftRaisesKeyError:
    """Missing nested config key must fail fast with the dotted path."""

    def test_state_machine_missing_ramp_duration_raises_with_path(self):
        cfg = _cfg()
        del cfg["test"]["phases"][1]["parameters"]["ramp_duration_s"]

        with pytest.raises(
            KeyError,
            match=r"test\.phases\[CURRENT_RAMP\]\.parameters\.ramp_duration_s",
        ):
            StateMachine(cfg)

    def test_cfg_get_missing_nested_key_dotted_message(self):
        cfg = _cfg()
        with pytest.raises(
            KeyError,
            match=r"Missing required config key: data_sources\.sensor\.formats\.csv\.nonexistent",
        ):
            _cfg_get(cfg, "data_sources", "sensor", "formats", "csv", "nonexistent")


# ---------------------------------------------------------------------------
# 3. Division by Zero — Math Safety
# ---------------------------------------------------------------------------

class TestDivByZeroEfficiency:
    """When PSU readings are 0.0, efficiency cell must be empty string."""

    _EFF_FORMULA = "(torque_nm * velocity_rad_s) / (psu_voltage_v * psu_current_a)"

    def _eff_cfg(self) -> Dict[str, Any]:
        return {
            "output": {
                "columns": [
                    "timestamp_s", "torque_nm", "velocity_rad_s",
                    "psu_voltage_v", "psu_current_a", "efficiency",
                ],
                "efficiency": {"formula": self._EFF_FORMULA},
            }
        }

    def _row(self, *, voltage: float, current: float) -> Dict[str, Any]:
        return {
            "timestamp_s": 1.0,
            "torque_nm": 5.0,
            "velocity_rad_s": 10.0,
            "voltage_v": voltage,    # → psu_voltage_v
            "current_a": current,    # → psu_current_a
        }

    def _parse(self, buf: io.StringIO) -> List[Dict[str, str]]:
        buf.seek(0)
        return list(csv.DictReader(buf))

    def test_zero_voltage_writes_empty_string(self):
        buf = io.StringIO()
        log = Logger(self._eff_cfg(), buf)
        log.write(self._row(voltage=0.0, current=2.0))   # must not crash
        rows = self._parse(buf)
        assert rows[0]["efficiency"] == ""

    def test_zero_current_writes_empty_string(self):
        buf = io.StringIO()
        log = Logger(self._eff_cfg(), buf)
        log.write(self._row(voltage=24.0, current=0.0))
        rows = self._parse(buf)
        assert rows[0]["efficiency"] == ""

    def test_both_zero_writes_empty_string(self):
        buf = io.StringIO()
        log = Logger(self._eff_cfg(), buf)
        log.write(self._row(voltage=0.0, current=0.0))
        rows = self._parse(buf)
        assert rows[0]["efficiency"] == ""
        assert rows[0]["efficiency"] != "None"


# ---------------------------------------------------------------------------
# 4. Impatient Operator — Threading / Abort
# ---------------------------------------------------------------------------

class TestImpatientOperatorAbort:
    """request_abort() and abort_event must transition to COMPLETE on next row."""

    def _row(self, t: float) -> Dict[str, Any]:
        return {
            "timestamp_s": t,
            "velocity_rad_s": 1.0,
            "measured_current_a": 0.0,
            "torque_nm": 0.0,
            "voltage_v": 24.0,
            "current_a": 1.0,
        }

    def test_state_machine_intercepts_abort_on_next_process_call(self):
        sm = StateMachine(_cfg())

        # Run a few rows to advance past SETUP.
        for i in range(5):
            sm.process(self._row(t=i * 0.001))
        assert not sm.is_complete
        pre_abort_phase = sm.current_phase

        # GUI thread fires the abort.
        sm.request_abort("user_clicked_stop")

        # The very next process() call must transition to COMPLETE.
        result = sm.process(self._row(t=0.005))
        assert sm.is_complete
        assert sm.current_phase == "COMPLETE"
        assert result["test_phase"] == "COMPLETE"

        stats = sm.get_stats()
        assert stats["abort_reason"] == "user_clicked_stop"
        # A transition entry recorded from whatever phase we were in to COMPLETE.
        assert any(t[1] == "COMPLETE" for t in stats["transitions"])
        assert pre_abort_phase != "COMPLETE"  # sanity

    def test_pipeline_loop_breaks_on_pre_set_abort_event(self, tmp_path):
        out = str(tmp_path / "aborted.csv")
        event = threading.Event()
        event.set()  # operator slammed the button before the pipeline got going

        motor = [{"timestamp_s": i * 0.001,
                  "velocity_rad_s": float(i),
                  "measured_current_a": 0.0} for i in range(50)]
        sensor = [{"timestamp_s": i * 0.001, "torque_nm": 0.0} for i in range(50)]
        psu = [{"timestamp_s": 0.0, "voltage_v": 24.0, "current_a": 1.0}]

        with patch("automation.main.MotorCSVReader",  return_value=_FakeMotor(motor)), \
             patch("automation.main.SensorCSVReader", return_value=_FakeSensor(sensor)), \
             patch("automation.main.PSUCSVReader",    return_value=_FakePSU(psu)):
            run_pipeline(_args(out), _cfg(), None, abort_event=event)

        with open(out, newline="") as fh:
            rows = list(csv.DictReader(fh))

        # Pipeline broke immediately — exactly one row written, in COMPLETE.
        assert len(rows) == 1
        assert rows[0]["test_phase"] == "COMPLETE"


# ---------------------------------------------------------------------------
# 5. O(1) Memory Proof — Infinite Stream
# ---------------------------------------------------------------------------

class TestO1MemoryUnderInfiniteStream:
    """500 000 rows through SchemaProjector → StateMachine → Logger.

    Asserts that peak memory growth between row 1 000 and row 500 000 is well
    under 500 KB — proves the pipeline is bounded O(1), not accumulating rows.
    Threshold deliberately generous to absorb GC / int-interning noise across
    platforms (per mentor advice).
    """

    _N_TOTAL = 500_000
    _SNAPSHOT_AT = 1_000
    _MAX_GROWTH_BYTES = 500_000  # 500 KB ceiling

    def _stream(self, n: int) -> Iterator[Dict[str, Any]]:
        # Stays in CURRENT_RAMP forever (torque=0, velocity steady, voltage steady).
        for i in range(n):
            yield {
                "timestamp_s": i * 0.001,
                "velocity_rad_s": 1.0,
                "measured_current_a": 0.0,
                "torque_nm": 0.0,
                "voltage_v": 24.0,
                "current_a": 1.0,
            }

    def _stream_cfg(self) -> Dict[str, Any]:
        # Push every threshold so far out of reach that the SM never leaves
        # CURRENT_RAMP for the full 500k rows.
        cfg = _cfg()
        cfg["test"]["phases"][1]["parameters"]["max_current_a"] = 1e9
        cfg["test"]["phases"][1]["parameters"]["target_torque_nm"] = 1e9
        cfg["test"]["phases"][1]["parameters"]["ramp_duration_s"] = 1e9
        cfg["test"]["phases"][2]["parameters"]["hold_duration_s"] = 1e9
        cfg["test"]["phases"][3]["parameters"]["min_voltage_v"] = -1.0
        cfg["test"]["safety"]["max_torque_nm"] = 1e9
        cfg["test"]["safety"]["max_current_a"] = 1e9
        return cfg

    def _expected_keys(self, cfg: Dict[str, Any]) -> Tuple[str, ...]:
        ds = cfg["data_sources"]
        sensor = [c["name"] for c in ds["sensor"]["formats"]["csv"]["columns"]]
        psu = [c["name"] for c in ds["power_supply"]["formats"]["csv"]["columns"]]
        motor = [c["name"] for c in ds["motor"]["formats"]["csv"]["columns"]]
        return tuple(set(sensor) | set(psu) | set(motor))

    def test_500k_row_stream_is_bounded_o1(self):
        cfg = self._stream_cfg()
        keys = self._expected_keys(cfg)

        projector = SchemaProjector(self._stream(self._N_TOTAL), keys)
        sm = StateMachine(cfg)

        with open(os.devnull, "w", newline="") as devnull:
            log = Logger(cfg, devnull)

            tracemalloc.start()
            snap_early = None

            for i, row in enumerate(projector, start=1):
                augmented = sm.process(row)
                log.write(augmented)

                if i == self._SNAPSHOT_AT:
                    gc.collect()
                    snap_early = tracemalloc.take_snapshot()

            gc.collect()
            snap_late = tracemalloc.take_snapshot()
            tracemalloc.stop()

        assert snap_early is not None, "early snapshot should have been taken"

        diffs = snap_late.compare_to(snap_early, "filename")
        total_growth = sum(d.size_diff for d in diffs)

        assert log.rows_written == self._N_TOTAL
        # Stayed in CURRENT_RAMP for the whole stream — proves SM didn't
        # transition (which would also pass the memory bound but fail intent).
        assert sm.current_phase == "CURRENT_RAMP"
        assert total_growth < self._MAX_GROWTH_BYTES, (
            f"Memory grew {total_growth} bytes across "
            f"{self._N_TOTAL - self._SNAPSHOT_AT} rows — exceeds "
            f"O(1) ceiling of {self._MAX_GROWTH_BYTES} bytes."
        )
