# Software Architecture & Threading Model

## System Overview

The Motor Characterization Bench is designed around a strictly decoupled, **data-driven pipeline**. It prioritizes zero-hardcoding, robust fault recovery, and an O(1) memory footprint.

The codebase is separated into three domains:

1. **Drivers (`drivers/`)** — Abstract interfaces and format-specific implementations (Binary/CSV) that yield data row-by-row. Automation never sees binary vs. CSV; it speaks only to `MotorDataSource`, `SensorDataSource`, and `PSUDataSource`.
2. **Automation (`automation/`)** — Format-agnostic business logic: time-synchronizer, schema-adapter, state machine, and CSV logger.
3. **User Interface (`ui/`)** — A decoupled Tkinter application for test orchestration and live telemetry.

## Pipeline Data Flow

```
┌─────────────────────────────────────────────────────┐
│  Background Worker Thread                           │
│                                                     │
│  MotorBinaryReader ─┐                               │
│  (or MotorCSVReader) │                              │
│                      ├──► Synchronizer              │
│  SensorCSVReader  ───┤    (nearest-prior join)      │
│                      │         │                    │
│  PSUCSVReader ───────┘         ▼                    │
│                          SchemaProjector            │
│                          (fill absent keys)         │
│                                │                    │
│                                ▼                    │
│                          StateMachine               │
│                          (YAML phase table)         │
│                                │                    │
│                                ▼                    │
│                            Logger                   │
│                          (streaming CSV)            │
│                                │                    │
│              status_queue ◄────┘  abort_event       │
└──────────────────────┬──────────────────────────────┘
                       │ queue.Queue          ▲ threading.Event
┌──────────────────────▼──────────────────────┴──────┐
│  Main Thread (GUI)                                  │
│                                                     │
│  root.after() poll ──► update telemetry labels      │
│  Abort button      ──► abort_event.set()            │
│  WM_DELETE_WINDOW  ──► abort_event.set() + join()   │
└─────────────────────────────────────────────────────┘
```

## Component Map

| File | Class | Responsibility |
|---|---|---|
| `drivers/motor.py` | `MotorBinaryReader` | YAML-driven binary framing; pre-compiled `struct.Struct`; XOR checksum; byte-scan resync |
| `drivers/motor.py` | `MotorCSVReader` | Dynamic column extraction; malformed-row skip; monotonicity tracking |
| `drivers/sensor.py` | `SensorCSVReader` | Same contract as `MotorCSVReader`; 4800 Hz stream |
| `drivers/psu.py` | `PSUCSVReader` | Same contract; 10 Hz stream |
| `automation/synchronizer.py` | `Synchronizer` | Nearest-prior join; two-pointer scan; O(N_motor + N_sensor + N_psu) |
| `automation/row_schema.py` | `SchemaProjector` | Fills absent keys with `None` sentinel during bootstrap window |
| `automation/state_machine.py` | `StateMachine` | YAML phase table; timestamp-driven transitions; safety + manual-abort |
| `automation/logger.py` | `Logger` | Streaming `csv.DictWriter`; efficiency formula compiled once via `compile()` |
| `automation/main.py` | `run_pipeline` | Wires all components; O(1) row loop; `ExitStack` teardown |
| `ui/gui.py` | `MonitoringApp` | Tkinter event loop; `queue.Queue` telemetry poll; `threading.Event` abort |

## Threading Model

### Main Thread (GUI)

- Runs the Tkinter event loop and handles all user interactions.
- Polls the telemetry queue via `root.after()` at a YAML-defined rate (e.g., 20 Hz) without blocking the UI.

### Background Worker Thread

- Spawned when the user clicks "Start Test".
- Runs the full generator chain: `Drivers → Synchronizer → SchemaProjector → StateMachine → Logger`.
- Processes data at Max playback speed without interfering with UI responsiveness.

### Cross-Thread Communication

| Direction | Primitive | Behavior |
|---|---|---|
| Pipeline → GUI (telemetry) | `queue.Queue` (bounded) | Worker calls `put_nowait()` — drops the frame if the queue is full, preventing backpressure from blocking the 1000 Hz loop |
| GUI → Pipeline (abort) | `threading.Event` | GUI calls `.set()`; worker checks `.is_set()` once per row — atomic, zero-overhead |
| Shutdown | `threading.Event` + `worker.join(timeout=2s)` | `WM_DELETE_WINDOW` sets the event and joins the thread before destroying the window |

## Synchronization Algorithm

The motor stream (1000 Hz) is the **primary clock**. For each motor sample at timestamp `t_m`:

1. Advance the sensor pointer to the latest sample with `t_s ≤ t_m`.
2. Advance the PSU pointer to the latest sample with `t_p ≤ t_m`.
3. Merge all three rows into one output record.

This is a **nearest-prior join** — it never interpolates or fabricates synthetic samples. Real-world scheduling jitter (±0.5 ms) is handled correctly because the algorithm uses timestamp comparison only, never index arithmetic. The sensor at 4800 Hz has a mean spacing of ≈208 µs; jitter can exceed this spacing, so index-based approaches would produce incorrect alignments.

A `SchemaProjector` wraps the `Synchronizer` output to fill absent keys with a `None` sentinel during the bootstrap window (first few rows before all streams have contributed their first sample). This prevents `KeyError` and `None > float` comparison failures in the `StateMachine`.

## State Machine

Phases are loaded from `test_config.yaml` — no phase name is hardcoded. The default configuration defines five phases:

```
SETUP → CURRENT_RAMP → TORQUE_HOLD → VOLTAGE_DECREASE → COMPLETE
```

Transitions are **timestamp-driven** (not wall-clock):

| Transition | Condition |
|---|---|
| `CURRENT_RAMP → TORQUE_HOLD` | `\|torque\| ≥ target_torque_nm` OR `\|current\| ≥ max_current_a` |
| `TORQUE_HOLD → VOLTAGE_DECREASE` | `hold_duration_s` of data timestamps elapsed |
| `VOLTAGE_DECREASE → COMPLETE` | `voltage ≤ min_voltage_v` OR data exhausted |
| Any phase → `COMPLETE` (safety) | `\|torque\| > max_torque_nm` — immediate abort |
| Any phase → `COMPLETE` (manual) | GUI Abort button sets `abort_event` |

## Memory Model (O(1) Footprint)

The system never loads entire files into memory. Each component in the chain is a Python generator that yields one row at a time. The RAM footprint remains constant whether processing a 5 MB test file or a 50 GB production log. This is verified by `tests/test_stress.py`, which asserts less than 500 KB of heap growth over a 500 000-row synthetic stream.

## Zero-Hardcoding Guarantee

No structural logic, packet offsets, safety thresholds, phase parameters, or output column names are hardcoded.

- `MotorBinaryReader` builds its `struct.unpack` format string from `motor_protocol.yaml` at init.
- `StateMachine` loads phase names, transition conditions, and thresholds from `test_config.yaml`.
- `Logger` derives output column order from `test_config.yaml → output.columns`.
- Adding a field to either YAML file requires no code change.
