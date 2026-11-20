# Development notes

Developed by Dr. Julia Hasty.

Read this before changing the dashboard. The lab user starts the program by double-clicking `START-DASHBOARD.bat`. This file is for the person, or the coding agent, who changes the program.

The folder is the whole program. It does not contain run data. Each computer keeps its own session at `%USERPROFILE%\.sbu-live-dashboard.json` (on Linux, `~/.sbu-live-dashboard.json`). Do not commit that file. It holds local paths and the calibration that person has applied.

Python 3.9 or newer. Every module starts with `from __future__ import annotations` so `list[str]` and `X | None` stay valid on 3.9. Dependencies are only `numpy` and `matplotlib`. Tk comes with the python.org Windows installer. The lab computer is Windows. The same files run on Linux with Tk and those two packages.

## Files

| File | Role |
| --- | --- |
| `live_dashboard.py` | Tk window. Paths, watch button, Run constants, three tabs, session load and save. |
| `engine.py` | Loaded state, calibration fit, carbon conversion, and the Method tab text. |
| `parsers.py` | TotalChrom `.rst`, Cole-Parmer PuTTY log, SpecView `.LOG` and CSV. |
| `plots.py` | The two graphs. Calls `dash.conversions()` and only hides points. |
| `START-DASHBOARD.bat` | What the lab user double-clicks. Stored with Windows CRLF on purpose. |
| `00-READ-ME-FIRST.txt` | The same start steps, in plain text, sorted to the top of the folder. |
| `requirements.txt` | `numpy` and `matplotlib`. |

`plots.py` imports `_flow_column` from `engine.py`. That name is shared on purpose so the inlet trace and the conversion use the same SpecView channel.

## What the window is doing

Three path rows, then one row of two boxes:

- Left box: **Start watching** / **Stop watching**. Watching polls every 2 seconds and rereads a file only when its size or mtime changes.
- Right box: **Run constants**, with the label `inlet meter k value`, a text field, and **Save**.

Tabs, in order: **Run**, **Calibration**, **Method**.

The bottom of the window says `Developed by Dr. Julia Hasty.` Leave that credit in place.

On startup the window forces three fields after loading the session, and does not reset `inlet_k`:

- `utc_offset_hours = -5` (Eastern Standard Time). Not editable.
- `min_temperature_c = None`. Cold points stay. The Run-tab temperature slider is the only way to hide them.
- `manual_feed_sccm = None`. There is no notes file and no typed feed rate. Inlet flow comes from SpecView times K.

If any path is already saved, the window reads the files once about 200 ms after open. It does not start watching unless `SBU_DASHBOARD_AUTOSTART=1`. `SBU_DASHBOARD_TAB=calibration` selects the Calibration tab. Those two variables are for the person running it, not for the lab user.

## Interface rules that are easy to break

These were set so a person can demo the window without a click redrawing the graph before they are ready.

1. A Cal gas checkbox or a Reject checkbox only flips `draft_use` or `draft_reject`. It does not fit, and it does not redraw.
2. **Recalculate calibration curve** fits the checked files into `pending_fits` and shows the equations. It does not replace `self.fits` and does not redraw.
3. **Use this calibration curve** copies the pending fit onto `self.fits`, copies the draft checks onto `use` and `reject`, saves the session, and redraws. The button stays disabled until `calibration_ready()` returns `"curve"` (a fresh fit matches the checks) or `"graph"` (the fit is unchanged and only Reject differs).
4. Editing a certified percent on a checked file invalidates the preview. They have to recalculate again.
5. Typing the inlet meter K does nothing to the graph. **Save** is the only action that stores K and redraws. Save rounds to 3 decimal places and writes the field back with `format_inlet_k`, so `0.970` stays `0.970`. Do not format K with `:g`. That drops the trailing zero.
6. Heavy work runs on the Tk thread. Before a scan, Save, or Use, call `_busy` so the status line and the wait cursor paint (`update_idletasks`) before the work. After it, `_idle` and then set the finished message.
7. A watch tick that finds nothing changed must not replace the status line. A Save or Use message has to stay until the next real change.
8. Sliders update their readout immediately and redraw 200 ms after the drag stops. They only hide points.
9. `draw()` may call `conversions()` again. `Dashboard.conversions` caches by data revision, K, fits, and which files are used or rejected. Keep that cache, or a Save will calculate the series twice. The cache key has to change when injections, flow, SpecView, K, fits, or the applied checks change.
10. Do not bring back a notes file for feed rate or inlet composition. Feed composition is the checked calibration gas. Inlet flow is the SpecView reading times K.

## Inlet meter K

Default is `1.0`, displayed as `1.000`. The 19 November spec-sheet factor is 0.960. That is not the default.

`format_inlet_k` in `engine.py` is the only display format. Save does `round(float(text), 3)` and then `format_inlet_k`. The Method tab prints the same string in `F_sccm = K * reading`.

JSON stores K as a number, so the file may contain `0.97`. The box still shows `0.970` the next time it opens.

## Calibration math

`ols` in `engine.py`:

- Two or more points and more than one distinct x: `np.polyfit` of degree 1. Do not average replicate areas before this fit. Averaging first changes the slope of a real multi-level curve.
- One distinct non-zero x: line through the origin, slope = mean(area) / percent, intercept 0.
- Otherwise raise. The message the user sees is `needs two different certified percents, or one non-zero percent`.

`_fit_entries` checks, in this order: at least one checked file, every checked file has numeric percents, at least one checked file has CO2 > 0, then fit all four gases. A blank percent must say `needs numeric certified percents` and must not be reported as a missing-CO2 error. A 2:1 hydrogen/CO tank (CO2 = 0) is allowed on the curve and does not set the feed composition.

Mole percent of a sample is `(area - intercept) / slope`.

`y_feed` is the mean of `(CO% + CO2% + CH4%) / 100` over applied rows (`entry.use`) with CO2 > 0. Methane is on both sides of the balance. Leaving it out of the feed and counting it in the outlet treats unreacted methane as conversion. The four certified percents are not scaled to 100%.

Checked standards (`use`) and rejected rows are omitted from the conversion series. They stay in the calibration table.

`feed` and `skip` still exist on `CalEntry` so an older session loads. The window does not show them and does not toggle them.

## Carbon conversion

For one injection that is not a standard and not rejected, and that has a timestamp:

```
F_sccm = K * nearest SpecView inlet reading
F_in   = F_sccm / 1000
C_in   = F_in * y_feed
C_out  = F_out * (CO% + CO2% + CH4%) / 100
X      = 100 * (C_in - C_out) / C_in
```

`F_out` is the Cole-Parmer nitrogen-equivalent mass flow in SLPM. There is no outlet K and no leak subtraction. An 89 SCCM leak was estimated after the 19 November run from carbon that never reached the gas measurement. It is not known while files are arriving, so the live number does not subtract it. Liquid product is not in X.

Needs CO, CO2, and CH4 fits. A missing temperature, inlet, or outlet leaves `conversion_pct` empty and sets `detail` to the reason the empty-graph message can show.

## Clocks

- TotalChrom `.rst` acquisition time is real UTC.
- SpecView labels its clock UTC, but the lab computer is set to Eastern. `parsers._specview_utc` adds 5 hours and stores real UTC. That +5 is hardcoded. It is not `utc_offset_hours`.
- Cole-Parmer stamps stay on the wall clock (Eastern).
- The dashboard shifts GC time by `utc_offset_hours` (−5) to meet Cole-Parmer. Daylight time would be −4. Do not make this a box on the window unless the SpecView +5 moves with it. The two offsets have to describe the same clock.

## Match windows

`MATCH_WINDOW_S = 30` for temperature and inlet. SpecView logs about every 10 seconds. Thirty seconds still finds the nearest sample if one record is missing. Two minutes can grab the next 300/500/800 step, or about 2 °C on a 1 °C/min ramp. A two-minute sample-line delay was tried on the 19 November run and did not improve conversion. The line volume was not measured, so no delay is added.

`OUTLET_WINDOW_S = 30` and `OUTLET_MIN_SAMPLES = 10`. Cole-Parmer is 1 Hz. The median of that window takes out about 1.6% second-to-second noise. The wander left at 60 seconds is about 4.5% of the reading, so a longer window is not quieter and can mix two real flow rates. The median ignores a spike or a negative. Fewer than 10 samples means no outlet flow.

Nearest SpecView row and the outlet window are bisect lookups on time-ordered series. If a file is not ordered, the code falls back to a full scan. Keep that fallback. Do not go back to scanning every sample for every injection: on the November run that made each redraw several seconds and froze the window.

The Run-tab lines may keep only about 2,500 points (`plots.py`). That thinning is for drawing. `F_out` uses every reading inside the 30 second window.

## What each parser does

TotalChrom `.rst`: files under 1,000 bytes are still being written and are skipped. Peak doubles are stored with the two 32-bit halves swapped. `_Reader.f64` swaps them back. An unnamed peak with retention time 0.7–1.1 min and area > 1e6 is Hydrogen. A duplicate component keeps the larger area. A missing gas is area 0.

Cole-Parmer: PuTTY log, read as latin1. The clock in the header is sample 0. Each later line that ends in N2 is +1 second. The mass-flow number is the meter's nitrogen-equivalent SLPM. Negatives are kept. No K.

SpecView: not encrypted. A `.LOG` skips bytes until the first `0x09` record. A CSV is plain text. Temperature channel is `Internal Reactor Temperature Actual` (Loop 1). Pressure on the Run tab is `Pressure Actual` (Loop 4). Neither enters X. Inlet channel: a name containing sccm, mfc, feed flow, or inlet flow, otherwise the unlabeled analog that sits within 40 of 300, of 500, and of 800, with at least 30 hits on at least two of those three. On the 19 November file that channel is `AIs-.Module5 Inp2`. Drop readings ≥ 9000 or < 0. A value > 20 is SCCM. A value from 0.05 to 20 is SLPM and is multiplied by 1,000. Then multiply by K. The inlet line on the graph keeps raw readings in 50–1500 and then multiplies by K.

## Method tab

`Dashboard.method_text()` is rebuilt from the live K, the applied fits, and the applied feed gases. `_redraw` writes it into the Method tab. If you change an equation, a window, or an assumption, change that text in the same edit. The point of the tab is that a person can check the math from the raw file to the plotted point without reading this note.

## Session JSON

`to_json` writes `utc_offset_hours`, `min_temperature_c`, `manual_feed_sccm`, `inlet_k`, `calibration` (including draft flags and the unused `feed` and `skip`), and `fits`. The window adds `totalchrom`, `cole_parmer`, and `specview`. There is no `notes` key anymore. If an old file still has one, ignore it.

Closing the window saves. An unsaved K sitting in the text box is not written, because persist uses `dash.inlet_k`, not the text.

## Speed

A full November run is on the order of 50 injections, ~34,000 Cole-Parmer rows, and ~4,000 SpecView rows. After the bisect lookups, a redraw is a few tenths of a second. The first open is about a second. If a change makes a button freeze with no status change, the status was set after the work, or `conversions()` ran twice, or a per-injection scan of the whole log came back.

## Checks worth keeping

- `format_inlet_k(0.97)` is `0.970`. Save of the text `0.970` leaves the box as `0.970`.
- A blank certified percent on a checked file errors before the CO2 check.
- A curve of only CO2 = 0 tanks errors and asks for a gas that contains CO2.
- One percent, several replicates, fits a line through the origin.
- Two different percents use `np.polyfit` on the individual points.
- Outlet median ignores a single spike. A sample more than 30 seconds from every flow reading, or a window with fewer than 10 readings, has no outlet.
- Checkbox clicks do not call `_redraw`. Recalculate does not call `_redraw`. Use and Save do.
