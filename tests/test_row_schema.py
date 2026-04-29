"""Tests for automation/row_schema.py — SchemaProjector adapter."""
from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest

from automation.row_schema import SchemaProjector
from automation.synchronizer import Synchronizer
from drivers.motor import MotorDataSource
from drivers.psu import PSUDataSource
from drivers.sensor import SensorDataSource


# ---------------------------------------------------------------------------
# Fakes (mirror tests/test_synchronizer.py for integration tests)
# ---------------------------------------------------------------------------

class FakeMotor(MotorDataSource):
    def __init__(self, samples: List[Dict[str, Any]]) -> None:
        self._samples = samples

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        yield from self._samples

    def get_stats(self) -> Dict[str, Any]:
        return {}


class FakeSensor(SensorDataSource):
    def __init__(self, samples: List[Dict[str, Any]]) -> None:
        self._samples = samples

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        yield from self._samples

    def get_stats(self) -> Dict[str, Any]:
        return {}


class FakePSU(PSUDataSource):
    def __init__(self, samples: List[Dict[str, Any]]) -> None:
        self._samples = samples

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        yield from self._samples

    def get_stats(self) -> Dict[str, Any]:
        return {}


def _motor(ts: float, vel: float = 0.0) -> Dict[str, Any]:
    return {"timestamp_s": ts, "velocity_rad_s": vel, "measured_current_a": 0.0}


def _sensor(ts: float, torque: float = 0.0) -> Dict[str, Any]:
    return {"timestamp_s": ts, "torque_nm": torque}


def _psu(ts: float, voltage: float = 12.0) -> Dict[str, Any]:
    return {"timestamp_s": ts, "voltage_v": voltage, "current_a": 1.0}


# ---------------------------------------------------------------------------
# 1. Pure adapter behavior (no Synchronizer)
# ---------------------------------------------------------------------------

class TestSchemaProjector:

    def test_missing_keys_filled_with_none(self):
        source = [{"timestamp_s": 0.001}]
        rows = list(SchemaProjector(source, expected_keys=["torque_nm", "voltage_v"]))
        assert rows[0]["torque_nm"] is None
        assert rows[0]["voltage_v"] is None

    def test_existing_values_not_overwritten(self):
        source = [{"timestamp_s": 0.001, "torque_nm": 42.0, "voltage_v": 12.0}]
        rows = list(SchemaProjector(source, expected_keys=["torque_nm", "voltage_v"]))
        assert rows[0]["torque_nm"] == pytest.approx(42.0)
        assert rows[0]["voltage_v"] == pytest.approx(12.0)

    def test_unexpected_keys_passed_through(self):
        source = [{"timestamp_s": 0.001, "extra_field": "kept"}]
        rows = list(SchemaProjector(source, expected_keys=["torque_nm"]))
        assert rows[0]["extra_field"] == "kept"
        assert rows[0]["torque_nm"] is None

    def test_empty_expected_keys_passes_rows_unchanged(self):
        source = [{"timestamp_s": 0.001, "x": 1}, {"timestamp_s": 0.002, "x": 2}]
        rows = list(SchemaProjector(source, expected_keys=[]))
        assert rows == source

    def test_source_dict_not_mutated(self):
        original = {"timestamp_s": 0.001}
        source = [original]
        list(SchemaProjector(source, expected_keys=["torque_nm"]))
        assert "torque_nm" not in original

    def test_iterates_lazily(self):
        consumed = []

        def gen():
            for i in range(3):
                consumed.append(i)
                yield {"timestamp_s": float(i)}

        proj = SchemaProjector(gen(), expected_keys=["torque_nm"])
        it = iter(proj)
        # No rows pulled yet
        assert consumed == []
        next(it)
        assert consumed == [0]
        next(it)
        assert consumed == [0, 1]

    def test_duplicate_expected_keys_no_overwrite(self):
        # timestamp_s appears twice (e.g. combined sensor+psu YAML lists);
        # must not overwrite the value already present from motor merge.
        source = [{"timestamp_s": 0.001, "velocity_rad_s": 5.0}]
        rows = list(SchemaProjector(
            source,
            expected_keys=["timestamp_s", "torque_nm", "timestamp_s", "voltage_v"],
        ))
        assert rows[0]["timestamp_s"] == pytest.approx(0.001)
        assert rows[0]["torque_nm"] is None
        assert rows[0]["voltage_v"] is None


# ---------------------------------------------------------------------------
# 2. Integration: Synchronizer + SchemaProjector
# ---------------------------------------------------------------------------

class TestSchemaProjectorWithSynchronizer:

    SENSOR_KEYS = ["timestamp_s", "torque_nm"]
    PSU_KEYS = ["timestamp_s", "voltage_v", "current_a"]

    def test_works_with_synchronizer_bootstrap(self):
        # Motor leads; sensor/PSU first samples arrive AFTER first motor sample.
        motor = FakeMotor([_motor(0.001), _motor(0.005)])
        sensor = FakeSensor([_sensor(0.003, 7.5)])
        psu = FakePSU([_psu(0.004, 11.5)])
        syncer = Synchronizer(motor, sensor, psu)
        expected = [*self.SENSOR_KEYS, *self.PSU_KEYS]
        rows = list(SchemaProjector(syncer, expected_keys=expected))

        # row 0 (t=0.001): no priors → sentinels
        assert rows[0]["torque_nm"] is None
        assert rows[0]["voltage_v"] is None
        assert rows[0]["current_a"] is None
        # motor's timestamp_s preserved (duplicate key in expected list harmless)
        assert rows[0]["timestamp_s"] == pytest.approx(0.001)

        # row 1 (t=0.005): real priors arrived
        assert rows[1]["torque_nm"] == pytest.approx(7.5)
        assert rows[1]["voltage_v"] == pytest.approx(11.5)

    def test_works_with_synchronizer_realprior_unchanged(self):
        # All priors available before motor stream — adapter must not alter values.
        motor = FakeMotor([_motor(0.001), _motor(0.002)])
        sensor = FakeSensor([_sensor(0.0005, 10.0)])
        psu = FakePSU([_psu(0.000, 12.0)])
        syncer = Synchronizer(motor, sensor, psu)
        expected = [*self.SENSOR_KEYS, *self.PSU_KEYS]
        rows = list(SchemaProjector(syncer, expected_keys=expected))

        for row in rows:
            assert row["torque_nm"] == pytest.approx(10.0)
            assert row["voltage_v"] == pytest.approx(12.0)
            assert row["current_a"] == pytest.approx(1.0)
