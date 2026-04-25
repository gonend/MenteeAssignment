import abc
import csv
import logging
import struct
from typing import Any, Dict, Iterator, List, Tuple

from config.consts import _BYTE_ORDER_PREFIX, _UNIT_SUFFIX_MAP, YAML_TYPE_MAP

logger = logging.getLogger(__name__)


class MotorDataSource(abc.ABC):
    """Abstract interface for motor telemetry data."""

    @abc.abstractmethod
    def __iter__(self) -> Iterator[Dict[str, Any]]:
        """
        Yields dicts containing at least:
        - 'timestamp_s'
        - 'velocity_rad_s'
        - 'measured_current_a'
        """

    @abc.abstractmethod
    def get_stats(self) -> Dict[str, Any]:
        """Returns error counts and parsing statistics."""


class MotorBinaryReader(MotorDataSource):
    """
    Reads BLDC motor binary telemetry. All packet layout, type sizes, field
    names, and response codes are derived from motor_protocol.yaml at init —
    no structural constants are hardcoded here.
    """

    def __init__(self, file_path: str, protocol_config: Dict[str, Any]) -> None:
        self.file_path = file_path
        self.protocol_config = protocol_config
        self.stats: Dict[str, int] = {
            "total_packets": 0,
            "checksum_errors": 0,
            "truncations": 0,
            "unknown_codes": 0,
        }
        self._init_framing()

    def _init_framing(self) -> None:
        cfg = self.protocol_config
        try:
            framing = cfg['framing']
            byte_order = cfg['protocol']['byte_order']
            bo = _BYTE_ORDER_PREFIX.get(byte_order, '<')

            # Build type-name → (bare struct char, byte size) from YAML types section.
            # Strip any existing prefix so we add our own once per combined Struct.
            type_fmt: Dict[str, Tuple[str, int]] = {
                name: (defn['format'].lstrip('<>!'), defn['size'])
                for name, defn in cfg.get('types', {}).items()
            }

            self._start_marker = bytes(framing['start_marker']['bytes'])
            self._end_marker = bytes(framing['end_marker']['bytes'])
            self._end_marker_size: int = framing['end_marker']['size']

            # Header struct — fields sorted by YAML offset, not assumed order
            hdr_fields = sorted(framing['header']['fields'], key=lambda f: f['offset'])
            hdr_fmt = bo + ''.join(type_fmt[f['type']][0] for f in hdr_fields)
            self._header_struct = struct.Struct(hdr_fmt)
            self._header_field_names: List[str] = [f['name'] for f in hdr_fields]
            self._header_size: int = self._header_struct.size  # 6 bytes for BLDC protocol

            # Pre-compile one Struct per response code; output key derived from name+unit
            self._response_structs: Dict[int, Tuple[struct.Struct, List[str]]] = {}
            for resp in cfg.get('responses', []):
                code = int(resp['code'])
                fields = resp['fields']
                fmt = bo + ''.join(type_fmt[f['type']][0] for f in fields)
                output_keys = [
                    f['name'] + _UNIT_SUFFIX_MAP.get(f.get('unit', ''), '')
                    for f in fields
                ]
                self._response_structs[code] = (struct.Struct(fmt), output_keys)

        except KeyError as e:
            raise ValueError(
                f"CRITICAL: Missing required field in protocol_config: {e}"
            )

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        self.stats = {
            "total_packets": 0,
            "checksum_errors": 0,
            "truncations": 0,
            "unknown_codes": 0,
        }
        sm = self._start_marker
        sm_len = len(sm)
        hdr_size = self._header_size
        em_size = self._end_marker_size

        with open(self.file_path, 'rb') as fh:
            data = fh.read()

        pos = 0
        length = len(data)

        while pos < length:
            # ── Step 1: scan for start marker ──────────────────────────────
            idx = data.find(sm, pos)
            if idx == -1:
                break

            pkt_start = idx
            hdr_end = pkt_start + sm_len + hdr_size  # offset 8 from pkt_start

            if hdr_end > length:
                # EOF: not enough bytes for a complete header
                self.stats["truncations"] += 1
                break

            # ── Step 2: parse header ────────────────────────────────────────
            hdr_vals = self._header_struct.unpack(data[pkt_start + sm_len : hdr_end])
            hdr = dict(zip(self._header_field_names, hdr_vals))
            payload_size: int = int(hdr['payload_size'])
            timestamp_ms: int = int(hdr['timestamp_ms'])

            payload_end = hdr_end + payload_size  # offset 8+N from pkt_start
            cs_pos = payload_end                  # offset 8+N
            em_start = cs_pos + 1                 # offset 9+N
            em_end = em_start + em_size           # offset 11+N

            if em_end > length:
                # EOF: packet is incomplete
                self.stats["truncations"] += 1
                break

            # ── Step 3: read payload, checksum, end marker ─────────────────
            payload = data[hdr_end:payload_end]
            checksum_byte: int = data[cs_pos]
            end_marker_bytes = data[em_start:em_end]

            # ── Step 4: verify XOR checksum over bytes[0..8+N-1] ──────────
            # = start_marker(2) + header(6) + payload(N) = 8+N bytes
            computed_cs = 0
            for b in data[pkt_start:payload_end]:
                computed_cs ^= b

            if computed_cs != checksum_byte:
                logger.warning(
                    "Checksum mismatch at byte %d (ts=%dms): computed=0x%02X expected=0x%02X",
                    pkt_start, timestamp_ms, computed_cs, checksum_byte,
                )
                self.stats["checksum_errors"] += 1
                pos = pkt_start + 1  # backtrack: re-scan from next byte
                continue

            # ── Step 5: verify end marker (warn only — packet already validated) ──
            if bytes(end_marker_bytes) != self._end_marker:
                logger.warning(
                    "End marker mismatch at ts=%dms: expected=%s got=%s",
                    timestamp_ms, self._end_marker.hex(), bytes(end_marker_bytes).hex(),
                )

            # ── Step 6: dispatch on response code ─────────────────────────
            if payload_size == 0:
                logger.warning("Empty payload at ts=%dms", timestamp_ms)
                self.stats["unknown_codes"] += 1
                pos = em_end
                continue

            response_code: int = payload[0]

            if response_code not in self._response_structs:
                logger.warning(
                    "Unknown response code 0x%02X at ts=%dms, skipping %d payload bytes",
                    response_code, timestamp_ms, payload_size,
                )
                self.stats["unknown_codes"] += 1
                pos = em_end
                continue

            # ── Step 7: unpack fields, yield record ────────────────────────
            resp_struct, output_keys = self._response_structs[response_code]
            field_data = payload[1:]  # skip the response-code byte

            if len(field_data) < resp_struct.size:
                self.stats["truncations"] += 1
                pos = em_end
                continue

            field_vals = resp_struct.unpack(field_data[: resp_struct.size])
            self.stats["total_packets"] += 1

            record: Dict[str, Any] = {"timestamp_s": timestamp_ms / 1000.0}
            for out_key, fval in zip(output_keys, field_vals):
                record[out_key] = fval

            pos = em_end
            yield record

    def get_stats(self) -> Dict[str, Any]:
        return self.stats


class MotorCSVReader(MotorDataSource):
    """
    Reads motor telemetry CSV. Column names and types come from
    data_sources.motor.formats.csv.columns in test_config.yaml — never hardcoded.
    """

    def __init__(self, file_path: str, test_config: Dict[str, Any]) -> None:
        self.file_path = file_path
        self.stats: Dict[str, int] = {
            "total_rows": 0,
            "malformed_rows": 0,
            "timestamp_gaps": 0,
        }

        try:
            csv_config = test_config['data_sources']['motor']['formats']['csv']['columns']
            self.expected_columns: Dict[str, Any] = {}
            for col in csv_config:
                col_name = col['name']
                col_type_str = col['type']
                if col_type_str not in YAML_TYPE_MAP:
                    raise ValueError(
                        f"CRITICAL: Unsupported type '{col_type_str}' for column '{col_name}'"
                    )
                self.expected_columns[col_name] = YAML_TYPE_MAP[col_type_str]
        except KeyError as e:
            raise ValueError(f"CRITICAL: Missing required YAML config for Motor CSV: {e}")

        self._validate_file()

    def _validate_file(self) -> None:
        with open(self.file_path, 'r', encoding='utf-8-sig') as fh:
            reader = csv.reader(fh)
            try:
                raw_headers = next(reader)
                headers = [h.strip() for h in raw_headers if h.strip()]
            except StopIteration:
                raise ValueError(f"CRITICAL: CSV file {self.file_path} is empty.")

            if not all(col in headers for col in self.expected_columns):
                raise ValueError(
                    f"CRITICAL: Missing headers in {self.file_path}. "
                    f"Expected {list(self.expected_columns.keys())}, found {headers}."
                )

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        self.stats = {"total_rows": 0, "malformed_rows": 0, "timestamp_gaps": 0}

        with open(self.file_path, 'r', encoding='utf-8-sig') as fh:
            reader_obj = csv.reader(fh)
            try:
                clean_fieldnames = [h.strip() for h in next(reader_obj)]
            except StopIteration:
                return

            reader = csv.DictReader(fh, fieldnames=clean_fieldnames)
            last_timestamp = -1.0

            for row_idx, row in enumerate(reader, start=2):
                self.stats["total_rows"] += 1

                if None in row.values() or None in row.keys():
                    logger.warning(
                        "Motor CSV row %d malformed: incorrect field count. Skipping.", row_idx
                    )
                    self.stats["malformed_rows"] += 1
                    continue

                try:
                    parsed_row: Dict[str, Any] = {}
                    for col_name, cast_func in self.expected_columns.items():
                        parsed_row[col_name] = cast_func(row[col_name])
                except (ValueError, KeyError):
                    logger.warning(
                        "Motor CSV row %d malformed: invalid type conversion. Skipping.", row_idx
                    )
                    self.stats["malformed_rows"] += 1
                    continue

                current_ts: float = parsed_row['timestamp_s']
                if current_ts <= last_timestamp:
                    logger.warning(
                        "Motor CSV timestamp issue at row %d: %f follows %f.",
                        row_idx, current_ts, last_timestamp,
                    )
                    self.stats["timestamp_gaps"] += 1

                last_timestamp = current_ts
                yield parsed_row

    def get_stats(self) -> Dict[str, Any]:
        return self.stats
