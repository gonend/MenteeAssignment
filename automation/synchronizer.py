from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, Optional

from config.consts import _ZERO_STATS
from drivers.motor import MotorDataSource
from drivers.psu import PSUDataSource
from drivers.sensor import SensorDataSource

logger = logging.getLogger(__name__)


class Synchronizer:
    """Nearest-prior join over three independent streams.

    For each motor sample (primary clock), attaches the most-recent sensor
    and PSU readings whose timestamps are <= the motor timestamp.
    Never interpolates or fabricates samples.
    """

    def __init__(
        self,
        motor: MotorDataSource,
        sensor: SensorDataSource,
        psu: PSUDataSource,
    ) -> None:
        self._motor = motor
        self._sensor = sensor
        self._psu = psu
        self._stats: Dict[str, int] = dict(_ZERO_STATS)

    def get_stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        self._stats = dict(_ZERO_STATS)

        last_sensor: Optional[Dict[str, Any]] = None
        last_psu: Optional[Dict[str, Any]] = None

        sensor_iter = iter(self._sensor)
        psu_iter = iter(self._psu)

        peek_sensor = next(sensor_iter, None)
        peek_psu = next(psu_iter, None)

        for m in self._motor:
            t_m = m["timestamp_s"]
            self._stats["motor_samples_total"] += 1

            while peek_sensor is not None and peek_sensor["timestamp_s"] <= t_m:
                last_sensor = peek_sensor
                self._stats["sensor_samples_consumed"] += 1
                peek_sensor = next(sensor_iter, None)

            while peek_psu is not None and peek_psu["timestamp_s"] <= t_m:
                last_psu = peek_psu
                self._stats["psu_samples_consumed"] += 1
                peek_psu = next(psu_iter, None)

            if last_sensor is None:
                self._stats["motor_samples_without_sensor_prior"] += 1
            if last_psu is None:
                self._stats["motor_samples_without_psu_prior"] += 1

            # Motor fields win on key collision (motor timestamp_s is primary clock).
            merged: Dict[str, Any] = {}
            if last_psu is not None:
                merged.update(last_psu)
            if last_sensor is not None:
                merged.update(last_sensor)
            merged.update(m)

            yield merged

        # Count samples that arrived after motor stream exhausted.
        if peek_sensor is not None:
            leftover = 1
            for _ in sensor_iter:
                leftover += 1
            self._stats["sensor_samples_after_motor_end"] = leftover

        if peek_psu is not None:
            leftover = 1
            for _ in psu_iter:
                leftover += 1
            self._stats["psu_samples_after_motor_end"] = leftover
