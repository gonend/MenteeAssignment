from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _cfg_get(cfg: Dict[str, Any], *path: str) -> Any:
    """Walk nested dicts by key path; raise KeyError with full dotted path on miss."""
    d = cfg
    dotted = ".".join(path)
    for k in path:
        if not isinstance(d, dict) or k not in d:
            raise KeyError(f"Missing required config key: {dotted}")
        d = d[k]
    return d


@dataclass(frozen=True)
class PhaseConfig:
    name: str
    parameters: Dict[str, Any]


class StateMachine:
    """Push-model phase driver for the motor characterization test.

    Caller feeds rows one at a time via process(row).  Each call returns the
    same row dict extended with three keys: test_phase, commanded_current_a,
    commanded_voltage_v.  Handlers decide the next phase AND the commanded
    values for the current row; phase transitions take effect before values are
    frozen, so an abort row always emits COMPLETE-frozen values.

    Thread safety: request_abort() may be called from any thread.  It sets a
    threading.Event that process() checks at the top of every call.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self._parse_config(config)

        # Abort is set by the GUI thread; polled by the pipeline thread each row.
        self._abort_event = threading.Event()
        self._manual_abort_reason: Optional[str] = None
        self._abort_reason: Optional[str] = None

        # Start in the first YAML-declared phase (expected to be SETUP).
        self._phase_name: str = self._phase_names[0]
        self._t_phase_entry: Optional[float] = None  # set on first _enter_phase call

        # Snapshot of commanded current captured at CURRENT_RAMP → TORQUE_HOLD exit.
        self._held_current_a: float = 0.0

        # Last row's commanded values; frozen by COMPLETE and abort/safety branches.
        self._last_commanded_current_a: float = 0.0
        self._last_commanded_voltage_v: float = self._initial_voltage_v

        self._stats: Dict[str, Any] = {
            "samples_per_phase": {name: 0 for name in self._phase_names},
            "transitions": [],       # list of (from_phase, to_phase, timestamp_s)
            "abort_reason": None,
            "end_phase": self._phase_name,
            "rows_processed": 0,
        }

        # Dispatch by phase name, never by YAML list index — reordering YAML is safe.
        self._handlers: Dict[
            str, Callable[[Dict[str, Any], float], Tuple[str, float, float]]
        ] = {
            "SETUP":            self._handle_setup,
            "CURRENT_RAMP":     self._handle_current_ramp,
            "TORQUE_HOLD":      self._handle_torque_hold,
            "VOLTAGE_DECREASE": self._handle_voltage_decrease,
            "COMPLETE":         self._handle_complete,
        }
        # Fail fast: surface new YAML phases that have no handler at startup,
        # not silently at runtime mid-test.
        for name in self._phase_names:
            if name not in self._handlers:
                raise ValueError(
                    f"No handler for YAML phase '{name}'. "
                    f"Registered: {sorted(self._handlers)}"
                )

        logger.info(
            "StateMachine ready | phases=%s | ramp_rate=%.2f A/s | "
            "hold=%.1f s | safety_torque=%.1f Nm | initial_voltage=%.1f V",
            self._phase_names,
            self._ramp_rate_a_per_s,
            self._hold_duration_s,
            self._safety_max_torque_nm,
            self._initial_voltage_v,
        )

    # ------------------------------------------------------------------
    # Config parsing
    # ------------------------------------------------------------------

    def _parse_config(self, config: Dict[str, Any]) -> None:
        """Extract and validate all YAML-driven parameters.  Raises KeyError with
        the full dotted config path on any missing field so the caller knows
        exactly what to fix."""
        phases_list = _cfg_get(config, "test", "phases")
        self._phase_names: Tuple[str, ...] = tuple(p["name"] for p in phases_list)
        self._phase_configs: Dict[str, Dict[str, Any]] = {
            p["name"]: p.get("parameters", {}) for p in phases_list
        }

        # Inner helper avoids repeating the dotted-path error construction.
        def _req(phase: str, param: str) -> Any:
            params = self._phase_configs.get(phase, {})
            if param not in params:
                raise KeyError(
                    f"Missing required config key: "
                    f"test.phases[{phase}].parameters.{param}"
                )
            return params[param]

        self._max_current_a_param: float = float(_req("CURRENT_RAMP", "max_current_a"))
        self._ramp_duration_s: float     = float(_req("CURRENT_RAMP", "ramp_duration_s"))
        self._target_torque_nm: float    = float(_req("CURRENT_RAMP", "target_torque_nm"))
        # Derived: ramp_rate = max_current / ramp_duration (YAML comment confirms this).
        self._ramp_rate_a_per_s: float = self._max_current_a_param / self._ramp_duration_s

        self._hold_duration_s: float = float(_req("TORQUE_HOLD", "hold_duration_s"))

        self._voltage_decrease_rate_v_per_s: float = float(
            _req("VOLTAGE_DECREASE", "voltage_decrease_rate_v_per_s")
        )
        self._min_voltage_v: float = float(_req("VOLTAGE_DECREASE", "min_voltage_v"))

        safety = _cfg_get(config, "test", "safety")
        for key in ("max_torque_nm", "max_current_a"):
            if key not in safety:
                raise KeyError(f"Missing required config key: test.safety.{key}")
        self._safety_max_torque_nm: float = float(safety["max_torque_nm"])

        self._initial_voltage_v: float = float(
            _cfg_get(config, "power_supply", "initial_voltage_v")
        )

    # ------------------------------------------------------------------
    # Phase entry
    # ------------------------------------------------------------------

    def _enter_phase(self, name: str, t: float) -> None:
        """Transition to a new phase: record timestamp, update stats, log."""
        if name == self._phase_name:
            return
        logger.info(
            "Phase transition: %s -> %s  (t=%.6f s)",
            self._phase_name, name, t,
        )
        self._stats["transitions"].append((self._phase_name, name, t))
        self._phase_name = name
        self._t_phase_entry = t
        self._stats["end_phase"] = name

    # ------------------------------------------------------------------
    # Handlers
    # Each returns (next_phase_name, commanded_current_a, commanded_voltage_v).
    # Returning the current phase name means "stay in this phase".
    # The handler runs BEFORE _enter_phase so _t_phase_entry still points to the
    # current phase's entry time — elapsed-time math is always correct.
    # ------------------------------------------------------------------

    def _handle_setup(
        self, row: Dict[str, Any], t: float
    ) -> Tuple[str, float, float]:
        """SETUP exits immediately on the first row; no data consumed here."""
        return "CURRENT_RAMP", 0.0, self._initial_voltage_v

    def _handle_current_ramp(
        self, row: Dict[str, Any], t: float
    ) -> Tuple[str, float, float]:
        """Ramp commanded current linearly; transition on torque OR current threshold."""
        # _t_phase_entry is set by _enter_phase when we entered CURRENT_RAMP;
        # the fallback to t keeps the formula safe against any init edge case.
        t_entry = self._t_phase_entry if self._t_phase_entry is not None else t
        t_elapsed = t - t_entry
        cmd_i = min(self._ramp_rate_a_per_s * t_elapsed, self._max_current_a_param)
        cmd_v = self._initial_voltage_v

        torque  = row.get("torque_nm")          # None during sensor bootstrap window
        current = row.get("measured_current_a") or 0.0  # always a motor field; or 0 guards None

        # torque_trip skipped when torque is None — cannot abort on missing data.
        torque_trip   = torque is not None and abs(torque) >= self._target_torque_nm
        current_trip  = abs(current) >= self._max_current_a_param

        if torque_trip or current_trip:
            reason = "torque" if torque_trip else "current"
            logger.info(
                "CURRENT_RAMP -> TORQUE_HOLD | reason=%s | "
                "torque=%.2f Nm | current=%.2f A | cmd_i=%.3f A",
                reason,
                torque if torque is not None else float("nan"),
                current,
                cmd_i,
            )
            # Snapshot commanded current at the exact moment of exit so TORQUE_HOLD
            # holds the ramp's endpoint, not a stale or zero value.
            self._held_current_a = cmd_i
            return "TORQUE_HOLD", cmd_i, cmd_v
        return "CURRENT_RAMP", cmd_i, cmd_v

    def _handle_torque_hold(
        self, row: Dict[str, Any], t: float
    ) -> Tuple[str, float, float]:
        """Hold current constant; transition after hold_duration_s of data time."""
        t_entry   = self._t_phase_entry if self._t_phase_entry is not None else t
        t_elapsed = t - t_entry  # driven by data timestamps, not wall clock
        cmd_i = self._held_current_a
        cmd_v = self._initial_voltage_v

        if t_elapsed >= self._hold_duration_s:
            logger.info(
                "TORQUE_HOLD -> VOLTAGE_DECREASE | elapsed=%.3f s | cmd_i=%.3f A",
                t_elapsed, cmd_i,
            )
            return "VOLTAGE_DECREASE", cmd_i, cmd_v
        return "TORQUE_HOLD", cmd_i, cmd_v

    def _handle_voltage_decrease(
        self, row: Dict[str, Any], t: float
    ) -> Tuple[str, float, float]:
        """Decrease commanded voltage linearly; transition when measured voltage hits min."""
        t_entry   = self._t_phase_entry if self._t_phase_entry is not None else t
        t_elapsed = t - t_entry
        cmd_i = self._held_current_a
        cmd_v = max(
            self._initial_voltage_v - self._voltage_decrease_rate_v_per_s * t_elapsed,
            self._min_voltage_v,
        )

        # Transition on MEASURED voltage (PSU reading), not commanded voltage.
        # If voltage_v is None (no PSU prior yet), skip — cannot abort on missing data.
        voltage = row.get("voltage_v")
        if voltage is not None and voltage <= self._min_voltage_v:
            logger.info(
                "VOLTAGE_DECREASE -> COMPLETE | measured_v=%.3f V (<=%.3f V min)",
                voltage, self._min_voltage_v,
            )
            return "COMPLETE", cmd_i, cmd_v
        return "VOLTAGE_DECREASE", cmd_i, cmd_v

    def _handle_complete(
        self, row: Dict[str, Any], t: float
    ) -> Tuple[str, float, float]:
        """Terminal phase — freeze commanded values from the last non-COMPLETE row."""
        return "COMPLETE", self._last_commanded_current_a, self._last_commanded_voltage_v

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Process one merged row and return it augmented with phase + commanded values.

        Evaluation priority (immutable — do NOT reorder):
          1. Manual abort  — GUI button; beats everything.
          2. Safety torque — hardware limit; beats phase logic.
          3. Phase handler — normal transition logic.

        Commanded values are computed after the phase decision is final so that
        an aborted row always emits COMPLETE-frozen values rather than stale
        ramp or voltage-decrease math.
        """
        t: float = row["timestamp_s"]
        torque = row.get("torque_nm")

        # Priority 1: manual abort (GUI thread sets the event; safe to poll here).
        if self._abort_event.is_set() and self._phase_name != "COMPLETE":
            logger.warning(
                "Manual abort received | reason=%s | phase=%s | t=%.6f s",
                self._manual_abort_reason, self._phase_name, t,
            )
            self._abort_reason = self._manual_abort_reason
            self._stats["abort_reason"] = self._abort_reason
            self._enter_phase("COMPLETE", t)
            # Freeze: return last row's commanded values, not stale phase math.
            cmd_i = self._last_commanded_current_a
            cmd_v = self._last_commanded_voltage_v

        # Priority 2: safety — strict ">" so exactly-at-limit rows go to phase logic.
        elif (
            self._phase_name != "COMPLETE"
            and torque is not None
            and abs(torque) > self._safety_max_torque_nm
        ):
            logger.warning(
                "Safety abort: |torque|=%.2f Nm > %.2f Nm limit | phase=%s | t=%.6f s",
                abs(torque), self._safety_max_torque_nm, self._phase_name, t,
            )
            self._abort_reason = "safety_torque_exceeded"
            self._stats["abort_reason"] = self._abort_reason
            self._enter_phase("COMPLETE", t)
            cmd_i = self._last_commanded_current_a
            cmd_v = self._last_commanded_voltage_v

        # Priority 3: normal phase dispatch.
        else:
            next_phase, cmd_i, cmd_v = self._handlers[self._phase_name](row, t)
            if next_phase != self._phase_name:
                self._enter_phase(next_phase, t)

        # Update freeze buffer so the NEXT abort/COMPLETE row can emit these values.
        self._last_commanded_current_a = cmd_i
        self._last_commanded_voltage_v = cmd_v

        result = dict(row)
        result["test_phase"]          = self._phase_name
        result["commanded_current_a"] = cmd_i
        result["commanded_voltage_v"] = cmd_v

        self._stats["rows_processed"] += 1
        self._stats["samples_per_phase"][self._phase_name] += 1

        logger.debug(
            "row t=%.6f | phase=%-16s | cmd_i=%6.3f A | cmd_v=%5.2f V",
            t, self._phase_name, cmd_i, cmd_v,
        )
        return result

    def request_abort(self, reason: str = "manual_abort") -> None:
        """Signal an immediate stop.  Thread-safe; may be called from GUI thread."""
        self._manual_abort_reason = reason
        self._abort_event.set()

    def get_stats(self) -> Dict[str, Any]:
        """Return a deep copy — mutations to the returned dict do not affect internal state."""
        return copy.deepcopy(self._stats)

    @property
    def current_phase(self) -> str:
        return self._phase_name

    @property
    def is_complete(self) -> bool:
        return self._phase_name == "COMPLETE"
