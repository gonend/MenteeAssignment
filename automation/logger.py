from __future__ import annotations

import csv
import logging
from typing import Any, Dict, Optional, TextIO

logger = logging.getLogger(__name__)

# Structural adapter: maps internal driver row keys → YAML output column names.
# Inclusion and order of columns remain strictly driven by yaml['output']['columns'].
FIELD_RENAME: Dict[str, str] = {
    "measured_current_a": "motor_current_a",
    "voltage_v":          "psu_voltage_v",
    "current_a":          "psu_current_a",
}


def _cfg_get(cfg: Dict[str, Any], *path: str) -> Any:
    """Walk nested dicts; raise KeyError with full dotted path on miss."""
    d = cfg
    dotted = ".".join(path)
    for k in path:
        if not isinstance(d, dict) or k not in d:
            raise KeyError(f"Missing required config key: {dotted}")
        d = d[k]
    return d


class Logger:
    """Streaming CSV writer for motor characterization test output.

    Column order and inclusion are driven strictly by yaml['output']['columns'].
    Efficiency is computed via a YAML-defined formula compiled once at __init__;
    it is written only when 'efficiency' appears in the columns list.

    The caller owns the output file — Logger does not open or close it.
    Use as a context manager or call write() directly.
    """

    def __init__(self, config: Dict[str, Any], output_file: TextIO) -> None:
        output_cfg = _cfg_get(config, "output")
        self._columns: tuple[str, ...] = tuple(output_cfg["columns"])

        # Compile efficiency formula once; None if section is absent.
        self._compiled_formula = None
        self._eff_col: Optional[str] = None
        eff_cfg = output_cfg.get("efficiency")
        if eff_cfg and "formula" in eff_cfg:
            formula_str: str = eff_cfg["formula"]
            self._compiled_formula = compile(formula_str, "<string>", "eval")
            self._eff_col = "efficiency"
            logger.info("Efficiency formula compiled: %s", formula_str)

        self._writer = csv.DictWriter(
            output_file,
            fieldnames=list(self._columns),
            extrasaction="ignore",
            lineterminator="\n",
        )
        self._writer.writeheader()
        self._rows_written = 0

        # Efficiency accumulators — three floats, O(1) memory
        self._eff_sum: float = 0.0
        self._eff_count: int = 0
        self._eff_peak: Optional[float] = None

        logger.info("Logger ready | columns=%s", self._columns)

    def write(self, row: Dict[str, Any]) -> None:
        """Translate keys, compute efficiency, sanitize None → '', write one row."""
        # Step 1: rename internal driver keys to output column names.
        translated: Dict[str, Any] = {}
        for k, v in row.items():
            translated[FIELD_RENAME.get(k, k)] = v

        # Step 2: compute efficiency (uses raw pre-sanitized values so None is detectable).
        if self._compiled_formula is not None and self._eff_col is not None:
            safe_dict = {col: translated.get(col) for col in self._columns}
            try:
                translated[self._eff_col] = eval(  # noqa: S307
                    self._compiled_formula, {}, safe_dict
                )
            except (ZeroDivisionError, TypeError, ValueError):
                translated[self._eff_col] = None
                logger.debug(
                    "Efficiency undefined at t=%s (div-by-zero or None input)",
                    translated.get("timestamp_s"),
                )
            eff_val = translated.get(self._eff_col)
            if eff_val is not None:
                self._eff_sum += eff_val
                self._eff_count += 1
                if self._eff_peak is None or eff_val > self._eff_peak:
                    self._eff_peak = eff_val

        # Step 3: None → '' prevents the literal string "None" from appearing in CSV.
        sanitized = {k: ("" if v is None else v) for k, v in translated.items()}

        self._writer.writerow(sanitized)
        self._rows_written += 1

    def __enter__(self) -> "Logger":
        return self

    def __exit__(self, *_: Any) -> None:
        pass  # caller owns the file; do not close it here

    @property
    def rows_written(self) -> int:
        return self._rows_written

    @property
    def efficiency_mean(self) -> Optional[float]:
        return self._eff_sum / self._eff_count if self._eff_count else None

    @property
    def efficiency_peak(self) -> Optional[float]:
        return self._eff_peak
