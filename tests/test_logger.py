"""Tests for automation/logger.py

Coverage:
  - Init: column loading, formula compilation, header written
  - Field translation: internal → output key rename
  - Null handling: None → '' (never writes literal "None")
  - Efficiency: correct value, div-by-zero, None inputs
  - Column control: YAML drives order/inclusion; extras dropped
  - Context manager: __enter__/__exit__ contract
  - Integration: multi-row write, output parseable by DictReader
"""
from __future__ import annotations

import csv
import io
from typing import Any, Dict, List

import pytest

from automation.logger import Logger, _FIELD_RENAME

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_BASE_COLUMNS = [
    "timestamp_s",
    "velocity_rad_s",
    "motor_current_a",
    "torque_nm",
    "psu_voltage_v",
    "psu_current_a",
    "commanded_current_a",
    "commanded_voltage_v",
    "test_phase",
]

_EFF_FORMULA = "(torque_nm * velocity_rad_s) / (psu_voltage_v * psu_current_a)"


def _config(*, with_efficiency: bool = True, columns: List[str] | None = None) -> Dict[str, Any]:
    cols = columns if columns is not None else list(_BASE_COLUMNS)
    cfg: Dict[str, Any] = {"output": {"columns": cols}}
    if with_efficiency:
        cfg["output"]["efficiency"] = {"formula": _EFF_FORMULA}
    return cfg


def _buf() -> io.StringIO:
    return io.StringIO()


def _full_row(**overrides: Any) -> Dict[str, Any]:
    """Return a complete merged row as the pipeline produces it (internal key names)."""
    base = {
        "timestamp_s":          1.0,
        "velocity_rad_s":       50.0,
        "measured_current_a":   10.0,   # → motor_current_a
        "torque_nm":            120.0,
        "voltage_v":            24.0,   # → psu_voltage_v
        "current_a":            2.0,    # → psu_current_a
        "commanded_current_a":  10.0,
        "commanded_voltage_v":  24.0,
        "test_phase":           "TORQUE_HOLD",
    }
    base.update(overrides)
    return base


def _parse(buf: io.StringIO) -> List[Dict[str, str]]:
    """Re-parse output buffer as CSV; return list of row dicts."""
    buf.seek(0)
    return list(csv.DictReader(buf))


# ---------------------------------------------------------------------------
# TestLoggerInit
# ---------------------------------------------------------------------------

class TestLoggerInit:
    def test_columns_loaded_from_yaml(self):
        log = Logger(_config(), _buf())
        assert log._columns == tuple(_BASE_COLUMNS)

    def test_header_written_on_init(self):
        buf = _buf()
        Logger(_config(), buf)
        buf.seek(0)
        header = buf.readline().rstrip("\n")
        assert header == ",".join(_BASE_COLUMNS)

    def test_formula_compiled_when_section_present(self):
        log = Logger(_config(with_efficiency=True), _buf())
        assert log._compiled_formula is not None

    def test_no_formula_when_efficiency_section_absent(self):
        log = Logger(_config(with_efficiency=False), _buf())
        assert log._compiled_formula is None

    def test_rows_written_starts_at_zero(self):
        log = Logger(_config(), _buf())
        assert log.rows_written == 0

    def test_missing_output_key_raises(self):
        with pytest.raises(KeyError, match="output"):
            Logger({"not_output": {}}, _buf())

    def test_custom_column_subset(self):
        cols = ["timestamp_s", "torque_nm"]
        log = Logger(_config(columns=cols), _buf())
        assert log._columns == ("timestamp_s", "torque_nm")


# ---------------------------------------------------------------------------
# TestFieldTranslation
# ---------------------------------------------------------------------------

class TestFieldTranslation:
    def test_rename_map_contains_all_three_adapters(self):
        assert _FIELD_RENAME["measured_current_a"] == "motor_current_a"
        assert _FIELD_RENAME["voltage_v"] == "psu_voltage_v"
        assert _FIELD_RENAME["current_a"] == "psu_current_a"

    def test_measured_current_renamed_in_output(self):
        buf = _buf()
        log = Logger(_config(), buf)
        log.write(_full_row(measured_current_a=7.5))
        rows = _parse(buf)
        assert rows[0]["motor_current_a"] == "7.5"

    def test_voltage_v_renamed_in_output(self):
        buf = _buf()
        log = Logger(_config(), buf)
        log.write(_full_row(voltage_v=18.0))
        rows = _parse(buf)
        assert rows[0]["psu_voltage_v"] == "18.0"

    def test_current_a_renamed_in_output(self):
        buf = _buf()
        log = Logger(_config(), buf)
        log.write(_full_row(current_a=1.5))
        rows = _parse(buf)
        assert rows[0]["psu_current_a"] == "1.5"

    def test_all_three_renames_simultaneously(self):
        buf = _buf()
        log = Logger(_config(), buf)
        log.write(_full_row(measured_current_a=3.0, voltage_v=12.0, current_a=0.5))
        rows = _parse(buf)
        assert rows[0]["motor_current_a"] == "3.0"
        assert rows[0]["psu_voltage_v"] == "12.0"
        assert rows[0]["psu_current_a"] == "0.5"

    def test_unmapped_keys_pass_through(self):
        buf = _buf()
        log = Logger(_config(), buf)
        log.write(_full_row(timestamp_s=99.0, test_phase="COMPLETE"))
        rows = _parse(buf)
        assert rows[0]["timestamp_s"] == "99.0"
        assert rows[0]["test_phase"] == "COMPLETE"


# ---------------------------------------------------------------------------
# TestNullHandling
# ---------------------------------------------------------------------------

class TestNullHandling:
    def test_none_column_writes_empty_string_not_none_literal(self):
        buf = _buf()
        log = Logger(_config(with_efficiency=False), buf)
        log.write(_full_row(torque_nm=None))
        rows = _parse(buf)
        assert rows[0]["torque_nm"] == ""
        assert rows[0]["torque_nm"] != "None"

    def test_multiple_none_columns_all_empty(self):
        buf = _buf()
        log = Logger(_config(with_efficiency=False), buf)
        log.write(_full_row(torque_nm=None, velocity_rad_s=None, voltage_v=None))
        rows = _parse(buf)
        assert rows[0]["torque_nm"] == ""
        assert rows[0]["velocity_rad_s"] == ""
        assert rows[0]["psu_voltage_v"] == ""

    def test_zero_not_converted_to_empty(self):
        buf = _buf()
        log = Logger(_config(with_efficiency=False), buf)
        log.write(_full_row(torque_nm=0.0, velocity_rad_s=0))
        rows = _parse(buf)
        assert rows[0]["torque_nm"] == "0.0"
        assert rows[0]["velocity_rad_s"] == "0"

    def test_false_not_converted_to_empty(self):
        buf = _buf()
        log = Logger(_config(columns=["timestamp_s", "test_phase"], with_efficiency=False), buf)
        log.write({"timestamp_s": 1.0, "test_phase": False})
        rows = _parse(buf)
        assert rows[0]["test_phase"] == "False"

    def test_empty_string_stays_empty(self):
        buf = _buf()
        log = Logger(_config(with_efficiency=False), buf)
        log.write(_full_row(test_phase=""))
        rows = _parse(buf)
        assert rows[0]["test_phase"] == ""


# ---------------------------------------------------------------------------
# TestEfficiency
# ---------------------------------------------------------------------------

class TestEfficiency:
    def _eff_config(self) -> Dict[str, Any]:
        cols = list(_BASE_COLUMNS) + ["efficiency"]
        return _config(with_efficiency=True, columns=cols)

    def test_correct_value_for_valid_inputs(self):
        # η = (120 * 50) / (24 * 2) = 6000 / 48 = 125.0
        buf = _buf()
        log = Logger(self._eff_config(), buf)
        log.write(_full_row(torque_nm=120.0, velocity_rad_s=50.0, voltage_v=24.0, current_a=2.0))
        rows = _parse(buf)
        assert float(rows[0]["efficiency"]) == pytest.approx(125.0)

    def test_div_by_zero_voltage_writes_empty(self):
        buf = _buf()
        log = Logger(self._eff_config(), buf)
        log.write(_full_row(voltage_v=0.0, current_a=2.0))
        rows = _parse(buf)
        assert rows[0]["efficiency"] == ""

    def test_div_by_zero_current_writes_empty(self):
        buf = _buf()
        log = Logger(self._eff_config(), buf)
        log.write(_full_row(voltage_v=24.0, current_a=0.0))
        rows = _parse(buf)
        assert rows[0]["efficiency"] == ""

    def test_none_torque_writes_empty_efficiency(self):
        buf = _buf()
        log = Logger(self._eff_config(), buf)
        log.write(_full_row(torque_nm=None))
        rows = _parse(buf)
        assert rows[0]["efficiency"] == ""

    def test_none_psu_voltage_writes_empty_efficiency(self):
        buf = _buf()
        log = Logger(self._eff_config(), buf)
        log.write(_full_row(voltage_v=None))
        rows = _parse(buf)
        assert rows[0]["efficiency"] == ""

    def test_none_psu_current_writes_empty_efficiency(self):
        buf = _buf()
        log = Logger(self._eff_config(), buf)
        log.write(_full_row(current_a=None))
        rows = _parse(buf)
        assert rows[0]["efficiency"] == ""

    def test_efficiency_not_written_when_not_in_columns(self):
        # Formula present in YAML but 'efficiency' not in output.columns → not in CSV.
        buf = _buf()
        log = Logger(_config(with_efficiency=True), buf)  # BASE_COLUMNS only
        log.write(_full_row())
        rows = _parse(buf)
        assert "efficiency" not in rows[0]

    def test_efficiency_not_computed_when_no_formula(self):
        cols = list(_BASE_COLUMNS) + ["efficiency"]
        buf = _buf()
        log = Logger(_config(with_efficiency=False, columns=cols), buf)
        log.write(_full_row())
        rows = _parse(buf)
        # Column present but no formula → no value computed → key absent or empty.
        assert rows[0].get("efficiency", "") == ""


# ---------------------------------------------------------------------------
# TestColumnControl
# ---------------------------------------------------------------------------

class TestColumnControl:
    def test_csv_headers_match_yaml_columns_exactly(self):
        buf = _buf()
        Logger(_config(), buf)
        buf.seek(0)
        headers = buf.readline().rstrip("\n").split(",")
        assert headers == _BASE_COLUMNS

    def test_extra_internal_fields_not_in_output(self):
        buf = _buf()
        log = Logger(_config(), buf)
        row = dict(_full_row())
        row["internal_debug_flag"] = "x"
        row["__raw_bytes"] = b"\xaa"
        log.write(row)
        rows = _parse(buf)
        assert "internal_debug_flag" not in rows[0]
        assert "__raw_bytes" not in rows[0]

    def test_column_order_matches_yaml(self):
        custom_cols = ["torque_nm", "timestamp_s", "test_phase"]
        buf = _buf()
        Logger(_config(columns=custom_cols, with_efficiency=False), buf)
        buf.seek(0)
        headers = buf.readline().rstrip("\n").split(",")
        assert headers == custom_cols

    def test_only_declared_columns_in_output(self):
        cols = ["timestamp_s", "torque_nm"]
        buf = _buf()
        log = Logger(_config(columns=cols, with_efficiency=False), buf)
        log.write(_full_row())
        rows = _parse(buf)
        assert set(rows[0].keys()) == {"timestamp_s", "torque_nm"}


# ---------------------------------------------------------------------------
# TestContextManager
# ---------------------------------------------------------------------------

class TestContextManager:
    def test_enter_returns_logger(self):
        log = Logger(_config(), _buf())
        assert log.__enter__() is log

    def test_exit_does_not_close_caller_file(self):
        buf = _buf()
        log = Logger(_config(), buf)
        log.__exit__(None, None, None)
        # File still writable after __exit__.
        assert not buf.closed

    def test_with_statement_rows_accessible(self):
        buf = _buf()
        with Logger(_config(), buf) as log:
            log.write(_full_row())
            log.write(_full_row(timestamp_s=2.0))
        assert log.rows_written == 2


# ---------------------------------------------------------------------------
# TestIntegration
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_rows_written_counter_increments(self):
        log = Logger(_config(), _buf())
        for i in range(5):
            log.write(_full_row(timestamp_s=float(i)))
        assert log.rows_written == 5

    def test_multi_row_output_parseable_by_dictreader(self):
        buf = _buf()
        log = Logger(_config(with_efficiency=False), buf)
        for i in range(3):
            log.write(_full_row(timestamp_s=float(i), torque_nm=float(i * 10)))
        rows = _parse(buf)
        assert len(rows) == 3
        assert [float(r["timestamp_s"]) for r in rows] == [0.0, 1.0, 2.0]
        assert [float(r["torque_nm"]) for r in rows] == [0.0, 10.0, 20.0]

    def test_header_written_exactly_once(self):
        buf = _buf()
        log = Logger(_config(), buf)
        for i in range(4):
            log.write(_full_row(timestamp_s=float(i)))
        buf.seek(0)
        lines = buf.readlines()
        # First line is header; remaining are data rows.
        assert len(lines) == 5
        header_cols = lines[0].rstrip("\n").split(",")
        assert header_cols == _BASE_COLUMNS

    def test_phase_transitions_recorded_correctly(self):
        buf = _buf()
        log = Logger(_config(), buf)
        log.write(_full_row(test_phase="CURRENT_RAMP"))
        log.write(_full_row(test_phase="TORQUE_HOLD"))
        log.write(_full_row(test_phase="COMPLETE"))
        rows = _parse(buf)
        assert [r["test_phase"] for r in rows] == [
            "CURRENT_RAMP", "TORQUE_HOLD", "COMPLETE"
        ]
