"""Motor Characterization Bench — Live Monitoring GUI.

Architecture:
  Main thread  : Tk mainloop + _poll (root.after loop)
  Worker thread: run_pipeline(…, status_queue=q, abort_event=e)

Cross-thread communication:
  pipeline → GUI  : queue.Queue  (bounded, put_nowait, drop on full)
  GUI → pipeline  : threading.Event (abort_event.set())

Teardown on window-close:
  WM_DELETE_WINDOW → abort_event.set() → worker.join(timeout=2s) → destroy()
  Logger.__exit__ is guaranteed to flush CSV before join returns.
"""
from __future__ import annotations

import argparse
import collections
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional

import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from config.loader import ConfigurationError, load_yaml_config
from automation.main import run_pipeline

_log = logging.getLogger(__name__)


class MonitoringApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Motor Characterization Bench")
        self.resizable(True, True)

        # Thread primitives — created fresh per run
        self._status_queue: Optional[queue.Queue] = None
        self._abort_event: Optional[threading.Event] = None
        self._worker: Optional[threading.Thread] = None
        self._poll_id: Optional[str] = None
        self._poll_ms: int = 50  # overridden from YAML at each Start

        # Path StringVars
        self._config_path = tk.StringVar()
        self._motor_path = tk.StringVar()
        self._sensor_path = tk.StringVar()
        self._psu_path = tk.StringVar()
        self._output_path = tk.StringVar()
        self._motor_format = tk.StringVar(value="csv")
        self._protocol_path = tk.StringVar()

        # Speed control
        self._speed_var = tk.StringVar(value="Max")

        # Telemetry StringVars
        self._phase_var = tk.StringVar(value="—")
        self._rows_var = tk.StringVar(value="—")
        self._t_var = tk.StringVar(value="—")
        self._cmd_i_var = tk.StringVar(value="—")
        self._cmd_v_var = tk.StringVar(value="—")
        self._torque_var = tk.StringVar(value="—")
        self._psu_v_var = tk.StringVar(value="—")
        self._psu_i_var = tk.StringVar(value="—")
        self._abort_reason_var = tk.StringVar(value="")
        self._status_var = tk.StringVar(value="Idle")

        # Summary StringVars
        self._sum_motor_packets_var = tk.StringVar(value="—")
        self._sum_motor_errors_var = tk.StringVar(value="—")
        self._sum_sensor_rows_var = tk.StringVar(value="—")
        self._sum_sensor_malformed_var = tk.StringVar(value="—")
        self._sum_psu_rows_var = tk.StringVar(value="—")
        self._sum_psu_malformed_var = tk.StringVar(value="—")
        self._sum_phase_dur_var = tk.StringVar(value="—")
        self._sum_peak_torque_var = tk.StringVar(value="—")
        self._sum_peak_current_var = tk.StringVar(value="—")
        self._sum_output_var = tk.StringVar(value="—")
        self._sum_efficiency_var    = tk.StringVar(value="—")
        self._sum_motor_dropped_var = tk.StringVar(value="—")
        self._sum_sensor_dropped_var = tk.StringVar(value="—")
        self._sum_psu_dropped_var   = tk.StringVar(value="—")

        # Plot deques — O(1) memory, 200-sample rolling window
        self._dq_t:        collections.deque = collections.deque(maxlen=200)
        self._dq_motor_i:  collections.deque = collections.deque(maxlen=200)
        self._dq_cmd_i:    collections.deque = collections.deque(maxlen=200)
        self._dq_torque:   collections.deque = collections.deque(maxlen=200)
        self._dq_psu_v:    collections.deque = collections.deque(maxlen=200)
        self._dq_velocity: collections.deque = collections.deque(maxlen=200)

        # Plot widget refs — populated by _build_ui()
        self._canvas: Optional[FigureCanvasTkAgg] = None
        self._line_motor_i  = None
        self._line_cmd_i    = None
        self._line_torque   = None
        self._line_psu_v    = None
        self._line_velocity = None
        self._phase_text    = None
        self._plot_axes: List = []

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Abort.TButton", foreground="red")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # ── Scrollable canvas shell ───────────────────────────────────
        canvas = tk.Canvas(self, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")

        vscroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        vscroll.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=vscroll.set)

        # All widgets live in this inner frame
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.columnconfigure(0, weight=1)
        inner.rowconfigure(4, weight=1)

        def _on_inner_configure(event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            # Cap window height to screen height
            max_h = self.winfo_screenheight() - 80
            canvas.configure(height=min(event.height, max_h))

        def _on_canvas_configure(event: tk.Event) -> None:
            canvas.itemconfig(inner_id, width=event.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel scrolling
        def _on_mousewheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ── Input Frame ───────────────────────────────────────────────
        inp = ttk.LabelFrame(inner, text="Inputs", padding=8)
        inp.grid(row=0, column=0, sticky="ew", padx=8, pady=4)
        inp.columnconfigure(1, weight=1)

        file_rows = [
            ("Config YAML:",  self._config_path,  "config"),
            ("Motor CSV/BIN:",   self._motor_path,   "motor"),
            ("Sensor CSV:",   self._sensor_path,  "sensor"),
            ("PSU CSV:",      self._psu_path,     "psu"),
        ]
        for r, (lbl, var, key) in enumerate(file_rows):
            ttk.Label(inp, text=lbl).grid(row=r, column=0, sticky="w")
            ttk.Entry(inp, textvariable=var, state="readonly", width=50).grid(
                row=r, column=1, sticky="ew", padx=4
            )
            ttk.Button(
                inp, text="Browse…", command=lambda k=key: self._on_browse(k)
            ).grid(row=r, column=2)

        # Format selector
        r = len(file_rows)
        ttk.Label(inp, text="Motor format:").grid(row=r, column=0, sticky="w")
        self._fmt_combo = ttk.Combobox(
            inp, textvariable=self._motor_format,
            values=["csv", "binary"], state="readonly", width=10,
        )
        self._fmt_combo.grid(row=r, column=1, sticky="w", padx=4)
        self._fmt_combo.bind(
            "<<ComboboxSelected>>",
            lambda _: (self._update_protocol_state(), self._check_ready()),
        )

        # Protocol YAML (enabled only when format=binary)
        r += 1
        ttk.Label(inp, text="Protocol YAML:").grid(row=r, column=0, sticky="w")
        self._proto_entry = ttk.Entry(
            inp, textvariable=self._protocol_path, state="disabled", width=50
        )
        self._proto_entry.grid(row=r, column=1, sticky="ew", padx=4)
        self._proto_btn = ttk.Button(
            inp, text="Browse…", command=lambda: self._on_browse("protocol"), state="disabled"
        )
        self._proto_btn.grid(row=r, column=2)

        # Output path
        r += 1
        ttk.Label(inp, text="Output CSV:").grid(row=r, column=0, sticky="w")
        ttk.Entry(inp, textvariable=self._output_path, state="readonly", width=50).grid(
            row=r, column=1, sticky="ew", padx=4
        )
        ttk.Button(inp, text="Save As…", command=self._on_browse_output).grid(row=r, column=2)

        # ── Control Frame ─────────────────────────────────────────────
        ctrl = ttk.Frame(inner, padding=8)
        ctrl.grid(row=1, column=0, sticky="ew", padx=8)

        self._start_btn = ttk.Button(
            ctrl, text="Start Test", command=self._on_start, state="disabled"
        )
        self._start_btn.grid(row=0, column=0, padx=4)

        self._abort_btn = ttk.Button(
            ctrl, text="Emergency Abort",
            style="Abort.TButton", command=self._on_abort, state="disabled",
        )
        self._abort_btn.grid(row=0, column=1, padx=4)

        ttk.Label(ctrl, text="Speed:").grid(row=0, column=2, padx=(16, 2))
        self._speed_combo = ttk.Combobox(
            ctrl, textvariable=self._speed_var,
            values=["1x", "5x", "10x", "Max"], state="readonly", width=6,
        )
        self._speed_combo.grid(row=0, column=3, padx=4)

        ttk.Label(ctrl, textvariable=self._status_var, width=24).grid(row=0, column=4, padx=8)

        # ── Telemetry Frame ───────────────────────────────────────────
        tele = ttk.LabelFrame(inner, text="Telemetry", padding=8)
        tele.grid(row=2, column=0, sticky="ew", padx=8, pady=4)

        tele_rows = [
            ("Phase:",           self._phase_var),
            ("Rows processed:",  self._rows_var),
            ("Timestamp (s):",   self._t_var),
            ("Cmd current (A):", self._cmd_i_var),
            ("Cmd voltage (V):", self._cmd_v_var),
            ("Torque (Nm):",     self._torque_var),
            ("PSU voltage (V):", self._psu_v_var),
            ("PSU current (A):", self._psu_i_var),
            ("Abort reason:",    self._abort_reason_var),
        ]
        for r, (lbl, var) in enumerate(tele_rows):
            ttk.Label(tele, text=lbl, anchor="e", width=18).grid(row=r, column=0, sticky="e")
            ttk.Label(tele, textvariable=var, anchor="w", width=24).grid(
                row=r, column=1, sticky="w"
            )

        # ── Run Summary Frame ─────────────────────────────────────────
        summ = ttk.LabelFrame(inner, text="Run Summary", padding=8)
        summ.grid(row=3, column=0, sticky="ew", padx=8, pady=4)

        summ_rows = [
            ("Motor packets/rows:",           self._sum_motor_packets_var),
            ("Motor errors:",                 self._sum_motor_errors_var),
            ("Motor dropped (chk/trunc/unk):", self._sum_motor_dropped_var),
            ("Sensor rows:",                  self._sum_sensor_rows_var),
            ("Sensor malformed:",             self._sum_sensor_malformed_var),
            ("Sensor timestamp gaps:",        self._sum_sensor_dropped_var),
            ("PSU rows:",                     self._sum_psu_rows_var),
            ("PSU malformed:",                self._sum_psu_malformed_var),
            ("PSU timestamp gaps:",           self._sum_psu_dropped_var),
            ("Efficiency mean / peak:",       self._sum_efficiency_var),
            ("Phase durations:",              self._sum_phase_dur_var),
            ("Peak torque (Nm):",             self._sum_peak_torque_var),
            ("Peak current (A):",             self._sum_peak_current_var),
            ("Output file:",                  self._sum_output_var),
        ]
        for r, (lbl, var) in enumerate(summ_rows):
            ttk.Label(summ, text=lbl, anchor="e", width=20).grid(row=r, column=0, sticky="e")
            ttk.Label(summ, textvariable=var, anchor="w", width=52).grid(
                row=r, column=1, sticky="w"
            )

        # ── Plot Frame ────────────────────────────────────────────────
        plot_frm = ttk.LabelFrame(inner, text="Live Telemetry", padding=4)
        plot_frm.grid(row=4, column=0, sticky="nsew", padx=8, pady=4)

        fig = Figure(figsize=(8, 5.5), dpi=90)
        fig.set_layout_engine("tight")

        ax0 = fig.add_subplot(4, 1, 1)
        ax1 = fig.add_subplot(4, 1, 2)
        ax2 = fig.add_subplot(4, 1, 3)
        ax3 = fig.add_subplot(4, 1, 4)

        self._line_motor_i, = ax0.plot([], [], label="Meas I", color="steelblue", linewidth=1)
        self._line_cmd_i,   = ax0.plot([], [], label="Cmd I",  color="orange",
                                        linestyle="--", linewidth=1)
        self._phase_text = ax0.text(
            0.02, 0.95, "", transform=ax0.transAxes, va="top", fontsize=7
        )
        ax0.set_ylabel("Current (A)", fontsize=8)
        ax0.legend(fontsize=7, loc="upper right")
        ax0.tick_params(labelsize=7)

        self._line_torque, = ax1.plot([], [], color="green", linewidth=1)
        ax1.set_ylabel("Torque (Nm)", fontsize=8)
        ax1.tick_params(labelsize=7)

        self._line_psu_v, = ax2.plot([], [], color="crimson", linewidth=1)
        ax2.set_ylabel("PSU V", fontsize=8)
        ax2.tick_params(labelsize=7)

        self._line_velocity, = ax3.plot([], [], color="purple", linewidth=1)
        ax3.set_ylabel("Velocity (rad/s)", fontsize=8)
        ax3.set_xlabel("Time (s)", fontsize=8)
        ax3.tick_params(labelsize=7)

        self._plot_axes = [ax0, ax1, ax2, ax3]

        self._canvas = FigureCanvasTkAgg(fig, master=plot_frm)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)
        self._canvas.draw()

    # ------------------------------------------------------------------
    # Browse callbacks
    # ------------------------------------------------------------------

    def _on_browse(self, key: str) -> None:
        path = filedialog.askopenfilename(
            title=f"Select {key} file",
            filetypes=[
                ("All files", "*.*"),
                ("YAML files", "*.yaml *.yml"),
                ("CSV files", "*.csv"),
                ("Binary files", "*.bin"),
            ],
        )
        if not path:
            return
        var_map: Dict[str, tk.StringVar] = {
            "config":   self._config_path,
            "motor":    self._motor_path,
            "sensor":   self._sensor_path,
            "psu":      self._psu_path,
            "protocol": self._protocol_path,
        }
        var_map[key].set(path)
        if key == "motor":
            if path.lower().endswith(".bin"):
                self._motor_format.set("binary")
            elif path.lower().endswith(".csv"):
                self._motor_format.set("csv")
            self._update_protocol_state()
        self._check_ready()

    def _on_browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save output CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self._output_path.set(path)
            self._check_ready()

    def _check_ready(self) -> None:
        """Enable Start only when all required file paths are populated.

        Protocol YAML is required only when motor format is binary.
        """
        required = [
            self._config_path.get().strip(),
            self._motor_path.get().strip(),
            self._sensor_path.get().strip(),
            self._psu_path.get().strip(),
            self._output_path.get().strip(),
        ]
        if self._motor_format.get() == "binary":
            required.append(self._protocol_path.get().strip())
        self._start_btn.config(state="normal" if all(required) else "disabled")

    def _update_protocol_state(self) -> None:
        if self._motor_format.get() == "binary":
            self._proto_entry.config(state="readonly")
            self._proto_btn.config(state="normal")
        else:
            self._proto_entry.config(state="disabled")
            self._proto_btn.config(state="disabled")

    # ------------------------------------------------------------------
    # Start / Abort
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_speed_multiplier(s: str) -> float:
        return {"1x": 1.0, "5x": 5.0, "10x": 10.0, "Max": 0.0}.get(s, 0.0)

    def _on_start(self) -> None:
        config_p  = self._config_path.get().strip()
        motor_p   = self._motor_path.get().strip()
        sensor_p  = self._sensor_path.get().strip()
        psu_p     = self._psu_path.get().strip()
        output_p  = self._output_path.get().strip()
        motor_fmt = self._motor_format.get()
        proto_p   = self._protocol_path.get().strip()

        missing = [
            lbl for lbl, val in [
                ("Config YAML", config_p), ("Motor file", motor_p),
                ("Sensor CSV",  sensor_p), ("PSU CSV",    psu_p),
                ("Output CSV",  output_p),
            ] if not val
        ]
        if missing:
            messagebox.showerror(
                "Missing inputs", "Required fields empty:\n" + "\n".join(missing)
            )
            return

        for lbl, val in [
            ("Config YAML", config_p), ("Motor file", motor_p),
            ("Sensor CSV",  sensor_p), ("PSU CSV",    psu_p),
        ]:
            if not Path(val).exists():
                messagebox.showerror("File not found", f"{lbl} not found:\n{val}")
                return

        if motor_fmt == "binary":
            if not proto_p:
                messagebox.showerror(
                    "Missing input", "Protocol YAML required for binary format."
                )
                return
            if not Path(proto_p).exists():
                messagebox.showerror("File not found", f"Protocol YAML not found:\n{proto_p}")
                return

        try:
            test_cfg = load_yaml_config(config_p)
        except ConfigurationError as exc:
            messagebox.showerror("Config error", f"Failed to load test config:\n{exc}")
            return

        motor_proto: Optional[Dict[str, Any]] = None
        if motor_fmt == "binary":
            try:
                motor_proto = load_yaml_config(proto_p)
            except ConfigurationError as exc:
                messagebox.showerror("Config error", f"Failed to load motor protocol:\n{exc}")
                return

        Path(output_p).parent.mkdir(parents=True, exist_ok=True)

        # Poll rate from YAML — zero hardcoding
        try:
            gui_rate = test_cfg["monitoring"]["gui_update_rate_hz"]
            self._poll_ms = max(1, int(1000 / gui_rate))
        except (KeyError, TypeError, ZeroDivisionError):
            self._poll_ms = 50

        speed_multiplier = self._parse_speed_multiplier(self._speed_var.get())

        args = argparse.Namespace(
            motor=motor_p,
            motor_format=motor_fmt,
            motor_protocol=proto_p if motor_fmt == "binary" else None,
            sensor=sensor_p,
            psu=psu_p,
            output=output_p,
        )

        self._status_queue = queue.Queue(maxsize=64)
        self._abort_event  = threading.Event()

        self._status_var.set("Running")
        self._start_btn.config(state="disabled")
        self._abort_btn.config(state="normal")

        for var in (
            self._phase_var, self._rows_var, self._t_var, self._cmd_i_var,
            self._cmd_v_var, self._torque_var, self._psu_v_var, self._psu_i_var,
        ):
            var.set("—")
        self._abort_reason_var.set("")

        # Clear plot deques and reset line data for fresh run
        for dq in (self._dq_t, self._dq_motor_i, self._dq_cmd_i,
                   self._dq_torque, self._dq_psu_v, self._dq_velocity):
            dq.clear()
        if self._canvas is not None:
            self._line_motor_i.set_data([], [])
            self._line_cmd_i.set_data([], [])
            self._line_torque.set_data([], [])
            self._line_psu_v.set_data([], [])
            self._line_velocity.set_data([], [])
            self._phase_text.set_text("")
            for ax in self._plot_axes:
                ax.relim()
                ax.autoscale_view()
            self._canvas.draw_idle()

        self._worker = threading.Thread(
            target=self._worker_target,
            args=(args, test_cfg, motor_proto, speed_multiplier),
            daemon=True,
            name="pipeline-worker",
        )
        self._worker.start()
        self._poll_id = self.after(self._poll_ms, self._poll)

    def _on_abort(self) -> None:
        if self._abort_event is not None:
            self._abort_event.set()

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _worker_target(
        self,
        args: argparse.Namespace,
        test_cfg: Dict[str, Any],
        motor_proto: Optional[Dict[str, Any]],
        speed_multiplier: float = 0.0,
    ) -> None:
        try:
            run_pipeline(
                args, test_cfg, motor_proto,
                status_queue=self._status_queue,
                abort_event=self._abort_event,
                speed_multiplier=speed_multiplier,
            )
        except Exception:
            _log.exception("Pipeline failed in worker thread")
            try:
                self._status_queue.put_nowait({  # type: ignore[union-attr]
                    "_done": True,
                    "stats": {"abort_reason": "pipeline_error"},
                    "rows": 0,
                })
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # Poll loop (main thread only)
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        if self._status_queue is None:
            return
        last: Optional[Dict[str, Any]] = None
        while True:
            try:
                last = self._status_queue.get_nowait()
            except queue.Empty:
                break
        if last is not None:
            if last.get("_done"):
                self._on_pipeline_done(last)
                self._redraw_plot()
                return
            self._apply_snapshot(last)
        self._redraw_plot()
        self._poll_id = self.after(self._poll_ms, self._poll)

    def _apply_snapshot(self, snap: Dict[str, Any]) -> None:
        self._phase_var.set(snap.get("test_phase") or "—")
        self._rows_var.set(str(snap.get("rows", "—")))
        t = snap.get("timestamp_s")
        self._t_var.set(f"{t:.3f}" if t is not None else "—")
        cmd_i = snap.get("commanded_current_a")
        self._cmd_i_var.set(f"{cmd_i:.3f}" if cmd_i is not None else "—")
        cmd_v = snap.get("commanded_voltage_v")
        self._cmd_v_var.set(f"{cmd_v:.3f}" if cmd_v is not None else "—")
        torque = snap.get("torque_nm")
        self._torque_var.set(f"{torque:.3f}" if torque is not None else "—")
        psu_v = snap.get("psu_voltage_v")
        self._psu_v_var.set(f"{psu_v:.3f}" if psu_v is not None else "—")
        psu_i = snap.get("psu_current_a")
        self._psu_i_var.set(f"{psu_i:.3f}" if psu_i is not None else "—")
        self._abort_reason_var.set(snap.get("abort_reason") or "")

        # Append to rolling deques only when timestamp is available
        if t is not None:
            self._dq_t.append(t)
            self._dq_motor_i.append(snap.get("motor_current_a"))
            self._dq_cmd_i.append(cmd_i)
            self._dq_torque.append(torque)
            self._dq_psu_v.append(psu_v)
            self._dq_velocity.append(snap.get("velocity_rad_s"))

    # ------------------------------------------------------------------
    # Live plot
    # ------------------------------------------------------------------

    def _redraw_plot(self) -> None:
        if self._canvas is None:
            return

        def _safe_pairs(t_list, val_list):
            pairs = [(ti, vi) for ti, vi in zip(t_list, val_list) if vi is not None]
            if not pairs:
                return [], []
            ts, vs = zip(*pairs)
            return list(ts), list(vs)

        # Re-enable autoscale so any set_ylim from the flat-line guard in the
        # previous redraw doesn't persist and block the next autoscale_view() call.
        for ax in self._plot_axes:
            ax.set_autoscale_on(True)

        t = list(self._dq_t)

        ts, vs = _safe_pairs(t, list(self._dq_motor_i))
        if ts:
            self._line_motor_i.set_data(ts, vs)

        ts, vs = _safe_pairs(t, list(self._dq_cmd_i))
        if ts:
            self._line_cmd_i.set_data(ts, vs)

        self._phase_text.set_text(self._phase_var.get())

        ts, vs = _safe_pairs(t, list(self._dq_torque))
        if ts:
            self._line_torque.set_data(ts, vs)

        ts, vs = _safe_pairs(t, list(self._dq_psu_v))
        if ts:
            self._line_psu_v.set_data(ts, vs)
        psu_vs = vs

        ts, vs = _safe_pairs(t, list(self._dq_velocity))
        if ts:
            self._line_velocity.set_data(ts, vs)

        for ax in self._plot_axes:
            ax.relim()
            ax.autoscale_view()

        # Flat PSU line (all values identical) → autoscale gives zero range → force padding
        if psu_vs:
            min_val = min(psu_vs)
            max_val = max(psu_vs)
            if max_val - min_val == 0:
                self._plot_axes[2].set_ylim(min_val - 5, max_val + 5)

        # draw_idle lets Tkinter schedule the draw without blocking the poll thread
        self._canvas.draw_idle()
        self._canvas.get_tk_widget().update_idletasks()

    def _on_pipeline_done(self, sentinel: Dict[str, Any]) -> None:
        stats = sentinel.get("stats") or {}
        motor_stats = sentinel.get("motor_stats") or {}
        sensor_stats = sentinel.get("sensor_stats") or {}
        psu_stats = sentinel.get("psu_stats") or {}

        abort_reason = stats.get("abort_reason")
        self._rows_var.set(str(sentinel.get("rows", "—")))
        self._abort_reason_var.set(abort_reason or "")
        self._status_var.set("Aborted" if abort_reason else "Complete")
        self._start_btn.config(state="normal")
        self._abort_btn.config(state="disabled")
        self._worker = None

        # Motor — binary uses total_packets/checksum_errors; CSV uses total_rows/malformed_rows
        if "total_packets" in motor_stats:
            self._sum_motor_packets_var.set(str(motor_stats.get("total_packets", "—")))
            self._sum_motor_errors_var.set(str(motor_stats.get("checksum_errors", "—")))
            motor_dropped = (
                (motor_stats.get("checksum_errors") or 0)
                + (motor_stats.get("truncations") or 0)
                + (motor_stats.get("unknown_codes") or 0)
            )
            self._sum_motor_dropped_var.set(str(motor_dropped))
        elif motor_stats:
            self._sum_motor_packets_var.set(str(motor_stats.get("total_rows", "—")))
            self._sum_motor_errors_var.set(str(motor_stats.get("malformed_rows", "—")))
            self._sum_motor_dropped_var.set(str(motor_stats.get("malformed_rows", "—")))

        self._sum_sensor_rows_var.set(str(sensor_stats.get("total_rows", "—")))
        self._sum_sensor_malformed_var.set(str(sensor_stats.get("malformed_rows", "—")))
        self._sum_sensor_dropped_var.set(str(sensor_stats.get("timestamp_gaps", "—")))
        self._sum_psu_rows_var.set(str(psu_stats.get("total_rows", "—")))
        self._sum_psu_malformed_var.set(str(psu_stats.get("malformed_rows", "—")))
        self._sum_psu_dropped_var.set(str(psu_stats.get("timestamp_gaps", "—")))

        eff_mean = sentinel.get("efficiency_mean")
        eff_peak = sentinel.get("efficiency_peak")
        if eff_mean is not None or eff_peak is not None:
            mean_s = f"{eff_mean:.4f}" if eff_mean is not None else "—"
            peak_s = f"{eff_peak:.4f}" if eff_peak is not None else "—"
            self._sum_efficiency_var.set(f"{mean_s} / {peak_s}")
        else:
            self._sum_efficiency_var.set("—")

        phase_dur = stats.get("phase_durations_s") or {}
        dur_str = ", ".join(
            f"{p}: {d:.2f}s" for p, d in phase_dur.items() if d > 0.0
        ) or "—"
        self._sum_phase_dur_var.set(dur_str)

        peak_torque = stats.get("peak_torque_nm")
        self._sum_peak_torque_var.set(
            f"{peak_torque:.2f}" if peak_torque is not None else "—"
        )
        peak_current = stats.get("peak_current_a")
        self._sum_peak_current_var.set(
            f"{peak_current:.2f}" if peak_current is not None else "—"
        )

        self._sum_output_var.set(self._output_path.get() or "—")

    # ------------------------------------------------------------------
    # Graceful teardown
    # ------------------------------------------------------------------

    def _on_closing(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self._abort_event.set()  # type: ignore[union-attr]
            self._status_var.set("Closing — flushing output…")
            self.update_idletasks()
            self._worker.join(timeout=2.0)
            if self._worker.is_alive():
                _log.warning("Worker thread did not exit within 2 s; forcing window close")
        self.destroy()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    app = MonitoringApp()
    app.mainloop()


if __name__ == "__main__":
    main()
