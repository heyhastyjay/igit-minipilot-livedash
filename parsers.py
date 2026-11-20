"""Readers for the three instruments on the mini-pilot.

TotalChrom .rst doubles are stored with the two 32-bit halves swapped.
SpecView .LOG files may start with a few bytes before the first 0x09 record.
The Cole-Parmer file is a PuTTY log. Its mass flow is the N2-equivalent
reading, stored as the meter sent it.
"""

from __future__ import annotations

import csv
import math
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class Peak:
    component: str
    rt_min: float
    area: float
    height: float
    area_percent: float


@dataclass
class Injection:
    name: str
    path: str
    acquired_utc: datetime | None
    sequence_index: int
    peaks: list[Peak] = field(default_factory=list)

    def areas(self) -> dict[str, float]:
        found: dict[str, float] = {}
        for peak in self.peaks:
            name = peak.component
            if (
                not name
                and 0.7 <= peak.rt_min <= 1.1
                and peak.area > 1_000_000
            ):
                name = "Hydrogen"
            if not name:
                continue
            if name not in found or peak.area > found[name]:
                found[name] = peak.area
        return found


class RstError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes, name: str) -> None:
        self.data = data
        self.n = len(data)
        self.i = 0
        self.name = name

    def need(self, n: int) -> bytes:
        if self.i + n > self.n:
            raise RstError(f"{self.name}: file ended early")
        chunk = self.data[self.i : self.i + n]
        self.i += n
        return chunk

    def u32(self) -> int:
        return struct.unpack(">I", self.need(4))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self.need(4))[0]

    def f64(self) -> float:
        raw = self.need(8)
        swapped = raw[4:8] + raw[0:4]
        return struct.unpack("<d", swapped[::-1])[0]

    def string(self) -> str:
        length = self.u32()
        if length > 1_000_000:
            raise RstError(f"{self.name}: implausible string length")
        padded = length + (4 - length % 4) % 4
        raw = self.need(padded)[:length]
        return raw.split(b"\x00", 1)[0].decode("latin1", errors="replace").strip()

    def skip(self, n: int) -> None:
        self.need(n)

    def timestamp(self) -> int:
        seconds = self.u32()
        self.skip(8)
        return seconds


def _areas_match(total: float, got: float) -> bool:
    tol = max(1e-4, abs(total) * 1e-8)
    return abs(got - total) <= tol


def _plausible(peak: Peak) -> bool:
    if not all(math.isfinite(v) for v in (peak.rt_min, peak.area, peak.height)):
        return False
    if peak.rt_min <= 0 or peak.rt_min > 180:
        return False
    return -1.0 <= peak.area_percent <= 110.0


def _read_peak(reader: _Reader, order: int) -> Peak:
    for _ in range(6):
        reader.i32()
    reader.f64()
    retention_s = reader.f64()
    reader.f64()
    area = reader.f64()
    height = reader.f64()
    for _ in range(9):
        reader.u32()
    reader.f64()
    reader.f64()
    component = reader.string()
    reader.i32()
    reader.i32()
    reader.i32()
    reader.f64()
    fraction = reader.f64()
    for _ in range(4):
        reader.f64()
    reader.u32()
    reader.u32()
    reader.f64()
    reader.skip(40)
    return Peak(component, retention_s / 60.0, area, height, fraction * 100.0)


def _skip_groups(reader: _Reader) -> None:
    reader.skip(4)
    count = reader.u32()
    if count > 10_000:
        raise RstError(f"{reader.name}: implausible group count")
    for _ in range(count):
        reader.string()
        reader.u32()
        reader.u32()
        for _k in range(5):
            reader.f64()
        for _k in range(3):
            reader.u32()
        for _k in range(5):
            reader.f64()
        for _k in range(3):
            reader.u32()


def _scan_peak_table(reader: _Reader, total_area: float) -> list[Peak]:
    best: list[Peak] | None = None
    best_diff = 1e300
    data = reader.data
    for pos in range(0, reader.n - 4, 4):
        if data[pos] != 0:
            continue
        trial = _Reader(data, reader.name)
        trial.i = pos
        try:
            count = trial.u32()
            if count == 0 or count > 100:
                continue
            peaks = []
            total = 0.0
            for i in range(count):
                peak = _read_peak(trial, i)
                if not _plausible(peak):
                    raise RstError("skip")
                peaks.append(peak)
                total += peak.area
        except (RstError, struct.error):
            continue
        if _areas_match(total_area, total):
            diff = abs(total - total_area)
            if diff < best_diff:
                best = peaks
                best_diff = diff
    if best is None:
        raise RstError(f"{reader.name}: could not find the peak table")
    best.sort(key=lambda peak: peak.rt_min)
    return best


def _clock(seconds: int) -> datetime | None:
    if seconds < 1_000_000_000 or seconds > 2_200_000_000:
        return None
    return datetime.utcfromtimestamp(seconds)


def _specview_utc(stamp: datetime) -> datetime:
    """SpecView labels its clock UTC, but that clock is set to Eastern time.

    The numbers in the log are the lab wall clock. Real UTC is five hours later.
    """
    return stamp + timedelta(hours=5)


def parse_rst(path: Path) -> Injection:
    data = path.read_bytes()
    reader = _Reader(data, path.name)
    if reader.need(4) != b"PENX":
        raise RstError(f"{path.name}: not a TotalChrom result file")
    for _ in range(6):
        reader.u32()
    reader.string()
    reader.string()
    reader.string()
    reader.string()
    reader.timestamp()
    reader.string()
    reader.string()
    reader.timestamp()
    reader.string()
    reader.u32()
    reader.u32()
    reader.string()
    reader.string()
    total_area = reader.f64()
    reader.f64()
    reader.f64()
    reader.f64()
    reader.u32()
    reader.string()
    reader.string()
    reader.u32()
    acquired = reader.timestamp()
    reader.u32()
    reader.string()
    reader.string()
    sequence_index = reader.i32()
    reader.string()
    reader.u32()
    reader.u32()
    reader.f64()
    reader.u32()
    reader.u32()
    reader.string()
    reader.f64()
    reader.f64()
    reader.u32()
    reader.skip(8)
    reader.string()
    reader.i32()
    reader.i32()
    reader.i32()
    reader.string()
    reader.string()
    reader.string()
    for _ in range(10):
        reader.string()
    for _ in range(8):
        reader.f64()
    reader.i32()
    reader.string()
    reader.i32()
    reader.string()
    reader.string()
    reader.i32()
    reader.string()
    reader.string()
    reader.string()
    reader.skip(44)

    peaks: list[Peak] | None = None
    mark = reader.i
    try:
        _skip_groups(reader)
        count = reader.u32()
        if count <= 10_000:
            found = [_read_peak(reader, i) for i in range(count)]
            if count == 0 or _areas_match(total_area, sum(p.area for p in found)):
                peaks = found
    except (RstError, struct.error):
        peaks = None
    if peaks is None:
        reader.i = mark
        peaks = _scan_peak_table(reader, total_area)
    peaks.sort(key=lambda peak: peak.rt_min)
    return Injection(
        name=path.stem,
        path=str(path),
        acquired_utc=_clock(acquired),
        sequence_index=sequence_index,
        peaks=peaks,
    )


@dataclass
class FlowSample:
    time_local: datetime
    psia: float
    temp_c: float
    volumetric_lpm: float
    mass_slpm_n2: float
    gas: str


_PUTTY = re.compile(
    r"PuTTY log (\d{4})\.(\d{2})\.(\d{2}) (\d{2}):(\d{2}):(\d{2})"
)


def parse_flow_log(path: Path) -> list[FlowSample]:
    text = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n").decode("latin1")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return []
    match = _PUTTY.search(lines[0])
    if match is None:
        raise ValueError(f"{path.name}: no PuTTY start time in the header")
    clock = datetime(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
        int(match.group(5)),
        int(match.group(6)),
    )
    samples: list[FlowSample] = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 5 or parts[-1].upper() != "N2":
            continue
        samples.append(
            FlowSample(
                clock,
                float(parts[0]),
                float(parts[1]),
                float(parts[2]),
                float(parts[3]),
                parts[-1],
            )
        )
        clock += timedelta(seconds=1)
    return samples


@dataclass
class SpecviewSeries:
    times_utc: list[datetime]
    columns: dict[str, list[float | None]]


def _specview_name(name: str) -> str:
    if name == "Loop1.Process Value":
        return "Internal Reactor Temperature Actual"
    if name == "Loop4.Process Value":
        return "Pressure Actual"
    return name


def parse_specview_log(path: Path) -> SpecviewSeries:
    data = path.read_bytes()
    n = len(data)

    def u16(pos: int) -> int:
        return data[pos] | (data[pos + 1] << 8)

    def u32(pos: int) -> int:
        return data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16) | (data[pos + 3] << 24)

    def valid(pos: int) -> int:
        if pos + 4 > n:
            return 0
        tag = data[pos]
        if tag not in (0x03, 0x06, 0x09):
            return 0
        length = u16(pos + 2)
        if length < 6 or pos + length > n:
            return 0
        return length

    def block_header(pos: int) -> bool:
        if pos + 8 > n or data[pos] != 0x01 or data[pos + 2] != 0x08 or data[pos + 3] != 0x00:
            return False
        ts = u32(pos + 4)
        return 946684800 <= ts <= 4102444800

    pos = 0
    while pos + 8 <= n and not (data[pos] == 0x09 and valid(pos)):
        pos += 1
    channels: list[tuple[int, str]] = []
    while pos + 8 <= n and not block_header(pos):
        length = valid(pos)
        if data[pos] != 0x09 or length < 13:
            raise ValueError(f"{path.name}: not a SpecView .LOG file")
        ident = u32(pos + 4)
        flag = u32(pos + 8)
        body = data[pos + 12 : pos + length]
        name = body.split(b"\x00", 1)[0].decode("latin1", errors="replace").strip(" ;\r\n")
        if flag == 1 and name:
            channels.append((ident, _specview_name(name)))
        pos += length
    if not channels or not block_header(pos):
        raise ValueError(f"{path.name}: not a SpecView .LOG file")

    index = {ident: i for i, (ident, _name) in enumerate(channels)}
    times: list[datetime] = []
    columns: dict[str, list[float | None]] = {name: [] for _ident, name in channels}
    names = [name for _ident, name in channels]

    while pos + 8 <= n and block_header(pos):
        ts = u32(pos + 4)
        pos += 8
        if pos + 4 <= n and data[pos : pos + 4] == b"\x08\x00\x04\x00":
            pos += 4
        have = [None] * len(channels)
        samples = 0
        while pos < n and not block_header(pos):
            length = valid(pos)
            if not length:
                if n - pos < 8:
                    pos = n
                    break
                raise ValueError(f"{path.name}: unrecognized record after {len(times)} rows")
            if data[pos] == 0x03 and length >= 12:
                ident = u32(pos + 4)
                value = struct.unpack("<f", data[pos + 8 : pos + 12])[0]
                col = index.get(ident)
                if col is not None and math.isfinite(value):
                    have[col] = float(value)
                    samples += 1
            pos += length
        if samples == 0:
            continue
        times.append(_specview_utc(datetime.utcfromtimestamp(ts)))
        for i, name in enumerate(names):
            columns[name].append(have[i])
    return SpecviewSeries(times, columns)


def parse_specview_csv(path: Path) -> SpecviewSeries:
    with path.open(newline="", encoding="latin1") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        time_col = 0
        columns: dict[str, list[float | None]] = {
            name: [] for i, name in enumerate(header) if i != time_col and name
        }
        names = [name for i, name in enumerate(header) if i != time_col and name]
        times: list[datetime] = []
        for row in reader:
            if not row or not row[0].strip():
                continue
            stamp = row[0].strip().replace("T", " ")
            try:
                when = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            times.append(_specview_utc(when))
            for i, name in enumerate(names, start=1):
                if i >= len(row) or row[i].strip() == "":
                    columns[name].append(None)
                else:
                    try:
                        columns[name].append(float(row[i]))
                    except ValueError:
                        columns[name].append(None)
    return SpecviewSeries(times, columns)
