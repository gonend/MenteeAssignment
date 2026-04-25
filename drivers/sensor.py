import csv
import logging
from typing import Iterator, Dict, Any
import abc

from config.consts import YAML_TYPE_MAP

logger = logging.getLogger(__name__)

class SensorDataSource(abc.ABC):
    """Abstract interface for torque sensor data."""
    @abc.abstractmethod
    def __iter__(self) -> Iterator[Dict[str, Any]]:
        pass
        
    @abc.abstractmethod
    def get_stats(self) -> Dict[str, Any]:
        pass

class SensorCSVReader(SensorDataSource):
    def __init__(self, file_path: str, test_config: Dict[str, Any]):
        self.file_path = file_path
        self.stats = {"total_rows": 0, "malformed_rows": 0, "timestamp_gaps": 0}
        

    # 1. Zero Hardcoding Rule: Dynamic Column & Type Extraction
        try:
            csv_config = test_config['data_sources']['sensor']['formats']['csv']['columns']
            # Build a dictionary of {column_name: casting_function}
            self.expected_columns = {}
            for col in csv_config:
                col_name = col['name']
                col_type_str = col['type']
                
                if col_type_str not in YAML_TYPE_MAP:
                    raise ValueError(f"CRITICAL: Unsupported type '{col_type_str}' for column '{col_name}'")
                    
                self.expected_columns[col_name] = YAML_TYPE_MAP[col_type_str]
                
        except KeyError as e:
            raise ValueError(f"CRITICAL: Missing required YAML config: {e}")
        # 2. Startup Validation: Catch empty files or missing headers immediately
        self._validate_file()

    def _validate_file(self):
        # FIX 1: Use utf-8-sig to handle Windows/Excel Byte Order Marks
        with open(self.file_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            try:
                # FIX 2: Strip whitespace from all headers to prevent key mismatch
                raw_headers = next(reader)
                headers = [h.strip() for h in raw_headers if h.strip()]
            except StopIteration:
                raise ValueError(f"CRITICAL: CSV file {self.file_path} is empty.")
            
            if not all(col in headers for col in self.expected_columns.keys()):
                raise ValueError(
                    f"CRITICAL: Missing headers in {self.file_path}. "
                    f"Expected {list(self.expected_columns.keys())}, found {headers}."
                )

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        self.stats = {"total_rows": 0, "malformed_rows": 0, "timestamp_gaps": 0}
        
        # Use utf-8-sig here as well
        with open(self.file_path, 'r', encoding='utf-8-sig') as f:
            # Extract clean headers to use as dictionary keys
            reader_obj = csv.reader(f)
            try:
                clean_fieldnames = [h.strip() for h in next(reader_obj)]
            except StopIteration:
                return # File is empty, safely exit generator
                
            # Pass the cleaned fieldnames to DictReader
            reader = csv.DictReader(f, fieldnames=clean_fieldnames)
            last_timestamp = -1.0
            
            for row_idx, row in enumerate(reader, start=2): # row 1 is the header
                self.stats["total_rows"] += 1
                
                # PDF Constraint: Skip malformed row (wrong field count)
                if None in row.values() or None in row.keys():
                    logger.warning(f"Row {row_idx} malformed: incorrect field count. Skipping.")
                    self.stats["malformed_rows"] += 1
                    continue

                # 7. Dynamic Type Conversion Check
                try:
                    parsed_row = {}
                    for col_name, cast_func in self.expected_columns.items():
                        parsed_row[col_name] = cast_func(row[col_name])
                except ValueError:
                    logger.warning(f"Row {row_idx} malformed: invalid type conversion. Skipping.")
                    self.stats["malformed_rows"] += 1
                    continue

                # PDF Constraint: Track timestamp gaps / non-monotonicity
                current_ts = parsed_row['timestamp_s']
                if current_ts <= last_timestamp:
                    logger.warning(f"Timestamp issue at row {row_idx}: {current_ts} follows {last_timestamp}.")
                    self.stats["timestamp_gaps"] += 1
                
                last_timestamp = current_ts
                yield parsed_row

    def get_stats(self) -> Dict[str, Any]:
        return self.stats