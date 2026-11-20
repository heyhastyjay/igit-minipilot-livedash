# Live dashboard

## Start

On the lab computer, open this folder and double-click **START-DASHBOARD.bat**.

A black window checks Python, installs numpy and matplotlib the first time, and opens the dashboard. Leave the black window open. Closing it closes the dashboard.

The same steps are in `00-READ-ME-FIRST.txt`, which is the first file in the folder.

Python 3.9 or newer is required. If the black window says Python was not found, install it from [python.org](https://www.python.org/downloads/) and check **Add python.exe to PATH** on the first installer page, then double-click the bat file again. The first launch needs internet. Later launches do not.

From a command prompt in this folder, the same start is:

```
py -3 -m pip install -r requirements.txt
py -3 live_dashboard.py
```

## Point it at the run

Use Browse on the three path rows:

- **TotalChrom folder:** the directory TotalChrom writes `.rst` files into.
- **Cole-Parmer log:** the PuTTY log. Mass flow is kept as the N2-equivalent reading.
- **SpecView log:** the `.LOG` file, or a CSV already converted from it. Reactor temperature is Loop 1. Pressure is Loop 4. Inlet flow is the channel that holds a few SCCM above 300, 500, or 800 while gas is flowing. On the 19 November file that channel is `AIs-.Module5 Inp2`.

**Start watching** is the button in the box on the left. **Run constants** is the box on the right. The inlet meter k value multiplies the SpecView inlet reading. Type it, then click **Save**. Save stores that value and redraws the graphs. A value of 1 leaves the logged reading unchanged. Typing alone does not change the graph.

SpecView's log is labeled UTC, but that computer's clock is set to Eastern time, so the dashboard reads those stamps as Eastern. Temperature and inlet flow are the nearest SpecView sample within 30 seconds. Outlet flow is the median of the Cole-Parmer readings inside the same 30 seconds, and at least ten readings are required. A checked calibration gas that contains CO2 supplies the feed composition from its CO, CO2, and methane percents.

The Run tab shows times in Eastern Standard Time. Two sliders trim what is drawn: run time, in hours from the first sample, and reactor temperature. Leave a slider at its ends to keep the whole range, including the cold start. Raise the low temperature slider to drop those points yourself. The tab updates every two seconds while watching.

The Calibration tab lists every result file. Check Cal gas on the tank injections and enter the certified percents. Check Reject to keep an injection off the graphs. Those checks only mark the files. **Recalculate calibration curve** fits the slopes from the checked files and shows the equations. **Use this calibration curve** then recalculates carbon conversion and redraws the graph. Checked calibration gases stay off that plot. If a checked gas contains CO2, its CO, CO2, and methane percents are the feed composition.

The Method tab writes down each file, the equation applied to it, and the smoothing and assumptions, using the K and the calibration curve that are on the graph.

The session, including the saved calibration, is written to `%USERPROFILE%\.sbu-live-dashboard.json` on the lab computer. This folder does not contain run data or a saved session. Each person points the three Browse rows at the files on that computer.
