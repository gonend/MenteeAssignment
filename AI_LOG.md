# AI Decision Log — Motor Characterization Bench

**Purpose:** Documents AI suggestions, manual interventions, and architectural justifications during development. Structured by development phase. Emphasis on safety-critical design decisions.

**Format per phase:** Three-section entries — _AI Suggestion_, _My Correction/Intervention_, _Architectural Justification_.

---

## Phase 1 — Config Layer (`config/loader.py`, `config/consts.py`)

### Entry 1.1 — Type Mapping

| Section | Content |
|---|---|
| **AI Suggestion** | Inline `if field['type'] == "float32": float(...)` chains per field during parse |
| **My Correction** | `consts.YAML_TYPE_MAP`: module-level dict mapping YAML type strings → Python cast functions; applied uniformly in binary reader and all CSV readers |
| **Architectural Justification** | O(1) lookup at parse time. Adding a new YAML type requires one dict entry — zero structural changes elsewhere. Inline chains scatter type-handling logic across every driver; a new type would require hunting every site. |

### Entry 1.2 — YAML Startup Validation

| Section | Content |
|---|---|
| **AI Suggestion** | Return `None` or a default value when a YAML field is missing |
| **My Correction** | `ValueError` with full dotted field path (e.g., `"framing.start_marker.bytes"`) raised at `__init__` |
| **Architectural Justification** | PDF Error Matrix row 8: "startup fail with full field path." Silent defaults mask schema drift between YAML and code; they surface only at runtime as cryptic `IndexError`s or wrong values. Fail-fast at startup prevents partial output from being produced. |

---

## Phase 2 — CSV Drivers: PSU + Sensor (`drivers/psu.py`, `drivers/sensor.py`)

### Entry 2.1 — Inheritance vs. Independent Classes

| Section | Content |
|---|---|
| **AI Suggestion** | Shared `BaseCSVReader` with common `__iter__`, column-loading, and validation logic |
| **My Correction** | Independent `PSUCSVReader` and `SensorCSVReader` — no shared base class, duplicate-and-vary pattern |
| **Architectural Justification** | PSU and sensor have different YAML config paths and will diverge on edge cases. A shared base binds both drivers to a common lifecycle; a change to one silently risks the other. Reviewability also favors self-contained drivers — each is independently readable without tracing inheritance. |

### Entry 2.2 — Stats Reset Timing

| Section | Content |
|---|---|
| **AI Suggestion** | Reset `stats` dict in `__init__` |
| **My Correction** | Stats reset at top of `__iter__`, not at `__init__` |
| **Architectural Justification** | Drivers are reusable across multiple pipeline runs without re-instantiation. Resetting at `__init__` would zero stats from a prior partially-consumed run, hiding errors in the summary. Resetting at `__iter__` gives each run a clean baseline while preserving prior-run data until iteration begins. |

### Entry 2.3 — Non-Monotonic Timestamp Handling

| Section | Content |
|---|---|
| **AI Suggestion** | Halt iteration or raise on non-monotonic timestamps |
| **My Correction** | Increment `stats["timestamp_gaps"]`, continue iteration |
| **Architectural Justification** | PDF Error Matrix: "continue with available data, NEVER fabricate/interpolate" for CSV gaps. Halting would fail tests on real hardware data with minor scheduling jitter. The counter exposes the anomaly in the run summary without censoring the stream. |

---

## Phase 3 — Motor CSV Reader (`drivers/motor.py::MotorCSVReader`)

### Entry 3.1 — Column Config Path

| Section | Content |
|---|---|
| **AI Suggestion** | Reuse PSU column extraction logic, pass config path as constructor argument |
| **My Correction** | Hardcode YAML path `data_sources.motor.formats.csv.columns` in `MotorCSVReader.__init__`; mirror PSU/Sensor pattern exactly otherwise |
| **Architectural Justification** | Uniform contract across all three CSV drivers lets the automation layer call `iter(source)` on any driver without format knowledge. A configurable base-class path argument would add abstraction with no benefit — all three paths are fixed by their respective YAML sections. |

---

## Phase 4 — Motor Binary Reader (`drivers/motor.py::MotorBinaryReader`)

### Entry 4.1 — ZERO HARDCODING: Struct Compilation

| Section | Content |
|---|---|
| **AI Suggestion** | Hardcode packet offsets directly in Python: `data[8:12]` for velocity, `data[12:14]` for current, etc. |
| **My Correction** | All offsets, field sizes, type formats, and response codes read from `motor_protocol.yaml` at `__init__`. One pre-compiled `struct.Struct` per response code stored in `_response_structs[code]`. Zero hardcoded offsets anywhere. |
| **Architectural Justification** | PDF must-pass criterion: "Add field to YAML → no code change." Hardcoded offsets would fail this check at evaluation. Pre-compilation validates the struct format string once at startup — catches bad YAML loudly rather than silently per-packet at runtime. |

### Entry 4.2 — BINARY RESILIENCE: Marker Scanning

| Section | Content |
|---|---|
| **AI Suggestion** | Simple block-read: advance `pos` by full packet size after each parse |
| **My Correction** | `bytes.find(start_marker, pos)` for marker scanning. On checksum failure: backtrack `pos` to `pkt_start + 1` (not `pkt_end`) and re-run `find()`. Start marker value read from `framing.start_marker.bytes` — never hardcoded as `0xAA 0x55`. |
| **Architectural Justification** | PDF Error Matrix: "scan byte-by-byte to next `0xAA 0x55`, realign" on shifted marker. Block-advancing skips everything between a corrupt packet boundary and the next valid marker. 1-byte backtrack ensures no valid start marker is skipped when `payload_size` is corrupted. `bytes.find()` delegates to C runtime — ~100× faster than Python loop on large files. |

### Entry 4.3 — Truncation Handling

| Section | Content |
|---|---|
| **AI Suggestion** | `continue` on truncated read to allow iteration to proceed |
| **My Correction** | `break` on `hdr_end > len(data)` or `em_end > len(data)` |
| **Architectural Justification** | At EOF with incomplete packet, no complete packets can follow. `continue` loops forever on the same oversized offset. PDF Error Matrix: "discard partial, report in summary." `break` satisfies this without special-casing EOF as a separate state. |

### Entry 4.4 — Output Key Naming

| Section | Content |
|---|---|
| **AI Suggestion** | Hardcode output keys: `"velocity_rad_s"`, `"motor_current_a"`, etc. |
| **My Correction** | `_UNIT_SUFFIX_MAP` module-level dict maps YAML unit strings → Python key suffixes (`"rad/s"` → `"_rad_s"`, `"A"` → `"_a"`). Output key = `field.name + _UNIT_SUFFIX_MAP[field.unit]` |
| **Architectural Justification** | Hardcoding final key strings would silently fail if YAML renames a field. The suffix map is the YAML-driven derivation — adding a new YAML unit requires one entry in the map, not a scattered search for hardcoded strings. |

---

## Phase 5 — Synchronizer (`automation/synchronizer.py`)

### Entry 5.1 — SYNCHRONIZATION: Join Algorithm

| Section | Content |
|---|---|
| **AI Suggestion** | `pandas.read_csv` + `merge_asof` for multi-rate alignment |
| **My Correction** | Two-pointer lazy-advance nearest-prior join. Motor is primary clock. For each motor sample at `t_m`: advance sensor pointer while `t_sensor_next ≤ t_m`, advance PSU pointer while `t_psu_next ≤ t_m`. Timestamp comparison only — no index arithmetic. |
| **Architectural Justification** | PDF Anti-Pattern: "`pandas.merge_asof` without per-sample timestamp validation hides jitter handling." Sensor runs at 4800 Hz (mean spacing ≈ 208 µs); jitter envelope is ±0.5 ms — jitter exceeds spacing. Any `i = t × rate` index arithmetic produces wrong alignment. `merge_asof` without explicit jitter handling is an opaque black box that passes tests on clean data but silently corrupts output under real scheduling variation. O(N_motor + N_sensor + N_psu) total; no buffering; O(1) memory. |

### Entry 5.2 — Bootstrap Window: Absent Fields

| Section | Content |
|---|---|
| **AI Suggestion** | Synchronizer injects `{field: None}` for all absent keys during bootstrap |
| **My Correction** | Synchronizer omits absent fields entirely. `SchemaProjector` adapter (separate layer) fills absent keys with `None` sentinel for downstream consumers. |
| **Architectural Justification** | Injecting `None` in the synchronizer would require reading YAML to know which keys to inject — violating the synchronizer's YAML-agnosticism. Absent fields are detectable downstream (`"torque_nm" not in row`). A separate adapter cleanly separates concerns and keeps both Synchronizer and StateMachine independently testable. |

### Entry 5.3 — Motor Fields Precedence on Collision

| Section | Content |
|---|---|
| **AI Suggestion** | Merge order unspecified — dict update order arbitrary |
| **My Correction** | Explicit merge order: `psu → sensor → motor`. Motor `update()` runs last. |
| **Architectural Justification** | All three drivers emit `timestamp_s`. Without explicit precedence, the PSU's 10 Hz timestamp would overwrite the 1000 Hz motor timestamp in the merged row, silently corrupting the downstream join key used by the state machine. |

---

## Phase 6 — Schema Projector (`automation/row_schema.py`)

### Entry 6.1 — None-Guard Strategy

| Section | Content |
|---|---|
| **AI Suggestion** | Add `None` guards in every state machine condition: `if row.get("torque_nm") is not None and ...` |
| **My Correction** | `SchemaProjector` stateless adapter: caller supplies `expected_keys` from YAML; adapter fills absent keys with `None` sentinel; never overwrites real values; never fabricates numbers |
| **Architectural Justification** | Scattering `None` guards across state machine conditions makes it harder to read and test. A stateless adapter with a clear contract (`None` = "no measurement yet, not fabricated") keeps both Synchronizer and StateMachine pure. The `None` vs `0.0` distinction is preserved for the logger — which writes `""` for `None` but `"0.0"` for a fabricated value. |

---

## Phase 7 — State Machine (`automation/state_machine.py`)

### Entry 7.1 — STATE MACHINE: Timestamp-Driven Transitions

| Section | Content |
|---|---|
| **AI Suggestion** | `time.sleep(hold_duration_s)` to pace `TORQUE_HOLD` phase; wall-clock timer for transitions |
| **My Correction** | Transitions driven by data timestamps (`t_current - t_phase_start >= hold_duration_s`). `hold_duration_s` read from YAML `phases[].parameters`. Zero `time.sleep` calls. |
| **Architectural Justification** | PDF Anti-Pattern: "`time.sleep` for phase pacing — transitions must be timestamp-driven." Wall-clock pacing breaks replay at non-realtime speeds (1×/5×/10×/Max). Timestamp-driven transitions work identically at all playback speeds and allow automated testing without actual waiting. |

### Entry 7.2 — Push vs. Pull Model

| Section | Content |
|---|---|
| **AI Suggestion** | Pull-model: state machine owns `__iter__`, pulls rows from pipeline |
| **My Correction** | Push-model: `process(row)` — caller feeds one row, receives one augmented row back |
| **Architectural Justification** | Pull-model requires wrapping the entire pipeline in another generator layer to inject the abort event. Push-model lets the orchestrator drive the loop and call `sm.process(row)` inline. Abort signal trivially testable: set event, call `process()`, assert `COMPLETE` — no threading required in unit tests. |

### Entry 7.3 — Abort Priority Order

| Section | Content |
|---|---|
| **AI Suggestion** | Check phase transitions first, then safety, then abort |
| **My Correction** | Priority: `abort_event.is_set()` → `|torque| > max_torque_nm` → phase handler → default |
| **Architectural Justification** | Manual abort must be instantaneous regardless of data state. Safety must override phase logic — a torque spike during `TORQUE_HOLD` must abort immediately, not wait for hold to complete. Phase transitions run only after safety is clear. Maps directly to PDF safety requirements. |

### Entry 7.4 — Peak Tracking with `abs()`

| Section | Content |
|---|---|
| **AI Suggestion** | `max(current_peak, row["torque_nm"])` — unsigned max |
| **My Correction** | `max(current_peak, abs(row["torque_nm"]))` for both `peak_torque_nm` and `peak_current_a` |
| **Architectural Justification** | Motor torque and current are signed quantities. Without `abs()`, a bidirectional test sequence silently reports a lower-than-actual peak. PDF safety thresholds apply to magnitude — a −200 Nm spike is as dangerous as +200 Nm. This is a safety reporting failure, not a cosmetic issue. |

### Entry 7.5 — Handler Dispatch by Name

| Section | Content |
|---|---|
| **AI Suggestion** | `_phase_names[i+1]` index-based dispatch |
| **My Correction** | `_handlers = Dict[str, Callable]` keyed on phase name strings from YAML. Dispatch: `self._handlers[self._phase_name](row, t)` |
| **Architectural Justification** | Index-based routing silently mis-routes on YAML reorder. Name-based dispatch fails fast at `__init__` with `ValueError` if YAML declares a phase with no registered handler — surfaces schema changes loudly at startup rather than as a silent mis-dispatch at runtime. |

---

## Phase 8 — Logger (`automation/logger.py`)

### Entry 8.1 — None Sanitization

| Section | Content |
|---|---|
| **AI Suggestion** | Pass row dict directly to `csv.DictWriter.writerow()` |
| **My Correction** | Explicit `None → ""` sanitization step before `csv.DictWriter` |
| **Architectural Justification** | `csv.DictWriter` writes Python `None` as the literal string `"None"`. Downstream consumers see a non-numeric token where a missing measurement should be empty. `""` is the standard CSV representation of "no value" and parses correctly as `NaN` in pandas/numpy. |

### Entry 8.2 — Efficiency Formula via `compile()` + `eval()`

| Section | Content |
|---|---|
| **AI Suggestion** | Hardcode `η = torque_nm * velocity_rad_s / (psu_voltage_v * psu_current_a)` in logger |
| **My Correction** | `compile(formula_string, "<yaml>", "eval")` at `__init__`. Per-row: `eval(compiled, {}, safe_dict)`. Formula string from YAML. `ZeroDivisionError` + `TypeError` → `None` → `""`. |
| **Architectural Justification** | Hardcoded formula diverges from YAML on efficiency spec changes. `compile()` validates syntax at startup — catches malformed YAML immediately. Per-row `eval` avoids re-parsing 1000 times/sec. Restricted namespace prevents formula from accessing builtins — limits injection surface. |

---

## Phase 9 — Pipeline Orchestrator (`automation/main.py`)

### Entry 9.1 — Field Name Translation

| Section | Content |
|---|---|
| **AI Suggestion** | Rename fields inside each driver to match output schema |
| **My Correction** | `_FIELD_RENAME` static dict in orchestrator: `measured_current_a → motor_current_a`, `voltage_v → psu_voltage_v`, `current_a → psu_current_a` |
| **Architectural Justification** | Driver field names follow YAML source naming (field name + unit suffix). Output schema uses semantic names. Centralizing translation in the orchestrator makes the mapping auditable in one place. Drivers remain pure to their YAML contract; the orchestrator owns the adapter concern. |

### Entry 9.2 — Key Derivation from YAML Only

| Section | Content |
|---|---|
| **AI Suggestion** | Peek first row from each driver to discover column keys |
| **My Correction** | `_derive_expected_keys()` reads YAML `data_sources.*` paths only — never peeks driver output |
| **Architectural Justification** | Peeking the first sample consumes a row before `SchemaProjector` is initialized, breaking bootstrap. YAML-driven derivation is side-effect-free and available before any file is opened. |

### Entry 9.3 — Throughput Measurement

| Section | Content |
|---|---|
| **AI Suggestion** | `elapsed = end_time - start_time`; `rows_per_s = total_rows / elapsed` |
| **My Correction** | `time.perf_counter` brackets pipeline loop. Pacing sleep durations subtracted from `processing_s`. Sentinel exposes `throughput_rows_per_s` / `processing_s` / `elapsed_wall_s`. |
| **Architectural Justification** | PDF requires "Track and report parsing throughput." Without subtracting pacing sleeps, throughput at 1× speed reports ~1000 rows/s regardless of parser performance — meaningless as benchmark. `processing_s` reflects true parser throughput; `elapsed_wall_s` reflects real-time playback. |

---

## Phase 10 — GUI (`ui/gui.py`)

### Entry 10.1 — Cross-Thread Communication

| Section | Content |
|---|---|
| **AI Suggestion** | Read `sm.current_phase` directly from GUI thread |
| **My Correction** | `queue.Queue(maxsize=64)` for pipeline → GUI snapshots (`put_nowait`, drop on full). `threading.Event` for GUI → pipeline abort. |
| **Architectural Justification** | Direct cross-thread access requires a lock and risks torn reads on Python objects that are not atomically updated. `put_nowait` with drop-on-full ensures pipeline never blocks on a slow GUI render. `Event.set()` is atomic from the setter's side. |

### Entry 10.2 — GUI Update Rate from YAML

| Section | Content |
|---|---|
| **AI Suggestion** | Hardcode `root.after(50, poll)` for 20 Hz refresh |
| **My Correction** | `poll_ms = int(1000 / monitoring.gui_update_rate_hz)` from YAML. `push_every_n_rows = max(1, motor_nominal_rate_hz // gui_update_rate_hz)` also YAML-derived. |
| **Architectural Justification** | Zero hardcoded GUI refresh interval. Changing from 20 Hz to 30 Hz requires no code change. `push_every_n` calculation ensures queue pressure scales with configured rate. |

### Entry 10.3 — Pre-Flight Scan Race Prevention

| Section | Content |
|---|---|
| **AI Suggestion** | Block on file scan in Browse callback; update labels synchronously |
| **My Correction** | Daemon threads for scan. Per-source generation counter discards stale results if user re-browses before scan completes. `self.after(0, ...)` marshals result back to main thread. |
| **Architectural Justification** | Scanning a 1000 Hz binary file blocks GUI for noticeable time. Generation counter prevents a slow scan from a prior Browse overwriting results from a subsequent faster scan — classic TOCTOU race on UI labels. |

---

## Phase 11 — Integration & Stress Tests

### Entry 11.1 — Integration Assertion Strategy

| Section | Content |
|---|---|
| **AI Suggestion** | Assert exact row count matches expected value |
| **My Correction** | Assert output CSV headers match YAML `output.columns`; rows non-empty; `test_phase` column contains only values from YAML phase list |
| **Architectural Justification** | Exact row count is brittle if data files change. Schema + phase presence assertions are durable and directly verify the PDF must-pass criteria: YAML-driven column names, valid phase transitions. |

### Entry 11.2 — O(1) Memory Regression Gate

| Section | Content |
|---|---|
| **AI Suggestion** | Manual profiling during development to verify memory usage |
| **My Correction** | `tests/test_stress.py::TestMemoryBounds`: 500k-row synthetic stream through full pipeline loop; `tracemalloc` delta asserted < 500 KB |
| **Architectural Justification** | O(1) memory is a hard constraint. Development-time profiling cannot substitute for a regression gate — a future refactor accidentally buffering rows would be caught here. 500 KB is conservative: fully buffered 500k rows at ~60 bytes/row = ~30 MB. |

### Entry 11.3 — Multi-Error Stream Recovery

| Section | Content |
|---|---|
| **AI Suggestion** | Test each corruption type in isolation |
| **My Correction** | Inject corrupt bytes at varying positions in a single stream. Assert recovery, continued valid-packet parsing, and correct stat counter increments across multiple concurrent corruption types. |
| **Architectural Justification** | Individual error path tests verify each branch. Multi-error stream tests verify composition: a stream with truncated packet → checksum fail → unknown code tests that resync algorithm composes correctly, not just in isolation. |

---

## Phase 12 — Pre-Submission Architecture Review

### Entry 12.1 — Log Verification Against Specifications

| Section | Content |
|---|---|
| **AI Suggestion (Gemini Mentor)** | Conducted a final architectural cross-check of the `AI_LOG.md` against the PDF "Must-pass" and "Strong signal" criteria prior to code review. |
| **My Correction/Intervention** | Validated that the log explicitly defends against black-box data-science tools (like `pandas.merge_asof`) and hardcoded offsets, focusing heavily on YAML-driven execution and jitter-tolerant nearest-prior joins. |
| **Architectural Justification** | The documentation must serve as a defensive roadmap for the technical interview. By pre-emptively justifying the rejection of typical LLM conveniences (wall-clock timers, dictionary update merges, silent error suppression), the log proves that the resulting architecture is deliberate, safety-oriented, and strictly specification-compliant. |

---

## Cross-Cutting Decision: Jitter vs. Non-Monotonic Timestamps

**Clarified distinction (2026-04-25):** Timing jitter (variable Δt, always monotonic) ≠ non-monotonic timestamps (t_n ≤ t_{n-1}). Provided 4800 Hz hardware data is strictly monotonic. Test assertions updated accordingly.

**Why this matters:** Conflating the two makes tests fragile — either rejecting valid jittered hardware data as "non-monotonic," or silently accepting actual non-monotonic data as "just jitter." Synchronizer correctness proof assumes sorted inputs; a non-monotonic timestamp corrupts the nearest-prior join without the stat counter surfacing it.

---

## Lessons Learned: Using LLMs in Safety-Critical Robotics Code

### 1. LLMs Default to Convenience, Not Safety

LLM suggestions consistently favored convenient patterns (`pandas.merge_asof`, hardcoded offsets, inheritance hierarchies, wall-clock timers) that are correct for typical data-science workflows but wrong for safety-critical systems with strict timing, memory, and error-handling contracts. Every AI suggestion required evaluation against the PDF specification rather than acceptance at face value.

**Takeaway:** Treat LLM output as a first draft from a capable engineer who has not read the spec. Review against requirements before accepting.

### 2. Implicit Assumptions are the Failure Mode

Most AI suggestions that required correction contained an implicit assumption: that timestamps are uniformly spaced, that the binary stream is uncorrupted, that `None` is an acceptable default. In safety-critical code, these assumptions must be made explicit and validated. The Error Matrix (Plan §4) is the checklist for surfacing implicit assumptions.

**Takeaway:** For every AI suggestion, ask "what does this assume about the input?" Reject suggestions that assume clean data in a system that must handle corrupted data.

### 3. The Anti-Pattern List is More Valuable Than the Suggestion List

`Claude_Plan.md §9` Anti-Patterns provided more value than the AI suggestions themselves. Knowing *what to reject* (hardcoded offsets, `pandas.merge_asof` without jitter handling, `time.sleep` for pacing, silent `except: pass`) focused review on the highest-risk decisions. This list was built incrementally from observed LLM tendencies and is a reusable artifact.

**Takeaway:** Maintain an explicit anti-pattern list. It compounds — each rejected suggestion refines the list for the next session.

### 4. Correctness Tests ≠ Safety Correctness

LLM-generated tests tend to verify happy-path contracts. The stress tests (`TestMemoryBounds`, poisoned binary/CSV recovery, adversarial scheduling under `ManualAbortThreading`) required explicit manual authorship. No AI suggestion surfaced the need to test `abs()` semantics for bidirectional torque — that gap was caught in a pre-submission review against PDF criteria.

**Takeaway:** After LLM-assisted implementation, manually review each PDF criterion and write at least one test that would fail if the criterion were violated. Do not rely on AI to identify gaps in its own output.

### 5. YAML as the Contract Boundary

The single most effective guard against LLM anti-patterns was the rule: *"if it is not in YAML, it is a hardcode."* Requiring every structural constant to originate from YAML created a natural review heuristic — scan every LLM-suggested literal string or number and ask "where does this come from?" This surfaces violations immediately before they reach code review.

**Takeaway:** Define the config schema first. Make YAML the authoritative source of truth before writing any code. LLMs generate code that satisfies visible test cases; they do not enforce schema contracts unless the contract is structurally enforced in the architecture.