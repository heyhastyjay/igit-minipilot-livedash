"""Watch-folder state, calibration fit, and carbon conversion."""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from parsers import (
    FlowSample,
    Injection,
    SpecviewSeries,
    parse_flow_log,
    parse_rst,
    parse_specview_csv,
    parse_specview_log,
)

GASES = ("Hydrogen", "Carbon Monoxide", "Carbon Dioxide", "Methane")
# SpecView is every 10 s, so 30 s still reaches the nearest sample if one
# record is missing, without taking a temperature or a flow from the next step.
MATCH_WINDOW_S = 30.0
# Cole-Parmer is 1 Hz. ±30 s is enough to quiet the second-to-second noise
# and short enough to stay on one outlet reading.
OUTLET_WINDOW_S = 30.0
OUTLET_MIN_SAMPLES = 10
GAS_KEY = {
    "Hydrogen": "h2",
    "Carbon Monoxide": "co",
    "Carbon Dioxide": "co2",
    "Methane": "ch4",
}


def format_inlet_k(value: float) -> str:
    """Three decimal places, so a typed 0.970 is not shown as 0.97."""
    return f"{float(value):.3f}"


@dataclass
class CalEntry:
    injection: str
    use: bool = False
    feed: bool = False
    skip: bool = False
    reject: bool = False
    h2: str = ""
    co: str = ""
    co2: str = ""
    ch4: str = ""
    draft_use: bool = False
    draft_reject: bool = False

    def certified(self) -> dict[str, float]:
        raw = {"h2": self.h2, "co": self.co, "co2": self.co2, "ch4": self.ch4}
        return {key: float(value) for key, value in raw.items()}


@dataclass
class Fit:
    slope: float
    intercept: float
    n_points: int


@dataclass
class ConversionPoint:
    name: str
    when_utc: datetime | None
    temperature_c: float | None
    feed_sccm: float | None
    outlet_slpm: float | None
    conversion_pct: float | None
    detail: str


def ols(xs: list[float], ys: list[float]) -> Fit:
    x = np.array(xs, dtype=float)
    y = np.array(ys, dtype=float)
    if len(x) >= 2 and float(np.ptp(x)) > 0:
        slope, intercept = np.polyfit(x, y, 1)
        return Fit(float(slope), float(intercept), len(x))
    # Replicates of one standard still define a line through the origin.
    if len(x) >= 1 and float(np.ptp(x)) == 0 and float(x[0]) != 0:
        return Fit(float(y.mean() / x[0]), 0.0, len(x))
    raise ValueError("needs two different certified percents, or one non-zero percent")


def mol_percent(area: float, fit: Fit) -> float:
    return (area - fit.intercept) / fit.slope


class Dashboard:
    def __init__(self) -> None:
        self.injections: dict[str, Injection] = {}
        self.flow: list[FlowSample] = []
        self.specview: SpecviewSeries | None = None
        self.calibration: dict[str, CalEntry] = {}
        self.fits: dict[str, Fit] = {}
        self.pending_fits: dict[str, Fit] | None = None
        self.preview_signature: tuple | None = None
        self.utc_offset_hours = -5.0
        self.min_temperature_c: float | None = None
        self.manual_feed_sccm: float | None = None
        self.inlet_k = 1.0
        self.status = "Choose the TotalChrom folder, Cole-Parmer log, and SpecView log, then start watching."
        self._seen: dict[str, tuple[int, int]] = {}
        self._series_rev = 0

    def to_json(self) -> dict:
        return {
            "utc_offset_hours": self.utc_offset_hours,
            "min_temperature_c": self.min_temperature_c,
            "manual_feed_sccm": self.manual_feed_sccm,
            "inlet_k": self.inlet_k,
            "calibration": [
                {
                    "injection": entry.injection,
                    "use": entry.use,
                    "feed": entry.feed,
                    "skip": entry.skip,
                    "reject": entry.reject,
                    "h2": entry.h2,
                    "co": entry.co,
                    "co2": entry.co2,
                    "ch4": entry.ch4,
                    "draft_use": entry.draft_use,
                    "draft_reject": entry.draft_reject,
                }
                for entry in self.calibration.values()
            ],
            "fits": {
                name: {"slope": fit.slope, "intercept": fit.intercept, "n_points": fit.n_points}
                for name, fit in self.fits.items()
            },
        }

    def load_json(self, payload: dict) -> None:
        if "utc_offset_hours" in payload:
            self.utc_offset_hours = float(payload["utc_offset_hours"])
        if payload.get("min_temperature_c") is not None:
            self.min_temperature_c = float(payload["min_temperature_c"])
        if payload.get("manual_feed_sccm") is not None:
            self.manual_feed_sccm = float(payload["manual_feed_sccm"])
        if payload.get("inlet_k") is not None:
            self.inlet_k = float(payload["inlet_k"])
        self.calibration = {}
        for row in payload.get("calibration", []):
            entry = CalEntry(
                injection=row["injection"],
                use=bool(row.get("use")),
                feed=bool(row.get("feed")),
                skip=bool(row.get("skip")),
                reject=bool(row.get("reject")),
                h2=str(row.get("h2", "")),
                co=str(row.get("co", "")),
                co2=str(row.get("co2", "")),
                ch4=str(row.get("ch4", "")),
                draft_use=bool(row["draft_use"]) if "draft_use" in row else bool(row.get("use")),
                draft_reject=bool(row["draft_reject"]) if "draft_reject" in row else bool(row.get("reject")),
            )
            self.calibration[entry.injection] = entry
        self.pending_fits = None
        self.preview_signature = None
        self.fits = {}
        for name, row in payload.get("fits", {}).items():
            self.fits[name] = Fit(float(row["slope"]), float(row["intercept"]), int(row["n_points"]))

    def remember(self, injection: Injection) -> None:
        self.injections[injection.name] = injection
        if injection.name not in self.calibration:
            self.calibration[injection.name] = CalEntry(injection.name)

    def _changed(self, path: Path) -> bool:
        stat = path.stat()
        key = str(path.resolve())
        signature = (stat.st_size, stat.st_mtime_ns)
        if self._seen.get(key) == signature:
            return False
        self._seen[key] = signature
        return True

    def refresh(
        self,
        totalchrom: Path | None,
        cole_parmer: Path | None,
        specview: Path | None,
    ) -> bool:
        changed = False
        problems: list[str] = []
        if totalchrom is None:
            pass
        elif not totalchrom.is_dir():
            problems.append("TotalChrom folder was not found.")
        else:
            results = sorted(totalchrom.glob("*.rst")) + sorted(totalchrom.glob("*.RST"))
            if not results:
                problems.append("TotalChrom folder has no result files.")
            for path in results:
                if path.stat().st_size < 1000 or not self._changed(path):
                    continue
                try:
                    self.remember(parse_rst(path))
                    changed = True
                except (OSError, ValueError):
                    self._seen.pop(str(path.resolve()), None)
        if cole_parmer is None:
            pass
        elif not cole_parmer.exists():
            problems.append("Cole-Parmer log was not found.")
        else:
            logs = _flow_files(cole_parmer)
            if not logs:
                problems.append("Cole-Parmer log was not found.")
            else:
                latest = max(logs, key=lambda item: item.stat().st_mtime)
                if self._changed(latest):
                    try:
                        self.flow = parse_flow_log(latest)
                        changed = True
                    except (OSError, ValueError):
                        self._seen.pop(str(latest.resolve()), None)
        if specview is None:
            pass
        elif not specview.exists():
            problems.append("SpecView log was not found.")
        else:
            chosen = _specview_file(specview)
            if chosen is None:
                problems.append("SpecView log was not found.")
            elif self._changed(chosen):
                try:
                    if chosen.suffix.lower() == ".csv":
                        self.specview = parse_specview_csv(chosen)
                    else:
                        self.specview = parse_specview_log(chosen)
                    changed = True
                except (OSError, ValueError):
                    self._seen.pop(str(chosen.resolve()), None)
        if problems or changed:
            summary = (
                f"{len(self.injections)} injections, "
                f"{len(self.flow)} flow readings, "
                f"{0 if self.specview is None else len(self.specview.times_utc)} SpecView rows"
            )
            self.status = f"{problems[0]} {summary}." if problems else summary
        if changed:
            self._series_rev += 1
        return changed

    def curve_signature(self, *, draft: bool) -> tuple:
        """Checked calibration files and their certified percents."""
        rows = []
        for name in sorted(self.calibration):
            entry = self.calibration[name]
            selected = entry.draft_use if draft else entry.use
            if not selected:
                continue
            rows.append((name, entry.h2, entry.co, entry.co2, entry.ch4))
        return tuple(rows)

    def calibration_ready(self) -> str:
        """What Use this calibration curve can do with the current checks.

        "curve" means a fresh fit matches the checked files.
        "graph" means the fit is unchanged and only Reject differs.
        """
        signature = self.curve_signature(draft=True)
        if self.pending_fits is not None and self.preview_signature == signature:
            return "curve"
        if (
            self.fits
            and signature == self.curve_signature(draft=False)
            and any(entry.draft_reject != entry.reject for entry in self.calibration.values())
        ):
            return "graph"
        return ""

    def _fit_entries(self, used: list[CalEntry]) -> dict[str, Fit]:
        if len(used) < 1:
            raise ValueError("Select at least one injection for the calibration.")
        rows = []
        for entry in used:
            injection = self.injections.get(entry.injection)
            if injection is None:
                raise ValueError(f"{entry.injection} is not loaded.")
            try:
                certified = entry.certified()
            except ValueError as exc:
                raise ValueError(f"{entry.injection} needs numeric certified percents.") from exc
            rows.append((injection, certified))
        if not any(certified["co2"] > 0 for _injection, certified in rows):
            raise ValueError(
                "Check a calibration gas that contains CO2. Its CO, CO2, and methane percents are the feed composition."
            )
        fits: dict[str, Fit] = {}
        for gas in GASES:
            xs = [certified[GAS_KEY[gas]] for _injection, certified in rows]
            ys = [injection.areas().get(gas, 0.0) for injection, _certified in rows]
            try:
                fits[gas] = ols(xs, ys)
            except ValueError as exc:
                raise ValueError(f"{gas}: {exc}") from exc
        return fits

    def preview_calibration(self) -> str:
        """Fit the checked files and hold the slopes until they are used."""
        fits = self._fit_entries([entry for entry in self.calibration.values() if entry.draft_use])
        self.pending_fits = fits
        self.preview_signature = self.curve_signature(draft=True)
        return (
            self.equation_text(fits)
            + "\n\nPress Use this calibration curve to put these slopes on the graph."
        )

    def apply_calibration(self) -> str:
        """Copy the held slopes onto the graph and match each file's checkboxes."""
        ready = self.calibration_ready()
        if ready == "curve" and self.pending_fits is not None:
            self.fits = dict(self.pending_fits)
        elif ready != "graph":
            raise ValueError("Recalculate the calibration curve before using it.")
        for entry in self.calibration.values():
            entry.use = entry.draft_use
            entry.reject = entry.draft_reject
        self.pending_fits = None
        self.preview_signature = None
        return self.equation_text()

    def save_calibration(self) -> str:
        self.preview_calibration()
        return self.apply_calibration()

    def equation_text(self, fits: dict[str, Fit] | None = None) -> str:
        chosen = self.fits if fits is None else fits
        lines = []
        for gas in GASES:
            fit = chosen.get(gas)
            if fit is None:
                continue
            sign = "+" if fit.intercept >= 0 else "−"
            lines.append(
                f"{gas}: y = {fit.slope:,.2f}x {sign} {abs(fit.intercept):,.0f}  ({fit.n_points} points)"
            )
        return "\n".join(lines)

    def method_text(self) -> str:
        """Plain-language path from the raw files to a plotted point, with the numbers in use."""
        carbon = self.inlet_carbon_fraction()
        carbon_text = "not set" if carbon is None else f"{carbon * 100:.4f} %"
        feed_lines = []
        curve_lines = []
        for name in sorted(self.calibration):
            entry = self.calibration[name]
            if not entry.use:
                continue
            try:
                certified = entry.certified()
            except ValueError:
                certified = None
            if certified is None:
                curve_lines.append(f"  {name}: certified percents are not numeric")
                continue
            curve_lines.append(
                f"  {name}: H2 {certified['h2']:g}, CO {certified['co']:g}, "
                f"CO2 {certified['co2']:g}, CH4 {certified['ch4']:g}"
            )
            if self._has_inlet_carbon(entry):
                feed_lines.append(
                    f"  {name}: CO {certified['co']:g} + CO2 {certified['co2']:g} + CH4 {certified['ch4']:g} "
                    f"= {certified['co'] + certified['co2'] + certified['ch4']:.4f} %"
                )
        inlet_name = None
        if self.specview is not None:
            inlet_name = _flow_column(self.specview)
        parts = [
            "From the raw files to a point on the graph",
            "",
            "A carbon-conversion point uses one TotalChrom injection, the SpecView sample nearest that time, and the Cole-Parmer readings around that time. The calibration curve turns peak areas into mole percents. The Run tab sliders only hide points. They do not change the calculated conversion.",
            "",
            "1. TotalChrom result file",
            "Each .rst file is one injection. Files smaller than 1,000 bytes are still being written and are skipped. The acquisition time in the file is real UTC. Peak areas are read from the result table. Doubles in that file are stored with the two 32-bit halves swapped, and the reader swaps them back.",
            "An unnamed peak is called hydrogen when its retention time is between 0.7 and 1.1 minutes and its area is greater than 1,000,000. If a gas is named twice, the larger area is kept. Missing gases are treated as area 0.",
            "Nothing is smoothed here. The area that enters the calibration curve is the area in the result file.",
            "",
            "2. Cole-Parmer outlet meter",
            "The log is a PuTTY text file. The clock in the header is the first sample, and each later line that ends in N2 is the next second. The mass-flow column is the meter's nitrogen-equivalent SLPM, including negative readings. No K-factor is applied. The outlet mixture changes through the run, so one K would be wrong for every point, and this meter's own composition table is not in the files.",
            f"At an injection, F_out is the median of those readings inside ±{OUTLET_WINDOW_S:g} seconds. At least {OUTLET_MIN_SAMPLES:g} readings are required, or the point has no outlet flow.",
            "Why this window: the meter reports once a second, so ±30 seconds is about 60 readings. That removes the 1.6% second-to-second noise. The wander that remains at one minute is about 4.5% of the reading, so a longer window does not make a quieter number and can mix two real flow rates. The median is used so one spike does not move the point.",
            "The outlet line on the Run tab may drop samples so the line stays under about 2,500 points. That thinning is for drawing only. F_out uses every reading inside the 30 second window.",
            "",
            "3. SpecView log",
            "A .LOG file is not encrypted. The reader skips any bytes before the first record, then reads the channel names and the samples. A CSV already converted from the log is read as text.",
            "The file labels its clock UTC. That computer is set to Eastern time, so each stamp is stored as the logged time plus 5 hours and is then real UTC. Cole-Parmer stamps stay on the wall clock. The dashboard compares them to the GC time shifted by "
            f"{self.utc_offset_hours:g} hours, which is Eastern Standard Time.",
            "Reactor temperature is the channel Internal Reactor Temperature Actual (Loop 1 in the log). Pressure on the Run tab is Pressure Actual (Loop 4). Neither is used in the conversion. Temperature is only the horizontal axis of the conversion graph.",
            "Inlet flow is "
            + (f"the channel {inlet_name}." if inlet_name else "not identified yet. Point the dashboard at a SpecView log and start watching.")
            + " A channel whose name contains sccm, mfc, feed flow, or inlet flow is used when one exists. Otherwise the reader keeps the unlabeled channel that spends time within 40 SCCM of 300, of 500, and of 800, with at least 30 samples near two of those rates. On the 19 November file that channel is AIs-.Module5 Inp2.",
            f"The value kept is the nearest sample within {MATCH_WINDOW_S:g} seconds. SpecView records about every 10 seconds, so the nearest sample is normally within 5 seconds. Thirty seconds still finds it if one record is missing. Two minutes is long enough to take the inlet from the wrong flow step, or about 2 °C of the wrong bed temperature on a 1 °C-per-minute ramp. A two-minute sample-line delay was checked on the 19 November run and did not improve the conversion, and the line volume was not measured, so no delay is added.",
            "A reading at or above 9,000, or below 0, is dropped. A reading above 20 is SCCM. A reading from 0.05 to 20 is treated as SLPM and multiplied by 1,000. Then the saved inlet meter K is applied:",
            f"  F_sccm = K * reading = {format_inlet_k(self.inlet_k)} * reading",
            "K is kept to three decimal places. 1.000 leaves the logged reading unchanged. The inlet line on the Run tab uses the same K. Readings outside 50 to 1,500 SCCM are left off that line. The conversion uses the nearest sample, not an average.",
            "",
            "4. Calibration curve",
            "Checked calibration injections are the standards. For each gas the fit is a straight line from certified percent (x) to peak area (y). When the checked files include two different percents:",
            "  area = slope * percent + intercept",
            "When every checked file has the same percent, the line goes through the origin and the areas at that percent are averaged:",
            "  area = slope * percent",
            "The percent used everywhere below is the rearrangement:",
            "  percent = (area - intercept) / slope",
            "The curve on the graph now is below. y is peak area and x is the certified percent:",
            self.equation_text() or "  No calibration curve has been applied yet.",
            "Standards on that curve:",
            "\n".join(curve_lines) or "  None.",
            "Checked calibration injections and rejected injections are left off the conversion graph. They are not dropped from the area table.",
            "",
            "5. Feed composition",
            "The streams do not contain the feed certificate. It is the certified CO, CO2, and methane on each checked calibration gas that contains CO2. Those three are added, the gases are averaged, and the result is divided by 100:",
            "  y_feed = mean(CO% + CO2% + CH4%) / 100",
            f"  y_feed now = {carbon_text}",
            "Files in that average:",
            "\n".join(feed_lines) or "  None. Check a calibration gas that contains CO2, then use the curve.",
            "Methane is on both sides. Leaving it out of the feed and counting it in the outlet would call unreacted methane a conversion. The 2:1 hydrogen/CO tank has no CO2, so it calibrates the curve and does not set y_feed. The four percents are not scaled to 100%.",
            "",
            "6. Carbon conversion",
            "For one injection that is not a standard and not rejected:",
            "  F_in  = F_sccm / 1000",
            "  C_in  = F_in * y_feed",
            "  C_out = F_out * (CO% + CO2% + CH4%) / 100",
            "  X     = 100 * (C_in - C_out) / C_in",
            "CO%, CO2%, and CH4% are the calibrated percents of that injection. X is carbon that left the gas. Liquid product is not in this number. No leak is subtracted. The 89 SCCM figure from the finished 19 November run was calculated afterward from carbon that never reached the gas measurement, so it is not known while the files are arriving.",
            "",
            "7. What the graphs show",
            "The left graph is wall-clock time against reactor temperature, pressure, inlet flow after K, and the outlet meter as recorded. The right graph is reactor temperature against X. Color is the inlet flow after K, in SCCM. Cold injections stay on the graph. Raise the low temperature slider on the Run tab to hide them. Move the slider back to its end and they return.",
        ]
        return "\n".join(parts)

    def inlet_carbon_fraction(self) -> float | None:
        checked = []
        for entry in self.calibration.values():
            if not entry.use or not self._has_inlet_carbon(entry):
                continue
            certified = entry.certified()
            checked.append(certified["co"] + certified["co2"] + certified["ch4"])
        if not checked:
            return None
        return (sum(checked) / len(checked)) / 100.0

    @staticmethod
    def _has_inlet_carbon(entry: CalEntry) -> bool:
        try:
            return entry.certified()["co2"] > 0
        except ValueError:
            return False

    def local_time(self, when_utc: datetime) -> datetime:
        return when_utc + timedelta(hours=self.utc_offset_hours)

    def outlet_slpm(self, when_utc: datetime) -> float | None:
        if not self.flow:
            return None
        center = self.local_time(when_utc)
        times, values, ordered = self._outlet_index()
        if not ordered:
            window = [
                sample.mass_slpm_n2
                for sample in self.flow
                if abs((sample.time_local - center).total_seconds()) <= OUTLET_WINDOW_S
            ]
        else:
            start = center - timedelta(seconds=OUTLET_WINDOW_S)
            stop = center + timedelta(seconds=OUTLET_WINDOW_S)
            window = values[bisect_left(times, start):bisect_right(times, stop)]
        if len(window) < OUTLET_MIN_SAMPLES:
            return None
        return float(np.median(window))

    def _outlet_index(self):
        if getattr(self, "_flow_index_id", None) != id(self.flow):
            times = [sample.time_local for sample in self.flow]
            ordered = all(times[i] <= times[i + 1] for i in range(len(times) - 1)) if len(times) > 1 else True
            self._flow_index_id = id(self.flow)
            self._flow_index = (times, [sample.mass_slpm_n2 for sample in self.flow], ordered)
        return self._flow_index

    def temperature_c(self, when_utc: datetime) -> float | None:
        series, _flow_name, temp_name = self._specview_columns()
        row = self._nearest_specview_row(when_utc)
        if series is None or temp_name is None or row is None:
            return None
        return series.columns[temp_name][row]

    def feed_sccm(self, when_utc: datetime) -> float | None:
        logged = self._specview_flow(when_utc)
        if logged is None:
            logged = self.manual_feed_sccm
        if logged is None:
            return None
        return logged * self.inlet_k

    def _specview_flow(self, when_utc: datetime) -> float | None:
        series, flow_name, _temp_name = self._specview_columns()
        row = self._nearest_specview_row(when_utc)
        if series is None or flow_name is None or row is None:
            return None
        value = series.columns[flow_name][row]
        if value is None or value >= 9000 or value < 0:
            return None
        if value > 20:
            return float(value)
        if value >= 0.05:
            return float(value) * 1000.0
        return None

    def _specview_columns(self):
        series = self.specview
        if series is None:
            return None, None, None
        if getattr(self, "_spec_columns_id", None) != id(series):
            self._spec_columns_id = id(series)
            self._spec_columns = (series, _flow_column(series), _temperature_column(series))
        return self._spec_columns

    def _nearest_specview_row(self, when_utc: datetime) -> int | None:
        series = self.specview
        if series is None or not series.times_utc:
            return None
        times, ordered = self._specview_times()
        if not ordered:
            best_i = None
            best_dt = 1e18
            for i, stamp in enumerate(times):
                gap = abs((stamp - when_utc).total_seconds())
                if gap < best_dt:
                    best_dt = gap
                    best_i = i
            if best_i is None or best_dt > MATCH_WINDOW_S:
                return None
            return best_i
        index = bisect_left(times, when_utc)
        best_i = None
        best_dt = 1e18
        for candidate in (index - 1, index):
            if candidate < 0 or candidate >= len(times):
                continue
            gap = abs((times[candidate] - when_utc).total_seconds())
            if gap < best_dt:
                best_dt = gap
                best_i = candidate
        if best_i is None or best_dt > MATCH_WINDOW_S:
            return None
        return best_i

    def _specview_times(self):
        series = self.specview
        if getattr(self, "_spec_times_id", None) != id(series):
            times = series.times_utc
            ordered = all(times[i] <= times[i + 1] for i in range(len(times) - 1)) if len(times) > 1 else True
            self._spec_times_id = id(series)
            self._spec_times = (times, ordered)
        return self._spec_times

    def conversions(self) -> list[ConversionPoint]:
        key = (
            self._series_rev,
            self.inlet_k,
            self.manual_feed_sccm,
            self.min_temperature_c,
            self.utc_offset_hours,
            tuple((name, fit.slope, fit.intercept) for name, fit in sorted(self.fits.items())),
            tuple(
                (name, entry.use, entry.reject, entry.h2, entry.co, entry.co2, entry.ch4)
                for name, entry in sorted(self.calibration.items())
            ),
        )
        if key == getattr(self, "_conv_key", None):
            return self._conv_points
        points = self._compute_conversions()
        self._conv_key = key
        self._conv_points = points
        return points

    def _compute_conversions(self) -> list[ConversionPoint]:
        points: list[ConversionPoint] = []
        standards = {name for name, entry in self.calibration.items() if entry.use}
        rejected = {name for name, entry in self.calibration.items() if entry.reject}
        carbon = self.inlet_carbon_fraction()
        co_fit = self.fits.get("Carbon Monoxide")
        co2_fit = self.fits.get("Carbon Dioxide")
        ch4_fit = self.fits.get("Methane")
        ordered = sorted(
            self.injections.values(),
            key=lambda item: (item.acquired_utc or datetime.min, item.sequence_index, item.name),
        )
        for injection in ordered:
            if injection.name in standards or injection.name in rejected or injection.acquired_utc is None:
                continue
            areas = injection.areas()
            temp = self.temperature_c(injection.acquired_utc)
            feed = self.feed_sccm(injection.acquired_utc)
            outlet = self.outlet_slpm(injection.acquired_utc)
            detail = ""
            conversion = None
            if self.min_temperature_c is not None and temp is not None and temp < self.min_temperature_c:
                detail = f"below {self.min_temperature_c:g} °C"
            elif co_fit is None or co2_fit is None or ch4_fit is None:
                detail = "Use a calibration curve to calculate conversion."
            elif carbon is None or carbon <= 0:
                detail = "Check a calibration gas that contains CO2."
            elif feed is None:
                detail = "No inlet flow in the SpecView log."
            elif outlet is None:
                detail = "No outlet flow in the Cole-Parmer log."
            elif temp is None:
                detail = "No reactor temperature in the SpecView log."
            else:
                y_co = mol_percent(areas.get("Carbon Monoxide", 0.0), co_fit)
                y_co2 = mol_percent(areas.get("Carbon Dioxide", 0.0), co2_fit)
                y_ch4 = mol_percent(areas.get("Methane", 0.0), ch4_fit)
                inlet = (feed / 1000.0) * carbon
                leaving = outlet * (y_co + y_co2 + y_ch4) / 100.0
                conversion = 100.0 * (inlet - leaving) / inlet
            points.append(
                ConversionPoint(
                    injection.name,
                    injection.acquired_utc,
                    temp,
                    feed,
                    outlet,
                    conversion,
                    detail,
                )
            )
        return points


def _flow_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    found = []
    for pattern in ("*.log", "*.txt", "*.LOG", "*.TXT"):
        found.extend(path.glob(pattern))
    return found


def _specview_file(path: Path) -> Path | None:
    if path.is_file():
        return path
    if not path.is_dir():
        return None
    found: list[Path] = []
    for pattern in ("*.LOG", "*.log", "*.csv", "*.CSV"):
        found.extend(path.glob(pattern))
    if not found:
        return None
    return max(found, key=lambda item: item.stat().st_mtime)


def _temperature_column(series: SpecviewSeries) -> str | None:
    preferred = "Internal Reactor Temperature Actual"
    if preferred in series.columns:
        return preferred
    return None


def _flow_column(series: SpecviewSeries) -> str | None:
    for name in series.columns:
        lowered = name.lower()
        if any(word in lowered for word in ("sccm", "mfc", "feed flow", "inlet flow")):
            return name
    # This SpecView project logs the inlet MFC as an unlabeled analog input.
    # During a run it sits a few SCCM above 300, 500, or 800. Pressure and
    # temperature pass through the same numbers but do not hold more than one
    # of those three rates.
    best_name = None
    best_score = 1
    for name, values in series.columns.items():
        lowered = name.lower()
        if any(word in lowered for word in ("pressure", "setpoint", "temperature", "output")):
            continue
        hits = [0, 0, 0]
        for value in values:
            if value is None or not 100 <= value <= 1500:
                continue
            for index, target in enumerate((300.0, 500.0, 800.0)):
                if abs(value - target) <= 40:
                    hits[index] += 1
        score = sum(1 for count in hits if count >= 30)
        if score > best_score:
            best_score = score
            best_name = name
    return best_name


def load_session(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_session(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
