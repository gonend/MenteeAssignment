import pytest
import yaml
from pathlib import Path
from drivers.psu import PSUCSVReader


@pytest.fixture
def test_config():
    config_path = Path(__file__).parent.parent / "config" / "test_config.yaml"
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


@pytest.fixture
def valid_psu_csv(tmp_path):
    csv_file = tmp_path / "psu.csv"
    csv_file.write_text(
        "timestamp_s,voltage_v,current_a\n"
        "0.000000,24.0,1.5\n"
        "0.100000,23.8,1.6\n"
        "0.200000,23.6,1.7\n"
        "0.300000,23.4,1.8\n"
    )
    return str(csv_file)


class TestPSUCSVReaderValidation:

    def test_empty_file_raises_error(self, tmp_path, test_config):
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text("")

        with pytest.raises(ValueError, match="empty"):
            PSUCSVReader(str(csv_file), test_config)

    def test_missing_headers_raises_error(self, tmp_path, test_config):
        csv_file = tmp_path / "bad_headers.csv"
        csv_file.write_text("timestamp_s,voltage_v\n0.0,24.0\n")

        with pytest.raises(ValueError, match="Missing headers"):
            PSUCSVReader(str(csv_file), test_config)

    def test_valid_headers_passes(self, valid_psu_csv, test_config):
        reader = PSUCSVReader(valid_psu_csv, test_config)
        assert set(reader.expected_columns.keys()) == {'timestamp_s', 'voltage_v', 'current_a'}

    def test_missing_yaml_key_raises_error(self, tmp_path):
        bad_config = {'data_sources': {}}
        csv_file = tmp_path / "psu.csv"
        csv_file.write_text("timestamp_s,voltage_v,current_a\n0.0,24.0,1.5\n")

        with pytest.raises(ValueError, match="CRITICAL"):
            PSUCSVReader(str(csv_file), bad_config)


class TestPSUCSVReaderParsing:

    def test_valid_rows_parsed(self, valid_psu_csv, test_config):
        reader = PSUCSVReader(valid_psu_csv, test_config)
        rows = list(reader)

        assert len(rows) == 4
        assert rows[0]['timestamp_s'] == 0.0
        assert rows[0]['voltage_v'] == 24.0
        assert rows[0]['current_a'] == 1.5
        assert rows[1]['timestamp_s'] == 0.1

    def test_malformed_row_incorrect_field_count(self, tmp_path, test_config):
        csv_file = tmp_path / "bad_fields.csv"
        csv_file.write_text(
            "timestamp_s,voltage_v,current_a\n"
            "0.0,24.0,1.5\n"
            "0.1,23.8\n"          # Missing current_a
            "0.2,23.6,1.7\n"
        )

        reader = PSUCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 2
        assert reader.stats['malformed_rows'] == 1
        assert reader.stats['total_rows'] == 3

    def test_malformed_row_non_numeric(self, tmp_path, test_config):
        csv_file = tmp_path / "bad_values.csv"
        csv_file.write_text(
            "timestamp_s,voltage_v,current_a\n"
            "0.0,24.0,1.5\n"
            "0.1,INVALID,1.6\n"
            "0.2,23.6,1.7\n"
        )

        reader = PSUCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 2
        assert reader.stats['malformed_rows'] == 1

    def test_multiple_malformed_rows(self, tmp_path, test_config):
        csv_file = tmp_path / "multi_bad.csv"
        csv_file.write_text(
            "timestamp_s,voltage_v,current_a\n"
            "0.0,24.0,1.5\n"
            "bad,23.8,1.6\n"
            "0.2\n"
            "0.3,23.4,not_numeric\n"
            "0.4,23.2,1.9\n"
        )

        reader = PSUCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 2
        assert reader.stats['malformed_rows'] == 3


class TestPSUCSVReaderTimestamps:

    def test_monotonic_timestamps(self, valid_psu_csv, test_config):
        reader = PSUCSVReader(valid_psu_csv, test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 0

    def test_timestamp_non_monotonic(self, tmp_path, test_config):
        csv_file = tmp_path / "bad_timestamps.csv"
        csv_file.write_text(
            "timestamp_s,voltage_v,current_a\n"
            "0.0,24.0,1.5\n"
            "0.2,23.8,1.6\n"
            "0.1,23.6,1.7\n"    # Goes backward
            "0.3,23.4,1.8\n"
        )

        reader = PSUCSVReader(str(csv_file), test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 1

    def test_duplicate_timestamp(self, tmp_path, test_config):
        csv_file = tmp_path / "dup_timestamps.csv"
        csv_file.write_text(
            "timestamp_s,voltage_v,current_a\n"
            "0.0,24.0,1.5\n"
            "0.1,23.8,1.6\n"
            "0.1,23.6,1.7\n"    # Same as previous
            "0.2,23.4,1.8\n"
        )

        reader = PSUCSVReader(str(csv_file), test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 1

    def test_timestamp_jitter_within_range(self, tmp_path, test_config):
        """Tiny backward step (within ±0.5ms jitter) should flag gap but keep row."""
        csv_file = tmp_path / "jitter.csv"
        csv_file.write_text(
            "timestamp_s,voltage_v,current_a\n"
            "0.000000,24.0,1.5\n"
            "0.000100,23.8,1.6\n"
            "0.000099,23.6,1.7\n"  # 1µs backward — scheduling jitter
            "0.000200,23.4,1.8\n"
        )

        reader = PSUCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert len(rows) == 4
        assert reader.stats['timestamp_gaps'] == 1

    def test_multiple_timestamp_gaps(self, tmp_path, test_config):
        csv_file = tmp_path / "multi_gaps.csv"
        csv_file.write_text(
            "timestamp_s,voltage_v,current_a\n"
            "0.0,24.0,1.5\n"
            "0.2,23.8,1.6\n"
            "0.1,23.6,1.7\n"    # gap 1
            "0.3,23.4,1.8\n"
            "0.2,23.2,1.9\n"    # gap 2
            "0.4,23.0,2.0\n"
        )

        reader = PSUCSVReader(str(csv_file), test_config)
        list(reader)

        assert reader.stats['timestamp_gaps'] == 2


class TestPSUCSVReaderStats:

    def test_stats_initial_state(self, tmp_path, test_config):
        csv_file = tmp_path / "psu.csv"
        csv_file.write_text("timestamp_s,voltage_v,current_a\n")

        reader = PSUCSVReader(str(csv_file), test_config)
        stats = reader.get_stats()

        assert stats['total_rows'] == 0
        assert stats['malformed_rows'] == 0
        assert stats['timestamp_gaps'] == 0

    def test_stats_after_parsing(self, tmp_path, test_config):
        csv_file = tmp_path / "psu.csv"
        csv_file.write_text(
            "timestamp_s,voltage_v,current_a\n"
            "0.0,24.0,1.5\n"
            "0.1,23.8,1.6\n"
            "bad,23.6,1.7\n"
            "0.05,23.4,1.8\n"   # Out of order
        )

        reader = PSUCSVReader(str(csv_file), test_config)
        list(reader)
        stats = reader.get_stats()

        assert stats['total_rows'] == 4
        assert stats['malformed_rows'] == 1
        assert stats['timestamp_gaps'] == 1

    def test_get_stats_returns_dict(self, valid_psu_csv, test_config):
        reader = PSUCSVReader(valid_psu_csv, test_config)
        stats = reader.get_stats()

        assert isinstance(stats, dict)
        assert 'total_rows' in stats
        assert 'malformed_rows' in stats
        assert 'timestamp_gaps' in stats

    def test_stats_reset_on_reiteration(self, tmp_path, test_config):
        csv_file = tmp_path / "psu.csv"
        csv_file.write_text(
            "timestamp_s,voltage_v,current_a\n"
            "0.0,24.0,1.5\n"
            "bad,23.8,1.6\n"
            "0.2,23.6,1.7\n"
        )

        reader = PSUCSVReader(str(csv_file), test_config)
        list(reader)
        assert reader.stats['malformed_rows'] == 1

        list(reader)
        assert reader.stats['malformed_rows'] == 1   # Same after re-iter, not doubled


class TestPSUCSVReaderIterator:

    def test_iterator_is_reusable(self, valid_psu_csv, test_config):
        reader = PSUCSVReader(valid_psu_csv, test_config)

        rows1 = list(reader)
        rows2 = list(reader)

        assert len(rows1) == len(rows2)
        assert rows1[0] == rows2[0]

    def test_iterator_yields_dicts(self, valid_psu_csv, test_config):
        reader = PSUCSVReader(valid_psu_csv, test_config)

        for row in reader:
            assert isinstance(row, dict)
            assert 'timestamp_s' in row
            assert 'voltage_v' in row
            assert 'current_a' in row
            assert len(row) == 3

    def test_row_values_are_floats(self, valid_psu_csv, test_config):
        reader = PSUCSVReader(valid_psu_csv, test_config)

        for row in reader:
            assert isinstance(row['timestamp_s'], float)
            assert isinstance(row['voltage_v'], float)
            assert isinstance(row['current_a'], float)

    def test_empty_data_yields_nothing(self, tmp_path, test_config):
        csv_file = tmp_path / "psu.csv"
        csv_file.write_text("timestamp_s,voltage_v,current_a\n")

        reader = PSUCSVReader(str(csv_file), test_config)
        rows = list(reader)

        assert rows == []


class TestPSUCSVReaderIntegration:

    def test_real_test_data(self, test_config):
        data_path = Path(__file__).parent.parent / "data" / "test_psu_10hz.csv"
        if not data_path.exists():
            pytest.skip("Test data not available")

        reader = PSUCSVReader(str(data_path), test_config)
        rows = list(reader)

        assert len(rows) > 0
        stats = reader.get_stats()
        assert stats['total_rows'] >= len(rows)
        assert all(isinstance(row['timestamp_s'], float) for row in rows)
        assert all(isinstance(row['voltage_v'], float) for row in rows)
        assert all(isinstance(row['current_a'], float) for row in rows)

    def test_timestamps_increase_monotonically_in_real_data(self, test_config):
        data_path = Path(__file__).parent.parent / "data" / "test_psu_10hz.csv"
        if not data_path.exists():
            pytest.skip("Test data not available")

        reader = PSUCSVReader(str(data_path), test_config)
        rows = list(reader)

        timestamps = [row['timestamp_s'] for row in rows]
        for i in range(1, len(timestamps)):
            assert timestamps[i] > timestamps[i - 1], f"Non-monotonic at index {i}"
