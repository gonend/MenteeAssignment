# Motor Characterization Bench

A Python-based, cross-platform test automation and replay GUI for humanoid robotics motor characterization.

## Features

- **Zero-Hardcoded Parsing:** Packet layout, field offsets, safety thresholds, and output columns are read from YAML at startup. Adding a field to `motor_protocol.yaml` requires no code change.
- **Multi-Rate Synchronization:** Executes a nearest-prior join to align 1000 Hz (motor), 4800 Hz (sensor), and 10 Hz (PSU) streams while accounting for real-world scheduling jitter (±0.5 ms).
- **O(1) Memory Pipeline:** Pure generator architecture streams data row-by-row. RAM footprint is constant whether the input is 5 MB or 50 GB.
- **Thread-Safe GUI:** Decoupled Tkinter UI with live 20 Hz telemetry plotting and deterministic hardware abort via `threading.Event`.

## Installation

Requires Python 3.8+.

```bash
# Create and activate a virtual environment
python -m venv venv

# Linux / macOS
source venv/bin/activate

# Windows
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

> **Note:** The GUI requires `matplotlib`. Install it separately if running the UI:
> ```bash
> pip install matplotlib
> ```

## Usage

### CLI (headless pipeline)

```bash
# Binary motor file
python -m automation.main \
  --config config/test_config.yaml \
  --motor-protocol config/motor_protocol.yaml \
  --motor data/test_motor_1000hz.bin \
  --sensor data/test_sensor_4800hz.csv \
  --psu data/test_psu_10hz.csv \
  --output data/results.csv

# CSV motor file (format auto-detected from .csv extension)
python -m automation.main \
  --config config/test_config.yaml \
  --motor data/test_motor_1000hz.csv \
  --sensor data/test_sensor_4800hz.csv \
  --psu data/test_psu_10hz.csv \
  --output data/results.csv
```

| Flag | Required | Description |
|---|---|---|
| `--config` | Yes | Path to `test_config.yaml` |
| `--motor` | Yes | Motor telemetry file (`.bin` or `.csv`) |
| `--sensor` | Yes | Sensor CSV input (4800 Hz) |
| `--psu` | Yes | PSU CSV input (10 Hz) |
| `--output` | Yes | Merged output CSV destination |
| `--motor-protocol` | Binary only | Path to `motor_protocol.yaml` |
| `--motor-format` | No | Override auto-detection: `binary` or `csv` |
| `--log-level` | No | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default: `INFO`) |

### GUI

```bash
python -m ui.gui
```

Provides file pickers for all inputs, a motor format selector (Binary/CSV), Start/Abort buttons, playback speed control (1×/5×/10×/Max), and a live telemetry panel.

## Running Tests

```bash
# Full suite (293 tests)
pytest tests/ -v

# Single module
pytest tests/test_motor_binary.py -v

# Single class
pytest tests/test_state_machine.py::TestSafetyAbort -v
```

## Project Structure

```
MenteeAssignment/
├── config/
│   ├── motor_protocol.yaml   # Binary packet framing spec (source of truth for drivers)
│   ├── test_config.yaml      # Test phases, safety thresholds, sync strategy, output schema
│   ├── consts.py             # Unit-to-suffix map shared across drivers
│   └── loader.py             # YAML loader with fail-fast field validation
├── drivers/
│   ├── motor.py              # MotorDataSource ABC + MotorBinaryReader + MotorCSVReader
│   ├── sensor.py             # SensorDataSource ABC + SensorCSVReader
│   └── psu.py                # PSUDataSource ABC + PSUCSVReader
├── automation/
│   ├── synchronizer.py       # Nearest-prior join across 3 streams (two-pointer, O(N))
│   ├── row_schema.py         # SchemaProjector — fills absent keys with None sentinel
│   ├── state_machine.py      # YAML-driven 5-phase table, timestamp-driven transitions
│   ├── logger.py             # Streaming CSV writer with efficiency formula
│   └── main.py               # CLI orchestrator (ExitStack, argparse, fail-fast validation)
├── ui/
│   └── gui.py                # MonitoringApp — Tkinter + Matplotlib live telemetry
├── tests/                    # 293 pytest tests across 11 test files
└── data/                     # Input telemetry files and output CSVs
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the threading model and pipeline design.
