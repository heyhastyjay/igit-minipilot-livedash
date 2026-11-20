"""Figures for the live dashboard. No window code, so they can be saved headless."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, Normalize

from engine import MATCH_WINDOW_S, Dashboard, _flow_column


@dataclass
class Trim:
    """Hours and degrees kept on the plots. Hours are measured from start_utc."""

    start_utc: datetime
    run_min_h: float
    run_max_h: float
    temp_min_c: float
    temp_max_c: float
    limit_temperature: bool


def included_injection_times(dash: Dashboard) -> list[datetime]:
    """Injections drawn on the graphs. Calibration gas and rejected rows are left out."""
    standards = {name for name, entry in dash.calibration.items() if entry.use}
    rejected = {name for name, entry in dash.calibration.items() if entry.reject}
    times = []
    for injection in dash.injections.values():
        if injection.acquired_utc is None or injection.name in standards or injection.name in rejected:
            continue
        times.append(injection.acquired_utc)
    return times


def coverage(dash: Dashboard) -> tuple[datetime, datetime] | None:
    """Run window: 15 minutes before the first included injection through 15 minutes after the last."""
    times = included_injection_times(dash)
    if times:
        pad = timedelta(minutes=15)
        return min(times) - pad, max(times) + pad
    stamps: list[datetime] = []
    series = dash.specview
    if series is not None and series.times_utc:
        stamps.append(series.times_utc[0])
        stamps.append(series.times_utc[-1])
    if dash.flow:
        stamps.append(_flow_utc(dash.flow[0].time_local, dash))
        stamps.append(_flow_utc(dash.flow[-1].time_local, dash))
    for injection in dash.injections.values():
        if injection.acquired_utc is not None:
            stamps.append(injection.acquired_utc)
    if not stamps:
        return None
    return min(stamps), max(stamps)


def temperature_bounds(dash: Dashboard) -> tuple[float, float] | None:
    series = dash.specview
    if series is None:
        return None
    span = coverage(dash)
    values = []
    for stamp, value in zip(series.times_utc, series.columns.get("Internal Reactor Temperature Actual", [])):
        if value is None:
            continue
        if span is not None and not span[0] <= stamp <= span[1]:
            continue
        values.append(value)
    if not values:
        return None
    return min(values), max(values)


def draw(fig: plt.Figure, dash: Dashboard, trim: Trim | None = None) -> None:
    fig.clear()
    fig.set_facecolor("white")
    time_ax = fig.add_subplot(1, 2, 1)
    conversion_ax = fig.add_subplot(1, 2, 2)
    lookup = _temperature_lookup(dash)
    handles, labels = _run_variables(time_ax, dash, trim, lookup)
    _conversion(fig, conversion_ax, dash, trim)
    fig.tight_layout(pad=1.2)
    _place_runtime_key(fig, time_ax, handles, labels)


def _flow_utc(stamp_local: datetime, dash: Dashboard) -> datetime:
    return stamp_local - timedelta(hours=dash.utc_offset_hours)


def _est(stamp_utc: datetime, dash: Dashboard) -> datetime:
    return stamp_utc + timedelta(hours=dash.utc_offset_hours)


def _temperature_lookup(dash: Dashboard):
    series = dash.specview
    if series is None:
        return None
    name = "Internal Reactor Temperature Actual"
    if name not in series.columns:
        return None
    paired = [(stamp, value) for stamp, value in zip(series.times_utc, series.columns[name]) if value is not None]
    if not paired:
        return None
    return [stamp for stamp, _value in paired], [value for _stamp, value in paired]


def _temp_at(lookup, stamp: datetime) -> float | None:
    if lookup is None:
        return None
    times, values = lookup
    index = bisect_left(times, stamp)
    best = None
    best_gap = 1e18
    for candidate in (index - 1, index):
        if candidate < 0 or candidate >= len(times):
            continue
        gap = abs((times[candidate] - stamp).total_seconds())
        if gap < best_gap:
            best_gap = gap
            best = values[candidate]
    if best is None or best_gap > MATCH_WINDOW_S:
        return None
    return best


def _visible(stamp_utc: datetime, dash: Dashboard, trim: Trim | None, lookup) -> bool:
    if trim is None:
        return True
    hours = (stamp_utc - trim.start_utc).total_seconds() / 3600.0
    if hours < trim.run_min_h or hours > trim.run_max_h:
        return False
    if not trim.limit_temperature:
        return True
    temp = _temp_at(lookup, stamp_utc)
    return temp is not None and trim.temp_min_c <= temp <= trim.temp_max_c


def _run_variables(ax, dash: Dashboard, trim: Trim | None, lookup) -> None:
    ax.set_title("Run variables")
    ax.set_xlabel("Time (EST)")
    plotted = False
    handles = []
    labels = []
    series = dash.specview
    if series is not None and series.times_utc:
        temp_name = "Internal Reactor Temperature Actual"
        if temp_name in series.columns:
            times, values = _kept(series.times_utc, series.columns[temp_name], dash, trim, lookup)
            if trim is not None and trim.limit_temperature:
                times = [stamp for stamp, value in zip(times, values) if trim.temp_min_c <= value <= trim.temp_max_c]
                values = [value for value in values if trim.temp_min_c <= value <= trim.temp_max_c]
            if times:
                line, = ax.plot([_est(stamp, dash) for stamp in times], values, color="#1f6feb", lw=1.2)
                handles.append(line)
                labels.append("Reactor temperature (°C)")
                plotted = True
        flow_name = _flow_column(series)
        if flow_name is not None:
            times, values = _kept(series.times_utc, series.columns[flow_name], dash, trim, lookup)
            chosen = [
                (stamp, value * dash.inlet_k)
                for stamp, value in zip(times, values)
                if 50 <= value <= 1500
            ]
            if chosen:
                inlet = ax.twinx()
                inlet.spines["left"].set_position(("axes", -0.14))
                inlet.spines["left"].set_visible(True)
                inlet.yaxis.set_label_position("left")
                inlet.yaxis.set_ticks_position("left")
                line, = inlet.plot(
                    [_est(stamp, dash) for stamp, _value in chosen],
                    [value for _stamp, value in chosen],
                    color="#8250df",
                    lw=1.0,
                )
                inlet.set_ylabel("Inlet flow (SCCM)")
                inlet.tick_params(labelsize=8)
                handles.append(line)
                labels.append("Inlet flow (SCCM)")
                plotted = True
        if "Pressure Actual" in series.columns:
            times, values = _kept(series.times_utc, series.columns["Pressure Actual"], dash, trim, lookup)
            if times:
                pressure = ax.twinx()
                line, = pressure.plot([_est(stamp, dash) for stamp in times], values, color="#9a6700", lw=1.0)
                pressure.set_ylabel("Pressure (psi)")
                pressure.tick_params(labelsize=8)
                handles.append(line)
                labels.append("Pressure (psi)")
                plotted = True
    if dash.flow:
        paired = [
            (_flow_utc(sample.time_local, dash), sample.mass_slpm_n2)
            for sample in dash.flow
            if _visible(_flow_utc(sample.time_local, dash), dash, trim, lookup)
        ]
        if paired:
            step = max(1, len(paired) // 2500)
            flow_ax = ax.twinx()
            flow_ax.spines["right"].set_position(("axes", 1.14))
            line, = flow_ax.plot(
                [_est(stamp, dash) for stamp, _value in paired][::step],
                [value for _stamp, value in paired][::step],
                color="#1a7f37",
                lw=1.0,
            )
            flow_ax.set_ylabel("Outlet flow (SLPM)")
            flow_ax.tick_params(labelsize=8)
            handles.append(line)
            labels.append("Outlet flow (SLPM, N2)")
            plotted = True
    ax.set_ylabel("Temperature (°C)")
    ax.tick_params(labelsize=8)
    ax.grid(True, color="#e6e6e6", lw=0.6)
    if plotted:
        _crop_time_axis(ax, dash, trim)
        _format_est_axis(ax)
    else:
        ax.text(0.5, 0.5, "Waiting for SpecView or Cole-Parmer data", ha="center", va="center", transform=ax.transAxes)
    return handles, labels


def _crop_time_axis(ax, dash: Dashboard, trim: Trim | None) -> None:
    if trim is None:
        return
    left = _est(trim.start_utc + timedelta(hours=trim.run_min_h), dash)
    right = _est(trim.start_utc + timedelta(hours=trim.run_max_h), dash)
    if right <= left:
        return
    ax.set_xlim(_naive(left), _naive(right))


def _clock(stamp: datetime) -> str:
    hour = stamp.hour % 12 or 12
    suffix = "AM" if stamp.hour < 12 else "PM"
    if stamp.minute:
        return f"{hour}:{stamp.minute:02d} {suffix}"
    return f"{hour} {suffix}"


def _naive(stamp: datetime) -> datetime:
    return stamp.replace(tzinfo=None) if stamp.tzinfo is not None else stamp


def _format_est_axis(ax) -> None:
    left, right = ax.get_xlim()
    start = _naive(mdates.num2date(left, tz=None))
    end = _naive(mdates.num2date(right, tz=None))
    locator = mdates.AutoDateLocator(minticks=4, maxticks=7)
    ticks = []
    for tick in locator.tick_values(start, end):
        stamp = tick if isinstance(tick, datetime) else mdates.num2date(tick, tz=None)
        stamp = _naive(stamp)
        if start <= stamp <= end:
            ticks.append(stamp)
    if len(ticks) < 2:
        return
    labels = []
    shown_day = None
    for stamp in ticks:
        clock = _clock(stamp)
        day = stamp.date()
        if day != shown_day:
            labels.append(f"{stamp.strftime('%b')} {stamp.day}\n{clock}")
            shown_day = day
        else:
            labels.append(clock)
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels)
    ax.set_xlabel("Time (EST)")


def _place_runtime_key(fig: plt.Figure, ax, handles, labels) -> None:
    if not handles:
        return
    original = ax.get_position()
    gap = 0.14
    lowered = [original.x0, original.y0, original.width, max(original.height - gap, 0.2)]
    for other in fig.axes:
        box = other.get_position()
        same_column = abs(box.x0 - original.x0) < 0.03 and abs(box.width - original.width) < 0.08
        same_row = abs(box.y0 - original.y0) < 0.03
        if same_column and same_row:
            other.set_position(lowered)
    ax.set_title("")
    fig.text(
        original.x0 + original.width / 2,
        original.y1,
        "Run variables",
        ha="center",
        va="top",
        fontsize=12,
    )
    fig.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(original.x0 + original.width / 2, original.y1 - 0.025),
        bbox_transform=fig.transFigure,
        ncol=2,
        fontsize=8,
        frameon=False,
        borderaxespad=0,
    )


def _kept(times, values, dash: Dashboard, trim: Trim | None, lookup):
    kept_t = []
    kept_v = []
    for stamp, value in zip(times, values):
        if value is None or not _visible(stamp, dash, trim, lookup):
            continue
        kept_t.append(stamp)
        kept_v.append(value)
    step = max(1, len(kept_t) // 2500)
    return kept_t[::step], kept_v[::step]


def _conversion(fig: plt.Figure, ax, dash: Dashboard, trim: Trim | None) -> None:
    ax.set_title("Carbon conversion")
    ax.set_xlabel("Temperature (°C)")
    ax.set_ylabel("Carbon conversion (%)")
    points = []
    blocked: list[str] = []
    outside = False
    for point in dash.conversions():
        ready = (
            point.conversion_pct is not None
            and point.temperature_c is not None
            and point.feed_sccm is not None
            and point.when_utc is not None
        )
        if not ready:
            if point.detail:
                blocked.append(point.detail)
            continue
        if trim is not None:
            hours = (point.when_utc - trim.start_utc).total_seconds() / 3600.0
            if hours < trim.run_min_h or hours > trim.run_max_h:
                outside = True
                continue
            if trim.limit_temperature and not trim.temp_min_c <= point.temperature_c <= trim.temp_max_c:
                outside = True
                continue
        points.append(point)
    if not points:
        if outside:
            message = "No points in this window. Widen the run-time or temperature sliders."
        elif blocked:
            message = max(set(blocked), key=blocked.count)
        elif dash.fits:
            message = "No sample injections to plot. Calibration gases and rejected files stay off this graph."
        else:
            message = "Use a calibration curve to calculate conversion."
        ax.text(0.5, 0.5, message, ha="center", va="center", wrap=True, transform=ax.transAxes)
        return
    feeds = np.array([point.feed_sccm for point in points], dtype=float)
    low = float(feeds.min())
    high = float(feeds.max())
    if high <= low:
        high = low + 1.0
    cmap = LinearSegmentedColormap.from_list("inlet", ["#3EC1FF", "#FF9A1A"])
    ax.scatter(
        [point.temperature_c for point in points],
        [point.conversion_pct for point in points],
        c=feeds,
        cmap=cmap,
        vmin=low,
        vmax=high,
        s=42,
        zorder=3,
    )
    norm = Normalize(low, high)
    mapper = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    mapper.set_array([])
    bar = fig.colorbar(mapper, ax=ax, fraction=0.046, pad=0.04)
    bar.set_label("Inlet flow (SCCM)")
    ax.tick_params(labelsize=8)
    ax.grid(True, color="#e6e6e6", lw=0.6)
