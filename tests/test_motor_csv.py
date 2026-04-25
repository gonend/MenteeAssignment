import pytest
import yaml
from pathlib import Path
from drivers.motor import MotorCSVReader


@pytest.fixture
def test_config():
    config_path = Path(__file__).parent.parent / "config" / "test_config.yaml"
    with open(config_path, 'r') as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def valid_motor_csv(tmp_path):
    csv_file = tmp_path / "motor.csv"
    csv_file.write_text(
        "timestamp_s,velocity_rad_s,measured_current_a\n"
        "0.000000,10.5,2.3\n"
        "0.001000,10.6,2.4\n"
        "0.002000,10.7,2.5\n"
        "0.003000,10.8,2.6\n"
    )
    return str(csv_file)


# ──────────────────────────────────────────────────────────────
# 1. Startup validation
# ──────────────────────────────────────────────────────────────

class TestMotorCSVReaderValidation:

    def test_empty_file_raises_error(self, tmp_path, test_config):
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text("")

        with pytest.raises(ValueError, match="empty"):
            MotorCSVReader(str(csv_file), test_config)

    def test_missing_headers_raises_error(self, tmp_path, test_config):
        csv_file = tmp_path / "bad_headers.csv"
        csv_file.write_text("timestamp_s,velocity_rad_s\n0.0,1.0\n")  # missing measured_current_a

        with pytest.raises(ValueError, match="Missing headers"):
            MotorCSVReader(str(csv_file), test_config)

    def test_valid_headers_passes(self, valid_motor_csv, test_config):
        reader = MotorCSVReader(valid_motor_csv, test_config)
        assert set(reader.expected_columns.keys()) == {
            'timestamp_s', 'velocity_rad_s', 'measured_current_a'
        }

    def test_missing_yaml_key_raises_error(self, tmp_path):
        bad_config = {'data_sources': {}}
        csv_file = tmp_path / "motor.csv"
        csv_file.write_text("timestamp_s,velocity_rad_s,measured_current_a\n0.0,1.0,0.5\n")

        with pytest.raises(ValueError, match="CRITICAL"):
            MotorCSVReader(str(csv_file), bad_config)


# ──────────────────────────────────────────────────────────────
# 2. Row parsing
# ──────────────────────────────────────────────────────────────

class TestMotorCSVReaderParsing:

    def test_valid_rows_parsed(self, valid_motor_csv, test_config):
        reader = MotorCSVReader(valid_motor_csv, test_config)
        rows = list(reader)

        assert len(rows) == 4
        assert rows[0]['timestamp_s'] == pytest.approx(0.0)
        assert rows[0]['velocity_rad_s'] == pytest.approx(10.5)
        assert rows[0]['measured_current_a'] == pytest.approx(2.3)

    def test_malformed_row_incorrect_field_count(self, tmp_path, test_config):
        csv_file = tmp_path / "bad_fields.csv"
        csv_file.write_text(
            "timestamp_s,velocity_rad_s,measured_current_a\n"
            "0.000,10.5,2.3\n"
            "0.001,10.6\n"           # missing measured_current_a
            "0.002,10.7,2.5\n"
        )

        reader = MotorCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 2
        assert reader.stats['malformed_rows'] == 1
        assert reader.stats['total_rows'] == 3

    def test_malformed_row_non_numeric(self, tmp_path, test_config):
        csv_file = tmp_path / "bad_values.csv"
        csv_file.write_text(
            "timestamp_s,velocity_rad_s,measured_current_a\n"
            "0.000,10.5,2.3\n"
            "0.001,INVALID,2.4\n"
            "0.002,10.7,2.5\n"
        )

        reader = MotorCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 2
        assert reader.stats['malformed_rows'] == 1

    def test_multiple_malformed_rows(self, tmp_path, test_config):
        csv_file = tmp_path / "multi_bad.csv"
        csv_file.write_text(
            "timestamp_s,velocity_rad_s,measured_current_a\n"
            "0.000,10.5,2.3\n"
            "bad,10.6,2.4\n"
            "0.002\n"
            "0.003,10.8,not_a_number\n"
            "0.004,10.9,2.7\n"
        )

        reader = MotorCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 2
        assert reader.stats['malformed_rows'] == 3


# ──────────────────────────────────────────────────────────────
# 3. Timestamp tracking
# ──────────────────────────────────────────────────────────────

class TestMotorCSVReaderTimestamps:

    def test_monotonic_timestamps_no_gaps(self, valid_motor_csv, test_config):
        reader = MotorCSVReader(valid_motor_csv, test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 0

    def test_non_monotonic_timestamp_flagged(self, tmp_path, test_config):
        csv_file = tmp_path / "bad_ts.csv"
        csv_file.write_text(
            "timestamp_s,velocity_rad_s,measured_current_a\n"
            "0.000,10.5,2.3\n"
            "0.002,10.6,2.4\n"
            "0.001,10.7,2.5\n"   # goes backward
            "0.003,10.8,2.6\n"
        )

        reader = MotorCSVReader(str(csv_file), test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 1

    def test_duplicate_timestamp_flagged(self, tmp_path, test_config):
        csv_file = tmp_path / "dup_ts.csv"
        csv_file.write_text(
            "timestamp_s,velocity_rad_s,measured_current_a\n"
            "0.000,10.5,2.3\n"
            "0.001,10.6,2.4\n"
            "0.001,10.7,2.5\n"   # duplicate
            "0.002,10.8,2.6\n"
        )

        reader = MotorCSVReader(str(csv_file), test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 1

    def test_jitter_within_range_kept_not_dropped(self, tmp_path, test_config):
        """Tiny backward step (within ±0.5ms jitter) flagged as gap but row is kept."""
        csv_file = tmp_path / "jitter.csv"
        csv_file.write_text(
            "timestamp_s,velocity_rad_s,measured_current_a\n"
            "0.000000,10.5,2.3\n"
            "0.001000,10.6,2.4\n"
            "0.000999,10.7,2.5\n"  # 1µs backward — scheduling jitter
            "0.002000,10.8,2.6\n"
        )

        reader = MotorCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 4
        assert reader.stats['timestamp_gaps'] == 1

    def test_multiple_timestamp_gaps(self, tmp_path, test_config):
        csv_file = tmp_path / "multi_gaps.csv"
        csv_file.write_text(
            "timestamp_s,velocity_rad_s,measured_current_a\n"
            "0.000,10.5,2.3\n"
            "0.002,10.6,2.4\n"
            "0.001,10.7,2.5\n"   # gap 1
            "0.003,10.8,2.6\n"
            "0.002,10.9,2.7\n"   # gap 2
            "0.004,11.0,2.8\n"
        )

        reader = MotorCSVReader(str(csv_file), test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 2


# ──────────────────────────────────────────────────────────────
# 4. Stats contract
# ──────────────────────────────────────────────────────────────

class TestMotorCSVReaderStats:

    def test_stats_initial_state(self, tmp_path, test_config):
        csv_file = tmp_path / "motor.csv"
        csv_file.write_text("timestamp_s,velocity_rad_s,measured_current_a\n")

        reader = MotorCSVReader(str(csv_file), test_config)
        stats = reader.get_stats()

        assert stats['total_rows'] == 0
        assert stats['malformed_rows'] == 0
        assert stats['timestamp_gaps'] == 0

    def test_stats_after_parsing(self, tmp_path, test_config):
        csv_file = tmp_path / "motor.csv"
        csv_file.write_text(
            "timestamp_s,velocity_rad_s,measured_current_a\n"
            "0.000,10.5,2.3\n"
            "0.001,10.6,2.4\n"
            "bad,10.7,2.5\n"
            "0.000500,10.8,2.6\n"   # out of order
        )

        reader = MotorCSVReader(str(csv_file), test_config)
        list(reader)
        stats = reader.get_stats()

        assert stats['total_rows'] == 4
        assert stats['malformed_rows'] == 1
        assert stats['timestamp_gaps'] == 1

    def test_get_stats_returns_dict(self, valid_motor_csv, test_config):
        reader = MotorCSVReader(valid_motor_csv, test_config)
        stats = reader.get_stats()

        assert isinstance(stats, dict)
        assert 'total_rows' in stats
        assert 'malformed_rows' in stats
        assert 'timestamp_gaps' in stats

    def test_stats_reset_on_reiteration(self, tmp_path, test_config):
        csv_file = tmp_path / "motor.csv"
        csv_file.write_text(
            "timestamp_s,velocity_rad_s,measured_current_a\n"
            "0.000,10.5,2.3\n"
            "bad,10.6,2.4\n"
            "0.002,10.7,2.5\n"
        )

        reader = MotorCSVReader(str(csv_file), test_config)
        list(reader)
        assert reader.stats['malformed_rows'] == 1

        list(reader)
        assert reader.stats['malformed_rows'] == 1  # reset, not doubled


# ──────────────────────────────────────────────────────────────
# 5. Iterator contract
# ──────────────────────────────────────────────────────────────

class TestMotorCSVReaderIterator:

    def test_iterator_is_reusable(self, valid_motor_csv, test_config):
        reader = MotorCSVReader(valid_motor_csv, test_config)

        rows1 = list(reader)
        rows2 = list(reader)

        assert len(rows1) == len(rows2)
        assert rows1[0] == rows2[0]

    def test_iterator_yields_dicts(self, valid_motor_csv, test_config):
        reader = MotorCSVReader(valid_motor_csv, test_config)

        for row in reader:
            assert isinstance(row, dict)
            assert 'timestamp_s' in row
            assert 'velocity_rad_s' in row
            assert 'measured_current_a' in row
            assert len(row) == 3

    def test_row_values_are_floats(self, valid_motor_csv, test_config):
        reader = MotorCSVReader(valid_motor_csv, test_config)

        for row in reader:
            assert isinstance(row['timestamp_s'], float)
            assert isinstance(row['velocity_rad_s'], float)
            assert isinstance(row['measured_current_a'], float)

    def test_empty_data_yields_nothing(self, tmp_path, test_config):
        csv_file = tmp_path / "motor.csv"
        csv_file.write_text("timestamp_s,velocity_rad_s,measured_current_a\n")

        reader = MotorCSVReader(str(csv_file), test_config)
        assert list(reader) == []


# ──────────────────────────────────────────────────────────────
# 6. Integration against real data
# ──────────────────────────────────────────────────────────────

class TestMotorCSVReaderIntegration:

    def test_real_csv_file_loads(self, test_config):
        data_path = Path(__file__).parent.parent / "data" / "test_motor_1000hz.csv"
        if not data_path.exists():
            pytest.skip("CSV test data not available")

        reader = MotorCSVReader(str(data_path), test_config)
        rows = list(reader)

        assert len(rows) > 0
        stats = reader.get_stats()
        assert stats['total_rows'] >= len(rows)
        assert all(isinstance(row['timestamp_s'], float) for row in rows)

    def test_real_csv_timestamps_monotonic(self, test_config):
        data_path = Path(__file__).parent.parent / "data" / "test_motor_1000hz.csv"
        if not data_path.exists():
            pytest.skip("CSV test data not available")

        reader = MotorCSVReader(str(data_path), test_config)
        rows = list(reader)

        timestamps = [r['timestamp_s'] for r in rows]
        for i in range(1, len(timestamps)):
            assert timestamps[i] > timestamps[i - 1], f"Non-monotonic at index {i}"
