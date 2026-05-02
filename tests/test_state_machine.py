"""Tests for automation/state_machine.py."""
from __future__ import annotations

import copy
import threading
from typing import Any, Dict, Optional

import pytest

from automation.state_machine import StateMachine

# ---------------------------------------------------------------------------
# Shared config fixture (mirrors test_config.yaml structure)
# ---------------------------------------------------------------------------

MINIMAL_CONFIG: Dict[str, Any] = {
    "test": {
        "phases": [
            {"name": "SETUP"},
            {
                "name": "CURRENT_RAMP",
                "parameters": {
                    "max_current_a": 34.0,
                    "ramp_duration_s": 10.0,
                    "target_torque_nm": 150.0,
                },
            },
            {"name": "TORQUE_HOLD", "parameters": {"hold_duration_s": 10.0}},
            {
                "name": "VOLTAGE_DECREASE",
                "parameters": {
                    "voltage_decrease_rate_v_per_s": 1.0,
                    "min_voltage_v": 0.0,
                },
            },
            {"name": "COMPLETE"},
        ],
        "safety": {
            "max_torque_nm": 200.0,
            "max_current_a": 34.0,
        },
    },
    "power_supply": {"initial_voltage_v": 24.0},
}


def _make_sm() -> StateMachine:
    return StateMachine(copy.deepcopy(MINIMAL_CONFIG))


def _row(
    t: float,
    vel: float = 0.0,
    current: float = 0.0,
    torque: Optional[float] = None,
    voltage: Optional[float] = None,
) -> Dict[str, Any]:
    r: Dict[str, Any] = {
        "timestamp_s": t,
        "velocity_rad_s": vel,
        "measured_current_a": current,
    }
    if torque is not None:
        r["torque_nm"] = torque
    if voltage is not None:
        r["voltage_v"] = voltage
    return r


def _advance_to_ramp(sm: StateMachine, t_start: float = 0.001) -> float:
    """Feed one row so SETUP → CURRENT_RAMP. Returns t used."""
    sm.process(_row(t_start))
    return t_start


def _advance_to_hold(sm: StateMachine) -> float:
    """Drive SETUP → CURRENT_RAMP → TORQUE_HOLD via torque trip. Returns t of hold entry."""
    _advance_to_ramp(sm, t_start=0.001)
    t = 0.002
    sm.process(_row(t, torque=150.0))  # torque >= target_torque_nm
    return t


def _advance_to_vd(sm: StateMachine) -> float:
    """Drive up to VOLTAGE_DECREASE. Returns t of VD entry."""
    t_hold = _advance_to_hold(sm)
    t_vd = t_hold + 10.0  # hold_duration_s elapsed
    sm.process(_row(t_vd))
    return t_vd


# ---------------------------------------------------------------------------
# 1. Initialisation
# ---------------------------------------------------------------------------


class TestStateMachineInit:
    def test_initial_phase_is_setup(self):
        sm = _make_sm()
        assert sm.current_phase == "SETUP"

    def test_is_not_complete_at_init(self):
        assert not _make_sm().is_complete

    def test_derived_ramp_rate_correct(self):
        sm = _make_sm()
        assert sm._ramp_rate_a_per_s == pytest.approx(34.0 / 10.0)

    def test_missing_top_level_key_raises_with_path(self):
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        del cfg["power_supply"]
        with pytest.raises(KeyError, match="power_supply"):
            StateMachine(cfg)

    def test_missing_safety_key_raises_with_path(self):
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        del cfg["test"]["safety"]["max_torque_nm"]
        with pytest.raises(KeyError, match="max_torque_nm"):
            StateMachine(cfg)

    def test_missing_ramp_param_raises_with_path(self):
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        del cfg["test"]["phases"][1]["parameters"]["max_current_a"]
        with pytest.raises(KeyError, match="CURRENT_RAMP"):
            StateMachine(cfg)

    def test_missing_hold_param_raises_with_path(self):
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        del cfg["test"]["phases"][2]["parameters"]["hold_duration_s"]
        with pytest.raises(KeyError, match="TORQUE_HOLD"):
            StateMachine(cfg)

    def test_missing_vd_param_raises_with_path(self):
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        del cfg["test"]["phases"][3]["parameters"]["min_voltage_v"]
        with pytest.raises(KeyError, match="VOLTAGE_DECREASE"):
            StateMachine(cfg)


# ---------------------------------------------------------------------------
# 2. SETUP transition
# ---------------------------------------------------------------------------


class TestStateMachineSetupTransition:
    def test_first_row_transitions_to_current_ramp(self):
        sm = _make_sm()
        sm.process(_row(0.001))
        assert sm.current_phase == "CURRENT_RAMP"

    def test_first_row_commanded_current_is_zero(self):
        sm = _make_sm()
        result = sm.process(_row(0.001))
        assert result["commanded_current_a"] == pytest.approx(0.0)

    def test_first_row_commanded_voltage_is_initial(self):
        sm = _make_sm()
        result = sm.process(_row(0.001))
        assert result["commanded_voltage_v"] == pytest.approx(24.0)

    def test_first_row_test_phase_field_is_current_ramp(self):
        sm = _make_sm()
        result = sm.process(_row(0.001))
        assert result["test_phase"] == "CURRENT_RAMP"

    def test_setup_sample_counted_under_current_ramp(self):
        sm = _make_sm()
        sm.process(_row(0.001))
        stats = sm.get_stats()
        assert stats["samples_per_phase"]["SETUP"] == 0
        assert stats["samples_per_phase"]["CURRENT_RAMP"] == 1


# ---------------------------------------------------------------------------
# 3. CURRENT_RAMP
# ---------------------------------------------------------------------------


class TestStateMachineCurrentRamp:
    def test_ramp_formula_at_elapsed_time(self):
        sm = _make_sm()
        t0 = 1.0
        sm.process(_row(t0))  # enters CURRENT_RAMP at t0
        t1 = t0 + 2.0
        result = sm.process(_row(t1))
        expected = 3.4 * 2.0  # rate=3.4 A/s, elapsed=2s
        assert result["commanded_current_a"] == pytest.approx(expected)

    def test_ramp_clamped_at_max_current(self):
        sm = _make_sm()
        sm.process(_row(0.0))  # enters RAMP
        result = sm.process(_row(100.0))  # 100s elapsed → would be 340 A
        assert result["commanded_current_a"] == pytest.approx(34.0)

    def test_transition_on_torque_threshold(self):
        sm = _make_sm()
        sm.process(_row(0.001))
        result = sm.process(_row(0.002, torque=150.0))
        assert result["test_phase"] == "TORQUE_HOLD"
        assert sm.current_phase == "TORQUE_HOLD"

    def test_transition_on_current_threshold(self):
        sm = _make_sm()
        sm.process(_row(0.001))
        result = sm.process(_row(0.002, current=34.0))
        assert result["test_phase"] == "TORQUE_HOLD"

    def test_no_transition_below_thresholds(self):
        sm = _make_sm()
        sm.process(_row(0.001))
        result = sm.process(_row(0.002, torque=100.0, current=10.0))
        assert result["test_phase"] == "CURRENT_RAMP"

    def test_held_current_snapshot_on_exit(self):
        sm = _make_sm()
        t0 = 1.0
        sm.process(_row(t0))  # enters RAMP at t0
        t1 = t0 + 2.0
        sm.process(_row(t1, torque=150.0))  # triggers HOLD
        expected_held = min(3.4 * 2.0, 34.0)
        assert sm._held_current_a == pytest.approx(expected_held)

    def test_torque_exactly_at_target_triggers_transition(self):
        sm = _make_sm()
        sm.process(_row(0.001))
        result = sm.process(_row(0.002, torque=150.0))
        assert sm.current_phase == "TORQUE_HOLD"
        # torque just below target — should NOT transition
        sm2 = _make_sm()
        sm2.process(_row(0.001))
        result2 = sm2.process(_row(0.002, torque=149.999))
        assert sm2.current_phase == "CURRENT_RAMP"


# ---------------------------------------------------------------------------
# 4. TORQUE_HOLD
# ---------------------------------------------------------------------------


class TestStateMachineTorqueHold:
    def test_commanded_current_is_held_value(self):
        sm = _make_sm()
        t_hold = _advance_to_hold(sm)
        result = sm.process(_row(t_hold + 1.0))
        assert result["commanded_current_a"] == pytest.approx(sm._held_current_a)

    def test_commanded_voltage_is_initial_during_hold(self):
        sm = _make_sm()
        t_hold = _advance_to_hold(sm)
        result = sm.process(_row(t_hold + 1.0))
        assert result["commanded_voltage_v"] == pytest.approx(24.0)

    def test_no_transition_before_hold_duration(self):
        sm = _make_sm()
        t_hold = _advance_to_hold(sm)
        result = sm.process(_row(t_hold + 9.999))
        assert result["test_phase"] == "TORQUE_HOLD"

    def test_transition_exactly_at_hold_duration(self):
        sm = _make_sm()
        t_hold = _advance_to_hold(sm)
        result = sm.process(_row(t_hold + 10.0))
        assert result["test_phase"] == "VOLTAGE_DECREASE"
        assert sm.current_phase == "VOLTAGE_DECREASE"

    def test_held_current_constant_across_hold_rows(self):
        sm = _make_sm()
        t_hold = _advance_to_hold(sm)
        held = sm._held_current_a
        for i in range(5):
            result = sm.process(_row(t_hold + i * 1.0))
            assert result["commanded_current_a"] == pytest.approx(held)


# ---------------------------------------------------------------------------
# 5. VOLTAGE_DECREASE
# ---------------------------------------------------------------------------


class TestStateMachineVoltageDecrease:
    def test_commanded_voltage_decreases_linearly(self):
        sm = _make_sm()
        t_vd = _advance_to_vd(sm)
        result = sm.process(_row(t_vd + 3.0))
        expected = max(24.0 - 1.0 * 3.0, 0.0)
        assert result["commanded_voltage_v"] == pytest.approx(expected)

    def test_commanded_voltage_floored_at_min(self):
        sm = _make_sm()
        t_vd = _advance_to_vd(sm)
        result = sm.process(_row(t_vd + 9999.0))
        assert result["commanded_voltage_v"] == pytest.approx(0.0)

    def test_commanded_current_is_held_during_vd(self):
        sm = _make_sm()
        t_vd = _advance_to_vd(sm)
        held = sm._held_current_a
        result = sm.process(_row(t_vd + 1.0))
        assert result["commanded_current_a"] == pytest.approx(held)

    def test_transition_when_measured_voltage_at_min(self):
        sm = _make_sm()
        t_vd = _advance_to_vd(sm)
        result = sm.process(_row(t_vd + 1.0, voltage=0.0))
        assert result["test_phase"] == "COMPLETE"
        assert sm.current_phase == "COMPLETE"

    def test_no_transition_when_voltage_above_min(self):
        sm = _make_sm()
        t_vd = _advance_to_vd(sm)
        result = sm.process(_row(t_vd + 1.0, voltage=0.1))
        assert result["test_phase"] == "VOLTAGE_DECREASE"

    def test_no_transition_when_voltage_absent(self):
        sm = _make_sm()
        t_vd = _advance_to_vd(sm)
        result = sm.process(_row(t_vd + 1.0))  # no voltage key
        assert result["test_phase"] == "VOLTAGE_DECREASE"


# ---------------------------------------------------------------------------
# 6. Safety
# ---------------------------------------------------------------------------


class TestStateMachineSafety:
    def test_safety_from_ramp_triggers_complete(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        result = sm.process(_row(0.002, torque=200.1))
        assert result["test_phase"] == "COMPLETE"
        assert sm.is_complete

    def test_safety_abort_reason_set(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        sm.process(_row(0.002, torque=200.1))
        assert sm.get_stats()["abort_reason"] == "safety_torque_exceeded"

    def test_safety_from_setup_freezes_at_zero_current(self):
        sm = _make_sm()
        result = sm.process(_row(0.001, torque=200.1))
        assert result["commanded_current_a"] == pytest.approx(0.0)
        assert result["commanded_voltage_v"] == pytest.approx(24.0)
        assert result["test_phase"] == "COMPLETE"

    def test_safety_from_hold_triggers_complete(self):
        sm = _make_sm()
        _advance_to_hold(sm)
        result = sm.process(_row(5.0, torque=250.0))
        assert sm.is_complete

    def test_safety_exactly_at_limit_does_not_trigger(self):
        # safety fires on abs(torque) > max_torque_nm (strict).
        # torque=200.0 equals max_torque_nm so safety does NOT fire → no COMPLETE abort.
        # RAMP torque-trip (>=150.0) DOES fire → TORQUE_HOLD, not COMPLETE.
        sm = _make_sm()
        _advance_to_ramp(sm)
        result = sm.process(_row(0.002, torque=200.0))
        assert result["test_phase"] != "COMPLETE"
        assert sm.get_stats()["abort_reason"] is None

    def test_complete_rows_do_not_re_trigger_safety(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        sm.process(_row(0.002, torque=200.1))
        result = sm.process(_row(0.003, torque=999.0))
        assert result["test_phase"] == "COMPLETE"
        assert sm.get_stats()["abort_reason"] == "safety_torque_exceeded"


# ---------------------------------------------------------------------------
# 7. Manual abort
# ---------------------------------------------------------------------------


class TestStateMachineManualAbort:
    def test_request_abort_transitions_to_complete(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        sm.request_abort("manual_abort")
        result = sm.process(_row(0.002))
        assert result["test_phase"] == "COMPLETE"
        assert sm.is_complete

    def test_abort_reason_recorded(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        sm.request_abort("user_stop")
        sm.process(_row(0.002))
        assert sm.get_stats()["abort_reason"] == "user_stop"

    def test_manual_abort_priority_over_safety(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        sm.request_abort("manual_abort")
        result = sm.process(_row(0.002, torque=999.0))  # both abort and safety
        assert sm.get_stats()["abort_reason"] == "manual_abort"

    def test_abort_from_another_thread(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        barrier = threading.Event()

        def _abort():
            barrier.wait()
            sm.request_abort("thread_abort")

        t = threading.Thread(target=_abort)
        t.start()
        barrier.set()
        t.join()
        result = sm.process(_row(0.002))
        assert result["test_phase"] == "COMPLETE"

    def test_frozen_commanded_values_after_abort(self):
        sm = _make_sm()
        sm.process(_row(0.001))  # enters RAMP
        last = sm.process(_row(0.002))  # a normal ramp row
        prev_cmd_i = last["commanded_current_a"]
        prev_cmd_v = last["commanded_voltage_v"]
        sm.request_abort()
        aborted = sm.process(_row(0.003))
        assert aborted["commanded_current_a"] == pytest.approx(prev_cmd_i)
        assert aborted["commanded_voltage_v"] == pytest.approx(prev_cmd_v)


# ---------------------------------------------------------------------------
# 8. Dispatch invariants
# ---------------------------------------------------------------------------


class TestStateMachineDispatchInvariants:
    def test_handler_exists_for_every_yaml_phase(self):
        sm = _make_sm()
        for name in sm._phase_names:
            assert name in sm._handlers

    def test_unknown_yaml_phase_raises_value_error(self):
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        cfg["test"]["phases"].append({"name": "MYSTERY_PHASE"})
        with pytest.raises(ValueError, match="MYSTERY_PHASE"):
            StateMachine(cfg)

    def test_reordering_cr_and_th_does_not_break_dispatch(self):
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        phases = cfg["test"]["phases"]
        idx_cr = next(i for i, p in enumerate(phases) if p["name"] == "CURRENT_RAMP")
        idx_th = next(i for i, p in enumerate(phases) if p["name"] == "TORQUE_HOLD")
        phases[idx_cr], phases[idx_th] = phases[idx_th], phases[idx_cr]
        # SETUP is still first; transitions are name-based
        sm = StateMachine(cfg)
        assert sm.current_phase == "SETUP"
        sm.process(_row(0.001))
        assert sm.current_phase == "CURRENT_RAMP"  # still routes correctly

    def test_complete_is_terminal(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        sm.request_abort()
        sm.process(_row(0.002))
        assert sm.is_complete
        sm.process(_row(0.003))
        assert sm.is_complete


# ---------------------------------------------------------------------------
# 9. None guards
# ---------------------------------------------------------------------------


class TestStateMachineNoneGuards:
    def test_torque_none_does_not_trigger_ramp_transition(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        for i in range(5):
            result = sm.process(_row(0.002 + i * 0.001))  # no torque key
        assert result["test_phase"] == "CURRENT_RAMP"

    def test_torque_none_does_not_trigger_safety(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        result = sm.process(_row(0.002))  # torque absent
        assert result["test_phase"] == "CURRENT_RAMP"

    def test_voltage_none_does_not_trigger_vd_complete(self):
        sm = _make_sm()
        t_vd = _advance_to_vd(sm)
        result = sm.process(_row(t_vd + 1.0))  # voltage absent
        assert result["test_phase"] == "VOLTAGE_DECREASE"

    def test_bootstrap_with_all_optional_none_no_exception(self):
        sm = _make_sm()
        sm.process(_row(0.001))  # enters RAMP
        for i in range(100):
            result = sm.process(_row(0.002 + i * 0.001))
        assert result["test_phase"] == "CURRENT_RAMP"


# ---------------------------------------------------------------------------
# 10. End of data
# ---------------------------------------------------------------------------


class TestStateMachineEndOfData:
    def test_end_phase_reflects_last_active_when_stopped_in_ramp(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        sm.process(_row(0.002))
        sm.process(_row(0.003))
        assert sm.get_stats()["end_phase"] == "CURRENT_RAMP"

    def test_is_complete_false_when_stopped_before_complete(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        assert not sm.is_complete

    def test_end_phase_is_setup_when_no_rows_processed(self):
        sm = _make_sm()
        assert sm.get_stats()["end_phase"] == "SETUP"

    def test_end_phase_updates_on_each_transition(self):
        sm = _make_sm()
        _advance_to_hold(sm)
        assert sm.get_stats()["end_phase"] == "TORQUE_HOLD"


# ---------------------------------------------------------------------------
# 11. Stats
# ---------------------------------------------------------------------------


class TestStateMachineStats:
    def test_samples_per_phase_total_equals_rows_processed(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        for i in range(10):
            sm.process(_row(0.002 + i * 0.001))
        stats = sm.get_stats()
        total = sum(stats["samples_per_phase"].values())
        assert total == stats["rows_processed"]

    def test_transitions_ordered_by_occurrence(self):
        sm = _make_sm()
        t_hold = _advance_to_hold(sm)
        sm.process(_row(t_hold + 10.0))
        transitions = sm.get_stats()["transitions"]
        phase_seq = [t[0] for t in transitions]
        assert phase_seq == ["SETUP", "CURRENT_RAMP", "TORQUE_HOLD"]

    def test_transitions_contain_correct_timestamps(self):
        sm = _make_sm()
        sm.process(_row(0.001))  # SETUP → CURRENT_RAMP at t=0.001
        ts = sm.get_stats()["transitions"][0][2]
        assert ts == pytest.approx(0.001)

    def test_get_stats_returns_copy(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        stats = sm.get_stats()
        stats["rows_processed"] = 9999
        stats["samples_per_phase"]["CURRENT_RAMP"] = 9999
        assert sm.get_stats()["rows_processed"] != 9999

    def test_abort_reason_none_without_abort(self):
        sm = _make_sm()
        _advance_to_ramp(sm)
        sm.process(_row(0.002))
        assert sm.get_stats()["abort_reason"] is None

    def test_rows_processed_increments_each_call(self):
        sm = _make_sm()
        for i in range(7):
            sm.process(_row(0.001 + i * 0.001))
        assert sm.get_stats()["rows_processed"] == 7
