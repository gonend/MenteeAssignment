"""Headless smoke tests for ui/gui.py — MonitoringApp.

Important: tk.Tk() must only be instantiated ONCE per process — multiple
instances corrupt Tcl's internal state on some Python/Windows builds.

Design: one module-scoped root instance; a function-scoped wrapper fixture
resets all mutable StringVars before each test.  Tests that call
_on_closing() (which calls destroy()) patch destroy() with a no-op so the
shared instance survives.
"""
from __future__ import annotations

import queue
import threading
from typing import List
from unittest.mock import patch

import pytest

tkinter = pytest.importorskip("tkinter")  # skip entire module if Tk unavailable

from ui.gui import MonitoringApp  # noqa: E402 — import after importorskip


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def root_app():
    """Single MonitoringApp for the entire module — created once, destroyed once."""
    _app = MonitoringApp()
    _app.withdraw()
    yield _app
    if _app._poll_id is not None:
        try:
            _app.after_cancel(_app._poll_id)
        except Exception:
            pass
    _app.destroy()


@pytest.fixture
def app(root_app):
    """Reset mutable state before each test; return the shared instance."""
    # Path vars
    for var in (
        root_app._config_path, root_app._motor_path, root_app._sensor_path,
        root_app._psu_path, root_app._output_path, root_app._protocol_path,
    ):
        var.set("")
    root_app._speed_var.set("Max")
    root_app._status_var.set("Idle")
    root_app._phase_var.set("—")
    root_app._rows_var.set("—")
    root_app._t_var.set("—")
    root_app._cmd_i_var.set("—")
    root_app._cmd_v_var.set("—")
    root_app._torque_var.set("—")
    root_app._psu_v_var.set("—")
    root_app._psu_i_var.set("—")
    root_app._abort_reason_var.set("")
    # Summary vars
    for var in (
        root_app._sum_motor_packets_var, root_app._sum_motor_errors_var,
        root_app._sum_motor_dropped_var,
        root_app._sum_sensor_rows_var, root_app._sum_sensor_malformed_var,
        root_app._sum_sensor_dropped_var,
        root_app._sum_psu_rows_var, root_app._sum_psu_malformed_var,
        root_app._sum_psu_dropped_var,
        root_app._sum_efficiency_var,
        root_app._sum_phase_dur_var, root_app._sum_peak_torque_var,
        root_app._sum_peak_current_var, root_app._sum_output_var,
    ):
        var.set("—")
    root_app._status_queue = None
    root_app._abort_event = None
    root_app._worker = None
    root_app._poll_id = None
    root_app._start_btn.config(state="disabled")
    root_app._abort_btn.config(state="disabled")
    return root_app


def _attach_queue(app: MonitoringApp) -> queue.Queue:
    """Wire a fresh Queue + Event onto app and return the queue."""
    q: queue.Queue = queue.Queue()
    app._status_queue = q
    app._abort_event = threading.Event()
    return q


# ---------------------------------------------------------------------------
# 1. Construction
# ---------------------------------------------------------------------------

class TestMonitoringAppInit:

    def test_constructs_without_error(self, app):
        assert app._status_var.get() == "Idle"

    def test_start_disabled_abort_disabled_initially(self, app):
        # Start is gated — disabled until all 5 paths populated
        assert str(app._start_btn["state"]) == "disabled"
        assert str(app._abort_btn["state"]) == "disabled"

    def test_motor_format_defaults_to_csv(self, app):
        assert app._motor_format.get() == "csv"


# ---------------------------------------------------------------------------
# 2. Telemetry — normal snapshots
# ---------------------------------------------------------------------------

class TestMonitoringAppTelemetry:

    def test_poll_applies_snapshot_to_stringvars(self, app):
        q = _attach_queue(app)
        q.put_nowait({
            "test_phase": "CURRENT_RAMP", "rows": 42, "timestamp_s": 1.23,
            "commanded_current_a": 5.0, "commanded_voltage_v": 24.0, "torque_nm": 100.0,
            "psu_voltage_v": 23.5, "psu_current_a": 1.1, "abort_reason": None,
        })
        app._poll()
        assert app._phase_var.get() == "CURRENT_RAMP"
        assert app._rows_var.get() == "42"
        assert "1.230" in app._t_var.get()
        assert "5.000" in app._cmd_i_var.get()
        assert "100.000" in app._torque_var.get()

    def test_poll_none_fields_show_dash(self, app):
        q = _attach_queue(app)
        q.put_nowait({
            "test_phase": "SETUP", "rows": 1, "timestamp_s": 0.001,
            "commanded_current_a": 0.0, "commanded_voltage_v": 24.0,
            "torque_nm": None, "psu_voltage_v": None, "psu_current_a": None,
            "abort_reason": None,
        })
        app._poll()
        assert app._torque_var.get() == "—"
        assert app._psu_v_var.get() == "—"
        assert app._psu_i_var.get() == "—"

    def test_poll_drains_all_items_applies_last(self, app):
        q = _attach_queue(app)
        q.put_nowait({
            "test_phase": "SETUP", "rows": 1, "timestamp_s": 0.0,
            "commanded_current_a": 0.0, "commanded_voltage_v": 24.0, "torque_nm": None,
            "psu_voltage_v": None, "psu_current_a": None, "abort_reason": None,
        })
        q.put_nowait({
            "test_phase": "CURRENT_RAMP", "rows": 99, "timestamp_s": 0.099,
            "commanded_current_a": 3.0, "commanded_voltage_v": 24.0, "torque_nm": 50.0,
            "psu_voltage_v": 23.9, "psu_current_a": 0.8, "abort_reason": None,
        })
        app._poll()
        assert app._phase_var.get() == "CURRENT_RAMP"
        assert app._rows_var.get() == "99"

    def test_poll_empty_queue_does_not_crash(self, app):
        _attach_queue(app)
        app._poll()  # empty queue — must not raise

    def test_poll_none_status_queue_returns_early(self, app):
        app._status_queue = None
        app._poll()  # must not raise


# ---------------------------------------------------------------------------
# 3. Done sentinel
# ---------------------------------------------------------------------------

class TestMonitoringAppDoneSentinel:

    def test_done_sentinel_status_complete(self, app):
        q = _attach_queue(app)
        q.put_nowait({"_done": True, "stats": {}, "rows": 100})
        app._poll()
        assert app._status_var.get() == "Complete"
        assert app._rows_var.get() == "100"
        assert str(app._start_btn["state"]) == "normal"
        assert str(app._abort_btn["state"]) == "disabled"

    def test_done_sentinel_with_abort_reason_shows_aborted(self, app):
        q = _attach_queue(app)
        q.put_nowait({
            "_done": True,
            "stats": {"abort_reason": "manual_abort_gui"},
            "rows": 50,
        })
        app._poll()
        assert app._status_var.get() == "Aborted"
        assert app._abort_reason_var.get() == "manual_abort_gui"

    def test_done_sentinel_does_not_reschedule_poll(self, app):
        q = _attach_queue(app)
        poll_id_before = app._poll_id  # None (reset by fixture)
        q.put_nowait({"_done": True, "stats": {}, "rows": 10})
        app._poll()
        assert app._poll_id == poll_id_before  # unchanged (no reschedule)


# ---------------------------------------------------------------------------
# 4. Abort button
# ---------------------------------------------------------------------------

class TestMonitoringAppAbort:

    def test_abort_sets_event(self, app):
        event = threading.Event()
        app._abort_event = event
        app._on_abort()
        assert event.is_set()

    def test_abort_with_none_event_does_not_raise(self, app):
        app._abort_event = None
        app._on_abort()  # must not raise


# ---------------------------------------------------------------------------
# 5. Window-close (WM_DELETE_WINDOW) handler
# — destroy() is patched to keep the shared Tk instance alive
# ---------------------------------------------------------------------------

class TestMonitoringAppClosing:

    def test_no_worker_closes_immediately(self, app):
        app._worker = None
        with patch.object(app, "destroy"):
            app._on_closing()  # must not raise

    def test_alive_worker_abort_and_join(self, app):
        event = threading.Event()
        app._abort_event = event
        join_calls: List[float] = []

        class FakeWorker:
            def is_alive(self) -> bool:
                return not join_calls  # alive until join is called
            def join(self, timeout: float = None) -> None:
                join_calls.append(timeout)

        app._worker = FakeWorker()
        with patch.object(app, "destroy"):
            app._on_closing()

        assert event.is_set()
        assert join_calls == [2.0]

    def test_wedged_worker_logs_warning(self, app, caplog):
        import logging
        event = threading.Event()
        app._abort_event = event

        class WedgedWorker:
            def is_alive(self) -> bool:
                return True  # never exits
            def join(self, timeout: float = None) -> None:
                pass  # returns immediately; worker stays "alive"

        app._worker = WedgedWorker()
        with caplog.at_level(logging.WARNING, logger="ui.gui"), \
             patch.object(app, "destroy"):
            app._on_closing()

        assert any("2 s" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. Start button gating
# ---------------------------------------------------------------------------

class TestStartButtonGating:

    def _set_all_paths(self, app: MonitoringApp) -> None:
        app._config_path.set("/a/config.yaml")
        app._motor_path.set("/a/motor.csv")
        app._sensor_path.set("/a/sensor.csv")
        app._psu_path.set("/a/psu.csv")
        app._output_path.set("/a/output.csv")

    def test_disabled_with_no_paths(self, app):
        app._check_ready()
        assert str(app._start_btn["state"]) == "disabled"

    def test_disabled_with_only_4_paths(self, app):
        app._config_path.set("/a/config.yaml")
        app._motor_path.set("/a/motor.csv")
        app._sensor_path.set("/a/sensor.csv")
        app._psu_path.set("/a/psu.csv")
        # output_path empty
        app._check_ready()
        assert str(app._start_btn["state"]) == "disabled"

    def test_enabled_with_all_5_paths(self, app):
        self._set_all_paths(app)
        app._check_ready()
        assert str(app._start_btn["state"]) == "normal"

    def test_disabled_after_clearing_one_path(self, app):
        self._set_all_paths(app)
        app._check_ready()
        assert str(app._start_btn["state"]) == "normal"
        app._output_path.set("")
        app._check_ready()
        assert str(app._start_btn["state"]) == "disabled"

    def test_each_missing_path_blocks_start(self, app):
        paths = [
            app._config_path, app._motor_path, app._sensor_path,
            app._psu_path, app._output_path,
        ]
        for skip_idx in range(len(paths)):
            for i, var in enumerate(paths):
                var.set("" if i == skip_idx else "/some/file")
            app._check_ready()
            assert str(app._start_btn["state"]) == "disabled", (
                f"Expected disabled when path[{skip_idx}] is empty"
            )


class TestStartButtonGatingBinaryFormat:
    """Protocol YAML is required only when motor format is binary."""

    def _set_csv_paths(self, app: MonitoringApp) -> None:
        app._config_path.set("/a/config.yaml")
        app._motor_path.set("/a/motor.bin")
        app._sensor_path.set("/a/sensor.csv")
        app._psu_path.set("/a/psu.csv")
        app._output_path.set("/a/output.csv")

    def test_binary_enabled_with_all_6_paths(self, app):
        app._motor_format.set("binary")
        self._set_csv_paths(app)
        app._protocol_path.set("/a/motor_protocol.yaml")
        app._check_ready()
        assert str(app._start_btn["state"]) == "normal"

    def test_binary_disabled_without_protocol_path(self, app):
        app._motor_format.set("binary")
        self._set_csv_paths(app)
        # protocol_path left empty
        app._check_ready()
        assert str(app._start_btn["state"]) == "disabled"

    def test_csv_enabled_without_protocol_path(self, app):
        app._motor_format.set("csv")
        self._set_csv_paths(app)
        # protocol_path intentionally empty — not required for csv
        app._check_ready()
        assert str(app._start_btn["state"]) == "normal"

    def test_binary_each_missing_path_blocks_start(self, app):
        app._motor_format.set("binary")
        paths = [
            app._config_path, app._motor_path, app._sensor_path,
            app._psu_path, app._output_path, app._protocol_path,
        ]
        for skip_idx in range(len(paths)):
            for i, var in enumerate(paths):
                var.set("" if i == skip_idx else "/some/file")
            app._check_ready()
            assert str(app._start_btn["state"]) == "disabled", (
                f"Expected disabled when binary path[{skip_idx}] is empty"
            )

    def test_switching_to_binary_re_evaluates_readiness(self, app):
        # CSV with 5 paths → enabled
        app._motor_format.set("csv")
        self._set_csv_paths(app)
        app._check_ready()
        assert str(app._start_btn["state"]) == "normal"

        # Switch to binary without protocol → disabled
        app._motor_format.set("binary")
        app._check_ready()
        assert str(app._start_btn["state"]) == "disabled"

        # Add protocol → re-enabled
        app._protocol_path.set("/a/motor_protocol.yaml")
        app._check_ready()
        assert str(app._start_btn["state"]) == "normal"


# ---------------------------------------------------------------------------
# 7. Speed control
# ---------------------------------------------------------------------------

class TestSpeedControl:

    def test_default_speed_is_max(self, app):
        assert app._speed_var.get() == "Max"

    def test_parse_max_returns_zero(self):
        assert MonitoringApp._parse_speed_multiplier("Max") == 0.0

    def test_parse_1x(self):
        assert MonitoringApp._parse_speed_multiplier("1x") == 1.0

    def test_parse_5x(self):
        assert MonitoringApp._parse_speed_multiplier("5x") == 5.0

    def test_parse_10x(self):
        assert MonitoringApp._parse_speed_multiplier("10x") == 10.0

    def test_parse_unknown_returns_zero(self):
        assert MonitoringApp._parse_speed_multiplier("bogus") == 0.0


# ---------------------------------------------------------------------------
# 8. Run Summary panel
# ---------------------------------------------------------------------------

def _full_sentinel(output_path: str = "") -> dict:
    return {
        "_done": True,
        "rows": 500,
        "stats": {
            "abort_reason": None,
            "end_phase": "COMPLETE",
            "rows_processed": 500,
            "peak_torque_nm": 150.1234,
            "peak_current_a": 24.5678,
            "phase_durations_s": {
                "SETUP": 0.0,
                "CURRENT_RAMP": 1.5,
                "TORQUE_HOLD": 3.0,
                "VOLTAGE_DECREASE": 0.8,
                "COMPLETE": 0.0,
            },
        },
        "motor_stats": {
            "total_packets": 1000, "checksum_errors": 3,
            "truncations": 1, "unknown_codes": 2,
        },
        "sensor_stats": {"total_rows": 4800, "malformed_rows": 2, "timestamp_gaps": 5},
        "psu_stats": {"total_rows": 50, "malformed_rows": 0, "timestamp_gaps": 1},
        "efficiency_mean": 0.8765,
        "efficiency_peak": 0.9321,
    }


class TestRunSummary:

    def test_motor_binary_stats_packets_and_errors(self, app):
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        assert app._sum_motor_packets_var.get() == "1000"
        assert app._sum_motor_errors_var.get() == "3"

    def test_motor_csv_stats_rows_and_malformed(self, app):
        sentinel = _full_sentinel()
        sentinel["motor_stats"] = {
            "total_rows": 1000, "malformed_rows": 2, "timestamp_gaps": 0
        }
        q = _attach_queue(app)
        q.put_nowait(sentinel)
        app._poll()
        assert app._sum_motor_packets_var.get() == "1000"
        assert app._sum_motor_errors_var.get() == "2"

    def test_sensor_rows_and_malformed(self, app):
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        assert app._sum_sensor_rows_var.get() == "4800"
        assert app._sum_sensor_malformed_var.get() == "2"

    def test_psu_rows_and_malformed(self, app):
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        assert app._sum_psu_rows_var.get() == "50"
        assert app._sum_psu_malformed_var.get() == "0"

    def test_peak_torque_formatted_2dp(self, app):
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        assert app._sum_peak_torque_var.get() == "150.12"

    def test_peak_current_formatted_2dp(self, app):
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        assert app._sum_peak_current_var.get() == "24.57"

    def test_phase_durations_nonzero_only(self, app):
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        dur = app._sum_phase_dur_var.get()
        assert "CURRENT_RAMP: 1.50s" in dur
        assert "TORQUE_HOLD: 3.00s" in dur
        assert "VOLTAGE_DECREASE: 0.80s" in dur
        assert "SETUP" not in dur      # 0.0 excluded
        assert "COMPLETE" not in dur   # 0.0 excluded

    def test_output_path_from_stringvar(self, app):
        app._output_path.set("/some/output.csv")
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        assert app._sum_output_var.get() == "/some/output.csv"

    def test_empty_motor_stats_keeps_dash(self, app):
        q = _attach_queue(app)
        q.put_nowait({"_done": True, "stats": {}, "rows": 0})
        app._poll()
        assert app._sum_motor_packets_var.get() == "—"
        assert app._sum_motor_errors_var.get() == "—"

    def test_missing_peak_values_show_dash(self, app):
        sentinel = _full_sentinel()
        sentinel["stats"].pop("peak_torque_nm")
        sentinel["stats"].pop("peak_current_a")
        q = _attach_queue(app)
        q.put_nowait(sentinel)
        app._poll()
        assert app._sum_peak_torque_var.get() == "—"
        assert app._sum_peak_current_var.get() == "—"

    def test_all_zero_phase_durations_shows_dash(self, app):
        sentinel = _full_sentinel()
        sentinel["stats"]["phase_durations_s"] = {
            "SETUP": 0.0, "CURRENT_RAMP": 0.0, "COMPLETE": 0.0
        }
        q = _attach_queue(app)
        q.put_nowait(sentinel)
        app._poll()
        assert app._sum_phase_dur_var.get() == "—"

    def test_motor_binary_dropped_sum(self, app):
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        # checksum_errors=3 + truncations=1 + unknown_codes=2 = 6
        assert app._sum_motor_dropped_var.get() == "6"

    def test_sensor_timestamp_gaps(self, app):
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        assert app._sum_sensor_dropped_var.get() == "5"

    def test_psu_timestamp_gaps(self, app):
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        assert app._sum_psu_dropped_var.get() == "1"

    def test_efficiency_mean_and_peak_formatted(self, app):
        q = _attach_queue(app)
        q.put_nowait(_full_sentinel())
        app._poll()
        val = app._sum_efficiency_var.get()
        assert "0.8765" in val
        assert "0.9321" in val

    def test_efficiency_none_shows_dash(self, app):
        sentinel = _full_sentinel()
        sentinel["efficiency_mean"] = None
        sentinel["efficiency_peak"] = None
        q = _attach_queue(app)
        q.put_nowait(sentinel)
        app._poll()
        assert app._sum_efficiency_var.get() == "—"

    def test_motor_csv_dropped_uses_malformed(self, app):
        sentinel = _full_sentinel()
        sentinel["motor_stats"] = {"total_rows": 1000, "malformed_rows": 7, "timestamp_gaps": 0}
        q = _attach_queue(app)
        q.put_nowait(sentinel)
        app._poll()
        assert app._sum_motor_dropped_var.get() == "7"
