"""Tests for automation/synchronizer.py — nearest-prior join."""
from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest

from automation.synchronizer import Synchronizer
from drivers.motor import MotorDataSource
from drivers.psu import PSUDataSource
from drivers.sensor import SensorDataSource


# ---------------------------------------------------------------------------
# Minimal fake data sources (list-backed, no mocking magic)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _motor(ts: float, vel: float = 0.0) -> Dict[str, Any]:
    return {"timestamp_s": ts, "velocity_rad_s": vel, "measured_current_a": 0.0}


def _sensor(ts: float, torque: float = 0.0) -> Dict[str, Any]:
    return {"timestamp_s": ts, "torque_nm": torque}


def _psu(ts: float, voltage: float = 12.0) -> Dict[str, Any]:
    return {"timestamp_s": ts, "voltage_v": voltage, "current_a": 1.0}


# ---------------------------------------------------------------------------
# 1. Happy-path nearest-prior join
# ---------------------------------------------------------------------------

class TestSynchronizerNearestPrior:

    def _make(self):
        motor = FakeMotor([_motor(0.001), _motor(0.002), _motor(0.003)])
        sensor = FakeSensor([_sensor(0.0005, 10.0), _sensor(0.0015, 20.0), _sensor(0.0025, 30.0)])
        psu = FakePSU([_psu(0.000, 12.0), _psu(0.002, 11.5)])
        return Synchronizer(motor, sensor, psu)

    def test_correct_sensor_prior_assigned(self):
        results = list(self._make())
        assert results[0]["torque_nm"] == 10.0   # 0.0005 <= 0.001
        assert results[1]["torque_nm"] == 20.0   # 0.0015 <= 0.002
        assert results[2]["torque_nm"] == 30.0   # 0.0025 <= 0.003

    def test_correct_psu_prior_assigned(self):
        results = list(self._make())
        assert results[0]["voltage_v"] == 12.0   # 0.000 <= 0.001
        assert results[1]["voltage_v"] == 11.5   # 0.002 <= 0.002 (exact match)
        assert results[2]["voltage_v"] == 11.5   # last psu still 0.002

    def test_motor_fields_present_in_every_row(self):
        results = list(self._make())
        for row in results:
            assert "timestamp_s" in row
            assert "velocity_rad_s" in row
            assert "measured_current_a" in row

    def test_motor_timestamp_wins_on_collision(self):
        # sensor also has timestamp_s — motor's must survive
        motor = FakeMotor([_motor(0.001)])
        sensor = FakeSensor([_sensor(0.0005)])   # sensor timestamp != motor timestamp
        psu = FakePSU([_psu(0.000)])
        syncer = Synchronizer(motor, sensor, psu)
        row = list(syncer)[0]
        assert row["timestamp_s"] == pytest.approx(0.001)

    def test_row_count_equals_motor_count(self):
        results = list(self._make())
        assert len(results) == 3

    def test_sensor_future_sample_not_used(self):
        motor = FakeMotor([_motor(0.001)])
        sensor = FakeSensor([_sensor(0.002, 99.0)])  # arrives after motor sample
        psu = FakePSU([_psu(0.000)])
        syncer = Synchronizer(motor, sensor, psu)
        row = list(syncer)[0]
        assert "torque_nm" not in row  # no prior sensor available


# ---------------------------------------------------------------------------
# 2. Bootstrap — motor samples arrive before any sensor / PSU data
# ---------------------------------------------------------------------------

class TestSynchronizerBootstrap:

    def _make(self):
        motor = FakeMotor([_motor(0.001), _motor(0.002)])
        sensor = FakeSensor([_sensor(0.010, 5.0)])   # arrives after all motor
        psu = FakePSU([_psu(0.010, 12.0)])            # same
        return Synchronizer(motor, sensor, psu)

    def test_stat_without_sensor_prior_incremented(self):
        syncer = self._make()
        list(syncer)
        assert syncer.get_stats()["motor_samples_without_sensor_prior"] == 2

    def test_stat_without_psu_prior_incremented(self):
        syncer = self._make()
        list(syncer)
        assert syncer.get_stats()["motor_samples_without_psu_prior"] == 2

    def test_sensor_field_absent_when_no_prior(self):
        syncer = self._make()
        rows = list(syncer)
        for row in rows:
            assert "torque_nm" not in row

    def test_psu_field_absent_when_no_prior(self):
        syncer = self._make()
        rows = list(syncer)
        for row in rows:
            assert "voltage_v" not in row

    def test_motor_fields_still_present_during_bootstrap(self):
        syncer = self._make()
        rows = list(syncer)
        for row in rows:
            assert "timestamp_s" in row
            assert "velocity_rad_s" in row

    def test_motor_total_correct(self):
        syncer = self._make()
        list(syncer)
        assert syncer.get_stats()["motor_samples_total"] == 2


# ---------------------------------------------------------------------------
# 3. Jitter — sensor timestamps have ±0.5 ms jitter; never use future sample
# ---------------------------------------------------------------------------

class TestSynchronizerJitter:

    def test_slightly_before_motor_sample_used(self):
        motor = FakeMotor([_motor(0.001000)])
        sensor = FakeSensor([_sensor(0.000900, 10.0)])  # 0.1ms before
        psu = FakePSU([_psu(0.000)])
        row = list(Synchronizer(motor, sensor, psu))[0]
        assert row["torque_nm"] == pytest.approx(10.0)

    def test_sample_just_after_motor_not_used(self):
        motor = FakeMotor([_motor(0.001000)])
        sensor = FakeSensor([_sensor(0.001001, 99.0)])  # 1µs after — future
        psu = FakePSU([_psu(0.000)])
        row = list(Synchronizer(motor, sensor, psu))[0]
        assert "torque_nm" not in row

    def test_exact_timestamp_match_used(self):
        motor = FakeMotor([_motor(0.001000)])
        sensor = FakeSensor([_sensor(0.001000, 42.0)])  # exact match
        psu = FakePSU([_psu(0.000)])
        row = list(Synchronizer(motor, sensor, psu))[0]
        assert row["torque_nm"] == pytest.approx(42.0)

    def test_jitter_sequence_picks_correct_prior(self):
        # sensor at 4800Hz with jitter; verify correct prior for each motor sample
        motor = FakeMotor([_motor(0.001000), _motor(0.002000), _motor(0.003000)])
        sensor = FakeSensor([
            _sensor(0.000900, 10.0),   # prior of motor[0]
            _sensor(0.001100, 11.0),   # prior of motor[1] (between [0] and [1])
            _sensor(0.002999, 12.0),   # prior of motor[2] (barely before)
            _sensor(0.003001, 13.0),   # future — NOT prior of motor[2]
        ])
        psu = FakePSU([_psu(0.000)])
        rows = list(Synchronizer(motor, sensor, psu))
        assert rows[0]["torque_nm"] == pytest.approx(10.0)
        assert rows[1]["torque_nm"] == pytest.approx(11.0)
        assert rows[2]["torque_nm"] == pytest.approx(12.0)

    def test_last_known_prior_sticks_when_sensor_lags(self):
        # motor advances but no new sensor sample arrives
        motor = FakeMotor([_motor(0.001), _motor(0.002), _motor(0.003)])
        sensor = FakeSensor([_sensor(0.0005, 7.0)])  # single old sample
        psu = FakePSU([_psu(0.000)])
        rows = list(Synchronizer(motor, sensor, psu))
        for row in rows:
            assert row["torque_nm"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# 4. Rate mismatch — 1000 Hz motor vs 10 Hz PSU
# ---------------------------------------------------------------------------

class TestSynchronizerRateMismatch:

    def _make_100row_syncer(self):
        # 100 motor samples at 1ms spacing; PSU at t=0.000 and t=0.100
        motor_samples = [_motor(i * 0.001) for i in range(100)]
        psu_samples = [_psu(0.000, 12.0), _psu(0.100, 11.5)]
        sensor_samples = [_sensor(i * 0.001) for i in range(100)]
        return Synchronizer(
            FakeMotor(motor_samples),
            FakeSensor(sensor_samples),
            FakePSU(psu_samples),
        )

    def test_psu_sticks_across_motor_rows(self):
        rows = list(self._make_100row_syncer())
        # rows 0..98: t=0.000..0.098, psu prior = 0.000 (v=12.0)
        for row in rows[:99]:
            assert row["voltage_v"] == pytest.approx(12.0)

    def test_psu_advances_at_correct_motor_row(self):
        rows = list(self._make_100row_syncer())
        # row 99: t=0.099 — psu 0.100 is NOT prior (0.100 > 0.099); still 12.0
        assert rows[99]["voltage_v"] == pytest.approx(12.0)

    def test_psu_consumed_count(self):
        syncer = self._make_100row_syncer()
        list(syncer)
        # only psu[0] consumed within motor range (psu[1]=0.100 > max motor t=0.099)
        assert syncer.get_stats()["psu_samples_consumed"] == 1

    def test_psu_leftover_after_motor_end(self):
        syncer = self._make_100row_syncer()
        list(syncer)
        # psu[1]=0.100 never consumed → leftover = 1
        assert syncer.get_stats()["sensor_samples_after_motor_end"] == 0
        assert syncer.get_stats()["psu_samples_after_motor_end"] == 1


# ---------------------------------------------------------------------------
# 5. Stats — reset on re-iteration; consumed totals match input
# ---------------------------------------------------------------------------

class TestSynchronizerStats:

    def _make(self):
        motor = FakeMotor([_motor(0.001), _motor(0.002), _motor(0.003)])
        sensor = FakeSensor([_sensor(0.0005), _sensor(0.0015), _sensor(0.0025)])
        psu = FakePSU([_psu(0.000), _psu(0.002)])
        return Synchronizer(motor, sensor, psu)

    def test_stats_start_at_zero(self):
        syncer = self._make()
        stats = syncer.get_stats()
        for v in stats.values():
            assert v == 0

    def test_motor_total_matches_input(self):
        syncer = self._make()
        list(syncer)
        assert syncer.get_stats()["motor_samples_total"] == 3

    def test_sensor_consumed_matches_input(self):
        syncer = self._make()
        list(syncer)
        assert syncer.get_stats()["sensor_samples_consumed"] == 3

    def test_psu_consumed_matches_input(self):
        syncer = self._make()
        list(syncer)
        assert syncer.get_stats()["psu_samples_consumed"] == 2

    def test_stats_reset_on_reiteration(self):
        syncer = self._make()
        list(syncer)
        stats_first = syncer.get_stats()
        list(syncer)
        stats_second = syncer.get_stats()
        assert stats_first == stats_second

    def test_no_without_prior_when_all_streams_start_before_motor(self):
        syncer = self._make()
        list(syncer)
        assert syncer.get_stats()["motor_samples_without_sensor_prior"] == 0
        assert syncer.get_stats()["motor_samples_without_psu_prior"] == 0

    def test_get_stats_returns_copy(self):
        syncer = self._make()
        list(syncer)
        stats = syncer.get_stats()
        stats["motor_samples_total"] = 999
        assert syncer.get_stats()["motor_samples_total"] == 3


# ---------------------------------------------------------------------------
# 6. End-of-stream — all three exhaustion cases
# ---------------------------------------------------------------------------

class TestSynchronizerEndOfStream:

    def test_motor_ends_first_sensor_leftover_counted(self):
        motor = FakeMotor([_motor(0.001)])
        sensor = FakeSensor([_sensor(0.000), _sensor(0.002), _sensor(0.003)])
        psu = FakePSU([_psu(0.000)])
        syncer = Synchronizer(motor, sensor, psu)
        list(syncer)
        # sensor[0]=0.000 consumed; sensor[1]=0.002 and sensor[2]=0.003 are leftover
        assert syncer.get_stats()["sensor_samples_after_motor_end"] == 2

    def test_motor_ends_first_psu_leftover_counted(self):
        motor = FakeMotor([_motor(0.001)])
        sensor = FakeSensor([_sensor(0.000)])
        psu = FakePSU([_psu(0.000), _psu(0.005), _psu(0.010)])
        syncer = Synchronizer(motor, sensor, psu)
        list(syncer)
        assert syncer.get_stats()["psu_samples_after_motor_end"] == 2

    def test_sensor_ends_first_last_value_sticks(self):
        # sensor exhausts at t=0.001; motor continues to t=0.003
        motor = FakeMotor([_motor(0.001), _motor(0.002), _motor(0.003)])
        sensor = FakeSensor([_sensor(0.000, 55.0)])  # only one sample
        psu = FakePSU([_psu(0.000)])
        rows = list(Synchronizer(motor, sensor, psu))
        for row in rows:
            assert row["torque_nm"] == pytest.approx(55.0)

    def test_psu_ends_first_last_value_sticks(self):
        motor = FakeMotor([_motor(0.001), _motor(0.002), _motor(0.003)])
        sensor = FakeSensor([_sensor(0.000)])
        psu = FakePSU([_psu(0.000, 9.9)])  # only one sample
        rows = list(Synchronizer(motor, sensor, psu))
        for row in rows:
            assert row["voltage_v"] == pytest.approx(9.9)

    def test_empty_motor_yields_nothing(self):
        motor = FakeMotor([])
        sensor = FakeSensor([_sensor(0.001)])
        psu = FakePSU([_psu(0.001)])
        rows = list(Synchronizer(motor, sensor, psu))
        assert rows == []

    def test_empty_motor_counts_remaining_as_leftover(self):
        motor = FakeMotor([])
        sensor = FakeSensor([_sensor(0.001), _sensor(0.002)])
        psu = FakePSU([_psu(0.001)])
        syncer = Synchronizer(motor, sensor, psu)
        list(syncer)
        assert syncer.get_stats()["sensor_samples_after_motor_end"] == 2
        assert syncer.get_stats()["psu_samples_after_motor_end"] == 1

    def test_all_streams_empty_yields_nothing(self):
        syncer = Synchronizer(FakeMotor([]), FakeSensor([]), FakePSU([]))
        assert list(syncer) == []
