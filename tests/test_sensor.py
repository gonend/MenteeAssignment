from csv import reader

import pytest
import tempfile
import os
import yaml
from pathlib import Path
from drivers.sensor import SensorCSVReader


@pytest.fixture
def test_config():
    """Load test configuration from test_config.yaml."""
    config_path = Path(__file__).parent.parent / "config" / "test_config.yaml"
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


@pytest.fixture
def valid_sensor_csv(tmp_path):
    """Create valid sensor CSV file."""
    csv_file = tmp_path / "sensor.csv"
    csv_file.write_text(
        "timestamp_s,torque_nm\n"
        "0.000000,-0.000720\n"
        "0.000214,0.008050\n"
        "0.000384,-0.001085\n"
        "0.000588,-0.000039\n"
    )
    return str(csv_file)


class TestSensorCSVReaderValidation:
    """Test file validation at startup."""

    def test_empty_file_raises_error(self, tmp_path, test_config):
        """Empty CSV should raise ValueError on init."""
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text("")

        with pytest.raises(ValueError, match="empty"):
            SensorCSVReader(str(csv_file), test_config)

    def test_missing_headers_raises_error(self, tmp_path, test_config):
        """CSV with missing required headers should raise ValueError."""
        csv_file = tmp_path / "bad_headers.csv"
        csv_file.write_text("timestamp_s\n0.0\n")

        with pytest.raises(ValueError, match="Missing headers"):
            SensorCSVReader(str(csv_file), test_config)

    def test_valid_headers_passes(self, valid_sensor_csv, test_config):
        """Valid headers should pass initialization."""
        reader = SensorCSVReader(valid_sensor_csv, test_config)
        assert list(reader.expected_columns.keys()) == ['timestamp_s', 'torque_nm']

    def test_missing_yaml_key_raises_error(self, tmp_path):
        """Missing required config key should raise ValueError with CRITICAL."""
        bad_config = {'data_sources': {}}
        csv_file = tmp_path / "sensor.csv"
        csv_file.write_text("timestamp_s,torque_nm\n0.0,-0.001\n")

        with pytest.raises(ValueError, match="CRITICAL"):
            SensorCSVReader(str(csv_file), bad_config)

class TestSensorCSVReaderParsing:
    """Test CSV parsing and row handling."""

    def test_valid_rows_parsed(self, valid_sensor_csv, test_config):
        """Valid rows should be parsed and yielded."""
        reader = SensorCSVReader(valid_sensor_csv, test_config)
        rows = list(reader)

        assert len(rows) == 4
        assert rows[0]['timestamp_s'] == 0.0
        assert rows[0]['torque_nm'] == -0.000720
        assert rows[1]['timestamp_s'] == 0.000214
        assert rows[1]['torque_nm'] == 0.008050

    def test_malformed_row_incorrect_field_count(self, tmp_path, test_config):
        """Rows with missing fields should be skipped."""
        csv_file = tmp_path / "bad_fields.csv"
        csv_file.write_text(
            "timestamp_s,torque_nm\n"
            "0.0,1.5\n"
            "0.1\n"  # Missing torque_nm
            "0.2,2.5\n"
        )

        reader = SensorCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 2
        assert reader.stats['malformed_rows'] == 1
        assert reader.stats['total_rows'] == 3

    def test_malformed_row_non_numeric(self, tmp_path, test_config):
        """Rows with non-numeric values should be skipped."""
        csv_file = tmp_path / "bad_values.csv"
        csv_file.write_text(
            "timestamp_s,torque_nm\n"
            "0.0,1.5\n"
            "not_a_number,2.0\n"
            "0.2,2.5\n"
        )

        reader = SensorCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 2
        assert reader.stats['malformed_rows'] == 1

    def test_multiple_malformed_rows(self, tmp_path, test_config):
        """Multiple malformed rows should all be tracked."""
        csv_file = tmp_path / "multi_bad.csv"
        csv_file.write_text(
            "timestamp_s,torque_nm\n"
            "0.0,1.5\n"
            "bad,2.0\n"
            "0.2\n"
            "0.3,not_numeric\n"
            "0.4,3.5\n"
        )

        reader = SensorCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 2
        assert reader.stats['malformed_rows'] == 3


class TestSensorCSVReaderTimestamps:
    """Test timestamp handling and gap detection."""

    def test_monotonic_timestamps(self, valid_sensor_csv, test_config):
        """Monotonic increasing timestamps should not trigger gap detection."""
        reader = SensorCSVReader(valid_sensor_csv, test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 0

    def test_timestamp_non_monotonic(self, tmp_path, test_config):
        """Non-monotonic timestamps should be flagged as gaps."""
        csv_file = tmp_path / "bad_timestamps.csv"
        csv_file.write_text(
            "timestamp_s,torque_nm\n"
            "0.0,1.5\n"
            "0.2,2.0\n"
            "0.1,2.5\n"  # Goes backward
            "0.3,3.0\n"
        )

        reader = SensorCSVReader(str(csv_file), test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 1

    def test_duplicate_timestamp(self, tmp_path, test_config):
        """Duplicate (equal) timestamps should be flagged."""
        csv_file = tmp_path / "dup_timestamps.csv"
        csv_file.write_text(
            "timestamp_s,torque_nm\n"
            "0.0,1.5\n"
            "0.1,2.0\n"
            "0.1,2.5\n"  # Same as previous
            "0.2,3.0\n"
        )

        reader = SensorCSVReader(str(csv_file), test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 1

    def test_timestamp_jitter_within_range(self, tmp_path, test_config):
        """Tiny backward step (within ±0.5ms jitter) should flag gap but keep row."""
        csv_file = tmp_path / "jitter.csv"
        csv_file.write_text(
            "timestamp_s,torque_nm\n"
            "0.000000,1.5\n"
            "0.000100,2.0\n"
            "0.000099,2.5\n"  # 1µs backward — scheduling jitter
            "0.000200,3.0\n"
        )

        reader = SensorCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 4
        assert reader.stats['timestamp_gaps'] == 1

    def test_multiple_timestamp_gaps(self, tmp_path, test_config):
        """Multiple non-monotonic timestamps should all be counted."""
        csv_file = tmp_path / "multi_gaps.csv"
        csv_file.write_text(
            "timestamp_s,torque_nm\n"
            "0.0,1.5\n"
            "0.2,2.0\n"
            "0.1,2.5\n"    # gap 1
            "0.3,3.0\n"
            "0.2,3.5\n"    # gap 2
            "0.4,4.0\n"
        )

        reader = SensorCSVReader(str(csv_file), test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 2


class TestSensorCSVReaderStats:
    """Test statistics collection."""

    def test_stats_initial_state(self, test_config, tmp_path):
        """Stats should be initialized correctly."""
        csv_file = tmp_path / "sensor.csv"
        csv_file.write_text("timestamp_s,torque_nm\n")

        reader = SensorCSVReader(str(csv_file), test_config)
        stats = reader.get_stats()

        assert stats['total_rows'] == 0
        assert stats['malformed_rows'] == 0
        assert stats['timestamp_gaps'] == 0

    def test_stats_after_parsing(self, tmp_path, test_config):
        """Stats should be accurate after parsing."""
        csv_file = tmp_path / "sensor.csv"
        csv_file.write_text(
            "timestamp_s,torque_nm\n"
            "0.0,1.5\n"
            "0.1,2.0\n"
            "bad,3.0\n"
            "0.05,4.0\n"  # Out of order
        )

        reader = SensorCSVReader(str(csv_file), test_config)
        list(reader)  # Must consume iterator to populate stats
        stats = reader.get_stats()

        assert stats['total_rows'] == 4
        assert stats['malformed_rows'] == 1
        assert stats['timestamp_gaps'] == 1

    def test_get_stats_returns_dict(self, valid_sensor_csv, test_config):
        """get_stats should return a dictionary."""
        reader = SensorCSVReader(valid_sensor_csv, test_config)
        stats = reader.get_stats()

        assert isinstance(stats, dict)
        assert 'total_rows' in stats
        assert 'malformed_rows' in stats
        assert 'timestamp_gaps' in stats

    def test_stats_reset_on_reiteration(self, tmp_path, test_config):
        """Re-iterating should not double stats."""
        csv_file = tmp_path / "sensor.csv"
        csv_file.write_text(
            "timestamp_s,torque_nm\n"
            "0.0,1.5\n"
            "bad,-0.001\n"
            "0.2,2.5\n"
        )

        reader = SensorCSVReader(str(csv_file), test_config)
        list(reader)
        assert reader.stats['malformed_rows'] == 1

        list(reader)
        assert reader.stats['malformed_rows'] == 1   # Same after re-iter, not doubled


class TestSensorCSVReaderIterator:
    """Test iterator behavior."""

    def test_iterator_is_reusable(self, valid_sensor_csv, test_config):
        """SensorCSVReader should be iterable multiple times."""
        reader = SensorCSVReader(valid_sensor_csv, test_config)

        rows1 = list(reader)
        rows2 = list(reader)

        assert len(rows1) == len(rows2)
        assert rows1[0] == rows2[0]

    def test_iterator_yields_dicts(self, valid_sensor_csv, test_config):
        """Each row should be a dictionary with correct keys."""
        reader = SensorCSVReader(valid_sensor_csv, test_config)

        for row in reader:
            assert isinstance(row, dict)
            assert 'timestamp_s' in row
            assert 'torque_nm' in row
            assert len(row) == 2

    def test_row_values_are_floats(self, valid_sensor_csv, test_config):
        """All row values should be floats."""
        reader = SensorCSVReader(valid_sensor_csv, test_config)

        for row in reader:
            assert isinstance(row['timestamp_s'], float)
            assert isinstance(row['torque_nm'], float)

    def test_empty_data_yields_nothing(self, tmp_path, test_config):
        """Headers-only CSV should yield no rows."""
        csv_file = tmp_path / "sensor.csv"
        csv_file.write_text("timestamp_s,torque_nm\n")

        reader = SensorCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert rows == []


class TestSensorCSVReaderIntegration:
    """Integration tests with real data."""

    def test_real_test_data(self, test_config):
        """Test with actual test data file."""
        data_path = Path(__file__).parent.parent / "data" / "test_sensor_4800hz.csv"
        if not data_path.exists():
            pytest.skip("Test data not available")

        reader = SensorCSVReader(str(data_path), test_config)
        rows = list(reader)

        assert len(rows) > 0
        stats = reader.get_stats()
        assert stats['total_rows'] >= len(rows)
        assert all(isinstance(row['timestamp_s'], float) for row in rows)
        assert all(isinstance(row['torque_nm'], float) for row in rows)

    def test_timestamps_increase_monotonically_in_real_data(self, test_config):
        """Real data should have monotonic timestamps after filtering."""
        data_path = Path(__file__).parent.parent / "data" / "test_sensor_4800hz.csv"
        if not data_path.exists():
            pytest.skip("Test data not available")

        reader = SensorCSVReader(str(data_path), test_config)
        rows = list(reader)

        timestamps = [row['timestamp_s'] for row in rows]
        for i in range(1, len(timestamps)):
            assert timestamps[i] > timestamps[i-1], f"Non-monotonic at index {i}"
...