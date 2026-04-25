"""
Tests for MotorBinaryReader.

Packet layout (from motor_protocol.yaml):
  [0xAA 0x55][module_type:B][payload_size:B][timestamp_ms:<I]
  [response_code:B][velocity:<f][measured_current:<f]
  [xor_checksum:B][0x55 0xAA]
  Total = 20 bytes for telemetry (response 0x0E, payload_size=9)
"""
import struct
from pathlib import Path

import pytest
import yaml

from drivers.motor import MotorBinaryReader


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def protocol_config():
    config_path = Path(__file__).parent.parent / "config" / "motor_protocol.yaml"
    with open(config_path, 'r') as fh:
        return yaml.safe_load(fh)


def _make_packet(
    timestamp_ms: int,
    velocity: float,
    current: float,
    module_type: int = 0x42,
    response_code: int = 0x0E,
    corrupt_checksum: bool = False,
    bad_end_marker: bool = False,
) -> bytes:
    """Build a well-formed 20-byte telemetry packet (or a deliberately broken one)."""
    start = bytes([0xAA, 0x55])
    payload_size = 9  # 1 (code) + 4 (velocity) + 4 (current)
    header = struct.pack('<BBI', module_type, payload_size, timestamp_ms)
    payload = struct.pack('<Bff', response_code, velocity, current)

    cs = 0
    for b in start + header + payload:
        cs ^= b
    if corrupt_checksum:
        cs ^= 0xFF  # guaranteed mismatch

    end = bytes([0x55, 0xAA]) if not bad_end_marker else bytes([0xFF, 0xFF])
    return start + header + payload + bytes([cs]) + end


# ──────────────────────────────────────────────────────────────
# 1. Initialisation
# ──────────────────────────────────────────────────────────────

class TestMotorBinaryReaderInit:

    def test_valid_init_compiles_structs(self, protocol_config):
        reader = MotorBinaryReader("dummy.bin", protocol_config)
        assert reader._header_struct is not None
        assert reader._header_size == 6  # BBl = 1+1+4

    def test_missing_protocol_key_raises(self, tmp_path):
        bad_cfg = {'framing': {}}  # no 'protocol' key
        with pytest.raises(ValueError, match="CRITICAL"):
            MotorBinaryReader(str(tmp_path / "x.bin"), bad_cfg)

    def test_missing_framing_key_raises(self, tmp_path):
        bad_cfg = {'protocol': {'byte_order': 'little_endian'}}  # no 'framing'
        with pytest.raises(ValueError, match="CRITICAL"):
            MotorBinaryReader(str(tmp_path / "x.bin"), bad_cfg)

    def test_response_structs_populated(self, protocol_config):
        reader = MotorBinaryReader("dummy.bin", protocol_config)
        # 0x0E (14) is the telemetry response code from YAML
        assert 0x0E in reader._response_structs


# ──────────────────────────────────────────────────────────────
# 2. Valid packets
# ──────────────────────────────────────────────────────────────

class TestMotorBinaryReaderValidPacket:

    def test_single_packet_yields_one_record(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(1000, 10.5, 2.3))

        records = list(MotorBinaryReader(str(f), protocol_config))
        assert len(records) == 1

    def test_timestamp_ms_converted_to_s(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(2500, 0.0, 0.0))

        record = list(MotorBinaryReader(str(f), protocol_config))[0]
        assert record['timestamp_s'] == pytest.approx(2.5)

    def test_velocity_value(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(1000, 31.4159, 0.0))

        record = list(MotorBinaryReader(str(f), protocol_config))[0]
        assert record['velocity_rad_s'] == pytest.approx(31.4159, rel=1e-5)

    def test_current_value(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(1000, 0.0, 15.75))

        record = list(MotorBinaryReader(str(f), protocol_config))[0]
        assert record['measured_current_a'] == pytest.approx(15.75, rel=1e-5)

    def test_stats_after_valid_packets(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(0, 1.0, 0.5) + _make_packet(1, 2.0, 1.0))

        reader = MotorBinaryReader(str(f), protocol_config)
        list(reader)

        assert reader.stats['total_packets'] == 2
        assert reader.stats['checksum_errors'] == 0
        assert reader.stats['truncations'] == 0
        assert reader.stats['unknown_codes'] == 0


# ──────────────────────────────────────────────────────────────
# 3. Checksum errors
# ──────────────────────────────────────────────────────────────

class TestMotorBinaryReaderChecksumError:

    def test_checksum_error_discards_packet(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(1000, 1.0, 0.5, corrupt_checksum=True))

        records = list(MotorBinaryReader(str(f), protocol_config))
        assert records == []

    def test_checksum_error_increments_counter(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(1000, 1.0, 0.5, corrupt_checksum=True))

        reader = MotorBinaryReader(str(f), protocol_config)
        list(reader)
        assert reader.stats['checksum_errors'] == 1

    def test_valid_after_checksum_error(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(
            _make_packet(1000, 1.0, 0.5, corrupt_checksum=True)
            + _make_packet(2000, 2.0, 1.0)
        )

        reader = MotorBinaryReader(str(f), protocol_config)
        records = list(reader)

        assert len(records) == 1
        assert records[0]['timestamp_s'] == pytest.approx(2.0)
        assert reader.stats['checksum_errors'] == 1


# ──────────────────────────────────────────────────────────────
# 4. Resync after corruption / junk bytes
# ──────────────────────────────────────────────────────────────

class TestMotorBinaryReaderResync:

    def test_resync_after_junk_prefix(self, tmp_path, protocol_config):
        """Junk bytes before first packet — reader must scan through them."""
        junk = bytes([0x11, 0x22, 0x33, 0xAA, 0x00, 0x44])  # 0xAA but no 0x55 follow
        f = tmp_path / "motor.bin"
        f.write_bytes(junk + _make_packet(500, 5.0, 1.5))

        records = list(MotorBinaryReader(str(f), protocol_config))
        assert len(records) == 1
        assert records[0]['timestamp_s'] == pytest.approx(0.5)

    def test_resync_after_checksum_failure_mid_stream(self, tmp_path, protocol_config):
        """Corrupted packet in the middle — reader recovers and yields subsequent packet."""
        f = tmp_path / "motor.bin"
        f.write_bytes(
            _make_packet(1000, 1.0, 0.5)
            + _make_packet(2000, 2.0, 1.0, corrupt_checksum=True)
            + _make_packet(3000, 3.0, 1.5)
        )

        reader = MotorBinaryReader(str(f), protocol_config)
        records = list(reader)

        assert len(records) == 2
        assert records[0]['timestamp_s'] == pytest.approx(1.0)
        assert records[1]['timestamp_s'] == pytest.approx(3.0)
        assert reader.stats['checksum_errors'] == 1

    def test_partial_start_marker_not_confused(self, tmp_path, protocol_config):
        """0xAA byte not followed by 0x55 must not break the scanner."""
        # Inject a lone 0xAA byte between two valid packets
        f = tmp_path / "motor.bin"
        f.write_bytes(
            _make_packet(100, 1.0, 0.1)
            + bytes([0xAA, 0x00])            # false partial marker
            + _make_packet(200, 2.0, 0.2)
        )

        records = list(MotorBinaryReader(str(f), protocol_config))
        # Both valid packets yielded; false marker skipped
        assert len(records) == 2


# ──────────────────────────────────────────────────────────────
# 5. Truncated packets
# ──────────────────────────────────────────────────────────────

class TestMotorBinaryReaderTruncation:

    def test_truncated_at_header(self, tmp_path, protocol_config):
        """File ends after start marker — header cannot be read."""
        f = tmp_path / "motor.bin"
        f.write_bytes(bytes([0xAA, 0x55]))  # only start marker

        reader = MotorBinaryReader(str(f), protocol_config)
        records = list(reader)

        assert records == []
        assert reader.stats['truncations'] == 1

    def test_truncated_at_payload(self, tmp_path, protocol_config):
        """File ends mid-payload (packet size = full packet minus last 5 bytes)."""
        full = _make_packet(1000, 1.0, 0.5)
        f = tmp_path / "motor.bin"
        f.write_bytes(full[:-5])  # chop end_marker + checksum + 3 payload bytes

        reader = MotorBinaryReader(str(f), protocol_config)
        records = list(reader)

        assert records == []
        assert reader.stats['truncations'] == 1

    def test_truncation_does_not_affect_prior_valid_packets(self, tmp_path, protocol_config):
        """Valid packets before the truncated tail must still be yielded."""
        full = _make_packet(2000, 2.0, 1.0)
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(1000, 1.0, 0.5) + full[:10])  # second packet is partial

        reader = MotorBinaryReader(str(f), protocol_config)
        records = list(reader)

        assert len(records) == 1
        assert records[0]['timestamp_s'] == pytest.approx(1.0)
        assert reader.stats['truncations'] == 1


# ──────────────────────────────────────────────────────────────
# 6. Unknown response codes
# ──────────────────────────────────────────────────────────────

class TestMotorBinaryReaderUnknownCode:

    def test_unknown_code_skipped(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(1000, 1.0, 0.5, response_code=0xFF))

        records = list(MotorBinaryReader(str(f), protocol_config))
        assert records == []

    def test_unknown_code_increments_counter(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(1000, 1.0, 0.5, response_code=0xFF))

        reader = MotorBinaryReader(str(f), protocol_config)
        list(reader)
        assert reader.stats['unknown_codes'] == 1

    def test_valid_after_unknown_code(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(
            _make_packet(1000, 1.0, 0.5, response_code=0xFF)
            + _make_packet(2000, 2.0, 1.0)
        )

        reader = MotorBinaryReader(str(f), protocol_config)
        records = list(reader)

        assert len(records) == 1
        assert records[0]['timestamp_s'] == pytest.approx(2.0)
        assert reader.stats['unknown_codes'] == 1


# ──────────────────────────────────────────────────────────────
# 7. Stats & iterator contract
# ──────────────────────────────────────────────────────────────

class TestMotorBinaryReaderStats:

    def test_stats_reset_on_reiteration(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(
            _make_packet(1000, 1.0, 0.5, corrupt_checksum=True)
            + _make_packet(2000, 2.0, 1.0)
        )

        reader = MotorBinaryReader(str(f), protocol_config)
        list(reader)
        assert reader.stats['checksum_errors'] == 1

        list(reader)
        assert reader.stats['checksum_errors'] == 1  # reset, not doubled

    def test_get_stats_returns_dict(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(1000, 1.0, 0.5))

        reader = MotorBinaryReader(str(f), protocol_config)
        list(reader)
        stats = reader.get_stats()

        assert isinstance(stats, dict)
        for key in ('total_packets', 'checksum_errors', 'truncations', 'unknown_codes'):
            assert key in stats

    def test_record_keys(self, tmp_path, protocol_config):
        f = tmp_path / "motor.bin"
        f.write_bytes(_make_packet(1000, 5.0, 2.0))

        records = list(MotorBinaryReader(str(f), protocol_config))
        assert set(records[0].keys()) == {'timestamp_s', 'velocity_rad_s', 'measured_current_a'}


# ──────────────────────────────────────────────────────────────
# 8. Real data integration
# ──────────────────────────────────────────────────────────────

class TestMotorBinaryReaderRealData:

    def test_real_binary_file_loads(self, protocol_config):
        data_path = Path(__file__).parent.parent / "data" / "test_motor_1000hz.bin"
        if not data_path.exists():
            pytest.skip("Binary test data not available")

        reader = MotorBinaryReader(str(data_path), protocol_config)
        records = list(reader)

        assert len(records) > 0
        assert reader.stats['total_packets'] == len(records)

    def test_real_data_has_correct_keys(self, protocol_config):
        data_path = Path(__file__).parent.parent / "data" / "test_motor_1000hz.bin"
        if not data_path.exists():
            pytest.skip("Binary test data not available")

        records = list(MotorBinaryReader(str(data_path), protocol_config))
        for record in records[:10]:  # spot-check first 10
            assert 'timestamp_s' in record
            assert 'velocity_rad_s' in record
            assert 'measured_current_a' in record
