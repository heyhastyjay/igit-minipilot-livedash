"""Live mini-pilot dashboard.

On the lab computer, double-click START-DASHBOARD.bat in this folder.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("TkAgg")
import tkinter as tk
from tkinter import filedialog, ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from engine import Dashboard, format_inlet_k, load_session, save_session
from plots import draw

SESSION = Path.home() / ".sbu-live-dashboard.json"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SBU mini-pilot")
        self.geometry("1280x820")
        self.dash = Dashboard()
        self.watching = False
        self._editor: tk.Entry | None = None
        self._known_names: tuple[str, ...] = ()
        stored = load_session(SESSION)
        self.dash.load_json(stored)
        self.dash.utc_offset_hours = -5.0
        self.dash.min_temperature_c = None
        self.dash.manual_feed_sccm = None

        self.totalchrom = tk.StringVar(value=stored.get("totalchrom", ""))
        self.cole = tk.StringVar(value=stored.get("cole_parmer", ""))
        self.specview = tk.StringVar(value=stored.get("specview", ""))
        self.status = tk.StringVar(value=self.dash.status)
        if any(text.strip() for text in (self.totalchrom.get(), self.cole.get(), self.specview.get())):
            self.status.set("Reading the saved files.")
        self._updating_sliders = False
        self._redraw_job = None
        self._runtime_span = 0.0
        self._temp_low_bound = 0.0
        self._temp_high_bound = 1.0
        self._follow_run_start = True
        self._follow_run_end = True
        self._follow_temp = True

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(1000, self._tick)
        if os.environ.get("SBU_DASHBOARD_AUTOSTART") == "1" and self.totalchrom.get().strip():
            self.after(400, self._toggle_watch)
        elif any(self._paths()):
            self.after(200, lambda: self._scan(force=True))
        if os.environ.get("SBU_DASHBOARD_TAB") == "calibration":
            self.notebook.select(1)

    def _build(self) -> None:
        paths = ttk.Frame(self, padding=8)
        paths.pack(fill="x")
        self._path_row(paths, 0, "TotalChrom folder", self.totalchrom, directory=True)
        self._path_row(paths, 1, "Cole-Parmer log", self.cole, directory=False)
        self._path_row(paths, 2, "SpecView log", self.specview, directory=False)

        controls = ttk.Frame(self, padding=(8, 0, 8, 4))
        controls.pack(fill="x")
        watch_box = ttk.LabelFrame(controls, padding=8)
        watch_box.pack(side="left", fill="y", padx=(0, 8))
        self.watch_button = ttk.Button(watch_box, text="Start watching", command=self._toggle_watch)
        self.watch_button.pack(padx=4, pady=2)

        constants = ttk.LabelFrame(controls, text="Run constants", padding=8)
        constants.pack(side="right", fill="y")
        ttk.Label(constants, text="inlet meter k value").grid(row=0, column=0, sticky="w")
        self.inlet_k_text = tk.StringVar(value=format_inlet_k(self.dash.inlet_k))
        ttk.Entry(constants, textvariable=self.inlet_k_text, width=10).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Button(constants, text="Save", command=self._save_constants).grid(row=0, column=2, sticky="w")
        ttk.Label(self, textvariable=self.status, padding=(8, 0)).pack(fill="x")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        run = ttk.Frame(self.notebook)
        self.notebook.add(run, text="Run")
        filters = ttk.Frame(run, padding=(4, 4, 4, 0))
        filters.pack(fill="x")
        self.runtime_low, self.runtime_high, self.runtime_readout = self._range_row(
            filters, "Run time (h)", 0.0, 1.0, 0.1, "runtime"
        )
        self.temp_low, self.temp_high, self.temp_readout = self._range_row(
            filters, "Temperature (°C)", 0.0, 1.0, 1.0, "temp"
        )
        self.figure = Figure(figsize=(12.2, 5.8), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.figure, master=run)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        cal = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(cal, text="Calibration")
        ttk.Label(
            cal,
            text="Check Cal gas for each tank injection that belongs on the calibration curve, "
            "then enter its certified percents. A green check marks that file. "
            "Check Reject to leave an injection off the graphs. "
            "Double-click a percent to edit it. Checking a box only marks the file. "
            "Recalculate calibration curve fits the slopes. "
            "Use this calibration curve then recalculates concentrations and redraws the graph. "
            "A checked gas that contains CO2 supplies the feed composition from its CO, CO2, and methane. "
            "Inlet flow comes from the SpecView log.",
            wraplength=1100,
        ).pack(anchor="w", pady=(0, 6))
        self._box, self._check = self._check_images()
        columns = ("reject", "file", "time", "h2", "co", "co2", "ch4")
        table = ttk.Frame(cal)
        table.pack(side="left", fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=columns, show="tree headings", height=18)
        self.tree.heading("#0", text="Cal gas")
        self.tree.column("#0", width=72, minwidth=72, anchor="center", stretch=False)
        headings = {
            "reject": "Reject",
            "file": "Injection",
            "time": "Time (EST)",
            "h2": "H2 %",
            "co": "CO %",
            "co2": "CO2 %",
            "ch4": "CH4 %",
        }
        widths = {"reject": 64, "file": 160, "time": 148, "h2": 64, "co": 64, "co2": 64, "ch4": 64}
        for name in columns:
            self.tree.heading(name, text=headings[name])
            self.tree.column(
                name,
                width=widths[name],
                minwidth=widths[name],
                anchor="center" if name != "file" else "w",
                stretch=name == "file",
            )
        yscroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<Double-1>", self._tree_edit)

        side = ttk.Frame(cal, padding=(8, 0))
        side.pack(side="left", fill="y")
        ttk.Button(side, text="Recalculate calibration curve", command=self._recalculate).pack(fill="x")
        self.cal_hint = tk.StringVar(value="Check the tank injections, then recalculate the calibration curve.")
        ttk.Label(side, textvariable=self.cal_hint, wraplength=320).pack(fill="x", pady=(6, 0))
        self.equations = tk.Text(side, width=42, height=16, wrap="word")
        self.equations.pack(fill="both", expand=True, pady=8)
        self.equations.insert("1.0", "No calibration curve yet.")
        self.equations.configure(state="disabled")
        self.use_button = ttk.Button(side, text="Use this calibration curve", command=self._apply)
        self.use_button.pack(fill="x")
        self.use_button.state(["disabled"])

        method = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(method, text="Method")
        method_scroll = ttk.Scrollbar(method, orient="vertical")
        self.method_text = tk.Text(method, wrap="word", yscrollcommand=method_scroll.set, padx=8, pady=8)
        method_scroll.configure(command=self.method_text.yview)
        self.method_text.pack(side="left", fill="both", expand=True)
        method_scroll.pack(side="left", fill="y")
        self.method_text.configure(state="disabled")

        if self.dash.fits:
            self._show_equations(self.dash.equation_text())
        self._sync_cal_hint(initial=True)
        self._redraw()

    def _check_images(self) -> tuple[tk.PhotoImage, tk.PhotoImage]:
        def box() -> tk.PhotoImage:
            image = tk.PhotoImage(width=18, height=18)
            for x in range(18):
                for y in range(18):
                    edge = x in (0, 1, 16, 17) or y in (0, 1, 16, 17)
                    image.put("#9aa4b2" if edge else "#ffffff", (x, y))
            return image

        empty = box()
        marked = box()
        green = "#16a34a"
        mark = (
            (4, 9), (5, 10), (6, 11), (7, 12), (8, 13),
            (9, 12), (10, 11), (11, 10), (12, 9), (13, 8), (14, 7), (15, 6),
            (4, 8), (5, 9), (6, 10), (7, 11), (8, 12),
            (9, 11), (10, 10), (11, 9), (12, 8), (13, 7), (14, 6),
            (5, 11), (6, 12), (7, 13), (9, 13), (10, 12),
        )
        for point in mark:
            marked.put(green, point)
        return empty, marked

    def _path_row(self, parent, row: int, label: str, variable: tk.StringVar, directory: bool) -> None:
        ttk.Label(parent, text=label, width=24).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=6)
        parent.columnconfigure(1, weight=1)
        ttk.Button(
            parent,
            text="Browse",
            command=lambda: self._browse(variable, directory),
        ).grid(row=row, column=2)

    def _browse(self, variable: tk.StringVar, directory: bool) -> None:
        if directory:
            chosen = filedialog.askdirectory()
        else:
            chosen = filedialog.askopenfilename()
        if chosen:
            variable.set(chosen)

    def _toggle_watch(self) -> None:
        if not self.watching and not any(self._paths()):
            self.status.set("Choose the TotalChrom folder, Cole-Parmer log, and SpecView log.")
            return
        self.watching = not self.watching
        self.watch_button.configure(text="Stop watching" if self.watching else "Start watching")
        if self.watching:
            self._scan(force=True)

    def _paths(self):
        def as_path(text: str) -> Path | None:
            text = text.strip()
            return Path(text) if text else None

        return as_path(self.totalchrom.get()), as_path(self.cole.get()), as_path(self.specview.get())

    def _range_row(self, parent, label: str, low: float, high: float, resolution: float, kind: str):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text=label, width=18).pack(side="left")
        ttk.Label(row, text="from").pack(side="left", padx=(0, 4))
        low_var = tk.DoubleVar(value=low)
        high_var = tk.DoubleVar(value=high)
        low_scale = tk.Scale(
            row,
            from_=low,
            to=high,
            resolution=resolution,
            orient="horizontal",
            showvalue=False,
            variable=low_var,
            command=lambda _value: self._on_slider(kind, "low"),
        )
        low_scale.pack(side="left", fill="x", expand=True)
        ttk.Label(row, text="to").pack(side="left", padx=4)
        high_scale = tk.Scale(
            row,
            from_=low,
            to=high,
            resolution=resolution,
            orient="horizontal",
            showvalue=False,
            variable=high_var,
            command=lambda _value: self._on_slider(kind, "high"),
        )
        high_scale.pack(side="left", fill="x", expand=True, padx=(0, 6))
        readout = ttk.Label(row, width=18)
        readout.pack(side="left")
        if kind == "runtime":
            self.runtime_low_scale = low_scale
            self.runtime_high_scale = high_scale
        else:
            self.temp_low_scale = low_scale
            self.temp_high_scale = high_scale
        return low_var, high_var, readout

    def _on_slider(self, kind: str, which: str) -> None:
        if self._updating_sliders or not hasattr(self, "temp_readout"):
            return
        self._clamp_pair(kind, which)
        if kind == "runtime":
            self._follow_run_start = self.runtime_low.get() <= 0.05
            self._follow_run_end = abs(self.runtime_high.get() - self._runtime_span) <= 0.15
        else:
            self._follow_temp = (
                abs(self.temp_low.get() - self._temp_low_bound) <= 0.6
                and abs(self.temp_high.get() - self._temp_high_bound) <= 0.6
            )
        self._write_readouts()
        if self._redraw_job is not None:
            self.after_cancel(self._redraw_job)
        self._redraw_job = self.after(200, self._slider_redraw)

    def _clamp_pair(self, kind: str, which: str) -> None:
        low_var = self.runtime_low if kind == "runtime" else self.temp_low
        high_var = self.runtime_high if kind == "runtime" else self.temp_high
        if low_var.get() <= high_var.get():
            return
        self._updating_sliders = True
        if which == "low":
            low_var.set(high_var.get())
        else:
            high_var.set(low_var.get())
        self._updating_sliders = False

    def _write_readouts(self) -> None:
        self.runtime_readout.configure(text=f"{self.runtime_low.get():.1f} – {self.runtime_high.get():.1f} h")
        self.temp_readout.configure(text=f"{self.temp_low.get():.0f} – {self.temp_high.get():.0f} °C")

    def _sync_sliders(self) -> None:
        from plots import coverage, temperature_bounds

        span = coverage(self.dash)
        temps = temperature_bounds(self.dash)
        self._updating_sliders = True
        if span is None:
            self.runtime_low_scale.configure(state="disabled")
            self.runtime_high_scale.configure(state="disabled")
        else:
            hours = max((span[1] - span[0]).total_seconds() / 3600.0, 0.1)
            self.runtime_low_scale.configure(state="normal", from_=0, to=hours)
            self.runtime_high_scale.configure(state="normal", from_=0, to=hours)
            if self._follow_run_start:
                self.runtime_low.set(0)
            if self._follow_run_end or self._runtime_span == 0:
                self.runtime_high.set(hours)
            self.runtime_low.set(min(self.runtime_low.get(), hours))
            self.runtime_high.set(max(self.runtime_low.get(), min(self.runtime_high.get(), hours)))
            self._runtime_span = hours
        if temps is None:
            self.temp_low_scale.configure(state="disabled")
            self.temp_high_scale.configure(state="disabled")
        else:
            low = math.floor(temps[0])
            high = math.ceil(temps[1])
            if high <= low:
                high = low + 1
            self.temp_low_scale.configure(state="normal", from_=low, to=high)
            self.temp_high_scale.configure(state="normal", from_=low, to=high)
            if self._follow_temp or self._temp_high_bound <= self._temp_low_bound:
                self.temp_low.set(low)
                self.temp_high.set(high)
            else:
                self.temp_low.set(min(max(self.temp_low.get(), low), high))
                self.temp_high.set(max(self.temp_low.get(), min(self.temp_high.get(), high)))
            self._temp_low_bound = low
            self._temp_high_bound = high
        self._updating_sliders = False
        self._write_readouts()

    def _current_trim(self):
        from plots import Trim, coverage

        span = coverage(self.dash)
        if span is None:
            return None
        limit = not (
            abs(self.temp_low.get() - self._temp_low_bound) <= 0.6
            and abs(self.temp_high.get() - self._temp_high_bound) <= 0.6
        )
        return Trim(
            span[0],
            self.runtime_low.get(),
            self.runtime_high.get(),
            self.temp_low.get(),
            self.temp_high.get(),
            limit,
        )

    def _slider_redraw(self) -> None:
        self._redraw_job = None
        self._redraw(sync=False)

    def _busy(self, message: str) -> None:
        self.status.set(message)
        self.configure(cursor="watch")
        self.update_idletasks()

    def _idle(self) -> None:
        self.configure(cursor="")

    def _tick(self) -> None:
        if self.watching:
            self._scan(force=False)
        self.after(2000, self._tick)

    def _scan(self, force: bool) -> None:
        before = self.dash.status
        if force:
            self._busy("Reading the files…")
        try:
            changed = self.dash.refresh(*self._paths())
        except Exception as exc:
            self._idle()
            self.status.set(str(exc))
            return
        names = tuple(sorted(self.dash.injections))
        if names != self._known_names:
            self._known_names = names
            self._fill_tree()
        if changed or force:
            self._busy("Updating the graph…")
            self._redraw()
        self._idle()
        if changed or force or self.dash.status != before:
            self.status.set(self.dash.status)

    def _redraw(self, sync: bool = True) -> None:
        if sync:
            self._sync_sliders()
        draw(self.figure, self.dash, self._current_trim())
        self.canvas.draw()
        self._show_method()

    def _show_method(self) -> None:
        if not hasattr(self, "method_text"):
            return
        self.method_text.configure(state="normal")
        self.method_text.delete("1.0", "end")
        self.method_text.insert("1.0", self.dash.method_text())
        self.method_text.configure(state="disabled")

    def _fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        ordered = sorted(
            self.dash.injections.values(),
            key=lambda item: (item.acquired_utc is None, item.acquired_utc, item.name),
        )
        for injection in ordered:
            entry = self.dash.calibration[injection.name]
            when = ""
            if injection.acquired_utc is not None:
                local = self.dash.local_time(injection.acquired_utc)
                when = local.strftime("%Y-%m-%d %H:%M:%S")
            self.tree.insert(
                "",
                "end",
                iid=injection.name,
                values=(
                    "☒" if entry.draft_reject else "☐",
                    injection.name,
                    when,
                    entry.h2,
                    entry.co,
                    entry.co2,
                    entry.ch4,
                ),
                image=self._check if entry.draft_use else self._box,
            )

    def _tree_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not row or column not in {"#0", "#1"}:
            return
        self._toggle(row, column)

    def _toggle(self, row: str, column: str) -> None:
        self._close_editor()
        if not row or column not in {"#0", "#1"}:
            return
        entry = self.dash.calibration.get(row)
        if entry is None:
            return
        if column == "#0":
            entry.draft_use = not entry.draft_use
            self.tree.item(row, image=self._check if entry.draft_use else self._box)
        else:
            entry.draft_reject = not entry.draft_reject
            self.tree.set(row, "reject", "☒" if entry.draft_reject else "☐")
        self._sync_cal_hint()

    def _tree_edit(self, event) -> None:
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        keys = {"#4": "h2", "#5": "co", "#6": "co2", "#7": "ch4"}
        if not row or column not in keys:
            return
        box = self.tree.bbox(row, column)
        if not box:
            return
        current = self.tree.set(row, keys[column])
        editor = tk.Entry(self.tree)
        editor.insert(0, current)
        editor.select_range(0, "end")
        editor.place(x=box[0], y=box[1], width=box[2], height=box[3])
        editor.focus_set()
        self._editor = editor

        def commit(_event=None) -> None:
            if self._editor is not editor:
                return
            value = editor.get().strip()
            self.tree.set(row, keys[column], value)
            setattr(self.dash.calibration[row], keys[column], value)
            editor.destroy()
            self._editor = None
            if self.dash.calibration[row].draft_use:
                self._sync_cal_hint()

        editor.bind("<Return>", commit)
        editor.bind("<FocusOut>", commit)

    def _close_editor(self) -> None:
        if self._editor is not None:
            self._editor.event_generate("<FocusOut>")

    def _show_equations(self, text: str) -> None:
        self.equations.configure(state="normal")
        self.equations.delete("1.0", "end")
        self.equations.insert("1.0", text)
        self.equations.configure(state="disabled")

    def _set_use_enabled(self) -> str:
        ready = self.dash.calibration_ready()
        self.use_button.state(["!disabled"] if ready else ["disabled"])
        return ready

    def _sync_cal_hint(self, initial: bool = False) -> None:
        ready = self._set_use_enabled()
        if ready == "curve":
            self.cal_hint.set(
                "Press Use this calibration curve to recalculate concentrations and redraw the graph."
            )
        elif ready == "graph":
            self.cal_hint.set("Press Use this calibration curve to update the graph.")
        elif self.dash.curve_signature(draft=True) != self.dash.curve_signature(draft=False):
            self.cal_hint.set("Checked files differ from the curve on the graph. Recalculate the calibration curve.")
        elif self.dash.fits:
            self.cal_hint.set("The graph is using the saved calibration curve.")
        elif not initial:
            self.cal_hint.set("Check the tank injections, then recalculate the calibration curve.")

    def _recalculate(self) -> None:
        self._close_editor()
        try:
            text = self.dash.preview_calibration()
        except ValueError as exc:
            self._set_use_enabled()
            self.status.set(str(exc))
            self.cal_hint.set(str(exc))
            return
        self._show_equations(text)
        self._sync_cal_hint()
        counted = sum(1 for entry in self.dash.calibration.values() if entry.draft_use)
        noun = "injection" if counted == 1 else "injections"
        self.status.set(f"Calibration curve fit from {counted} {noun}. The graph is unchanged.")

    def _apply(self) -> None:
        self._close_editor()
        try:
            text = self.dash.apply_calibration()
        except ValueError as exc:
            self.status.set(str(exc))
            self.cal_hint.set(str(exc))
            return
        self._show_equations(text)
        self._persist()
        self._busy("Updating the graph…")
        try:
            self._redraw()
            counted = sum(1 for point in self.dash.conversions() if point.conversion_pct is not None)
        finally:
            self._idle()
        self.status.set(f"Using this calibration curve. {counted} injections have a carbon conversion.")
        self.cal_hint.set("Concentrations updated and the graph redrawn.")
        self._set_use_enabled()

    def _save_constants(self) -> None:
        text = self.inlet_k_text.get().strip()
        try:
            k = round(float(text), 3)
        except ValueError:
            self.status.set("inlet meter k value needs to be a number.")
            return
        if k <= 0:
            self.status.set("inlet meter k value needs to be greater than zero.")
            return
        self.dash.inlet_k = k
        self.inlet_k_text.set(format_inlet_k(k))
        self._persist()
        self._busy("Updating the graph…")
        try:
            self._redraw()
            counted = sum(1 for point in self.dash.conversions() if point.conversion_pct is not None)
        finally:
            self._idle()
        self.status.set(f"Saved. Inlet flow is the SpecView reading times {format_inlet_k(k)}. {counted} injections have a carbon conversion.")

    def _persist(self) -> None:
        payload = self.dash.to_json()
        payload.update(
            {
                "totalchrom": self.totalchrom.get(),
                "cole_parmer": self.cole.get(),
                "specview": self.specview.get(),
            }
        )
        save_session(SESSION, payload)

    def _close(self) -> None:
        try:
            self._persist()
        finally:
            self.destroy()


def main() -> None:
    if sys.version_info < (3, 9):
        raise SystemExit("Python 3.9 or newer is required.")
    App().mainloop()


if __name__ == "__main__":
    main()
