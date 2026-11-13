#! /usr/bin/env python3
"""Ask a season's log index questions: find events, and measure things per match.

    ./season.py logs find '/RealOutputs/Alerts/errors' --contains CANivore
    ./season.py logs measure increase '/RealOutputs/Vision/BR/UpdatePoseCount'
    ./season.py logs measure sum '/RealOutputs/Vision/RobotPosesAccepted/length'
    ./season.py logs measure integral '/Drivetrain/*/DriveSupplyCurrent' --by season
    ./season.py logs entries '/RealOutputs/Vision/**'

`logs` is the folder the index was built for (or give --index). Questions run
against the index, not the .wpilog files, so they take seconds. When a question
reads an entry whose samples the index does not hold yet (a double or a struct),
those samples are stored first, once, from each log's file: auto-extend, as in
`analysis.py --index`. `--read-only` turns that off.

**Which logs.** `measure` defaults to official matches: qualification and
elimination logs recorded with the field system attached. `find` and `entries`
default to every log. `--logs matches` adds practice matches, `--logs all`
everything, and `--event` narrows to events by their code. A log whose name
carries no event code takes the code most other logs in its folder carry.

**Per match.** A match whose robot restarted part-way through has two logs; they
count as one match. `measure` reports each event's matches, a total where adding
up makes sense, the average per match, and the lowest and highest match.

**While.** Measures default to `--while enabled` (auto and teleop); `find`
defaults to `--while always`.
"""

import argparse
import bisect
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from entry_patterns import EntryPattern
from log_index import (DEFAULT_INDEX_NAME, NUMERIC_SCALAR_TYPES, IndexVersionError, LogIndex,
                       Manifest, NotAnIndexError, default_index_path)
from Log import Log

__all__ = ["LogInfo", "select_logs", "Timeline", "MEASURES", "measure", "find",
           "build_parser", "run", "main"]

GATES = {"enabled": {"auto", "teleop"}, "auto": {"auto"}, "teleop": {"teleop"},
         "disabled": {"disabled"}, "always": {"disabled", "auto", "teleop"}}

# Measures, and how per-log values combine into a match and an event.
#   additive: values add up, so a total means something.
#   weighted: a time-weighted average, combined by the seconds behind it.
#   extreme:  the lowest or highest.
MEASURES = {
    "count": ("additive", "records written: each one is a change of value"),
    "rises": ("additive", "times the value became true (or met the condition)"),
    "time": ("additive", "seconds the value was true (or met the condition)"),
    "sum": ("additive", "sum of the values written (an array counts its elements)"),
    "increase": ("additive", "how much a counter went up (a reset is not a decrease)"),
    "integral": ("additive", "value x seconds held: amps give coulombs, m/s gives meters"),
    "mean": ("weighted", "time-weighted mean of the value"),
    "rms": ("weighted", "time-weighted root mean square (heating goes as current squared)"),
    "max": ("extreme", "highest value held"),
    "min": ("extreme", "lowest value held"),
}
# Measures the index's per-mode statistics answer without any samples, for an
# entry that is a numeric scalar (not a struct leaf).
FROM_STATISTICS = {"mean", "rms", "max", "min", "integral"}


# === Which logs ===============================================================

@dataclass
class LogInfo:
    file_id: int
    name: str
    path: str
    event: Optional[str]
    match: Optional[str]
    fms_attached: bool
    recorded_at: Optional[str]
    last_us: Optional[int]

    @property
    def official(self) -> bool:
        return (self.fms_attached and self.match is not None
                and not self.match.startswith("p"))

    @property
    def match_key(self) -> Tuple[str, str]:
        """Logs of one match share this; a log that is not a match is its own."""
        if self.match is not None and self.fms_attached:
            return (self.event or "-", self.match)
        return (self.event or "-", self.name)


def select_logs(index: LogIndex, scope: str = "all",
                events: Sequence[str] = ()) -> List[LogInfo]:
    """Indexed logs in recording order, filtered by scope and event code."""
    rows = index.conn.execute(
        "SELECT f.id, f.name, MIN(p.path), f.event, f.match, f.fms_attached, "
        "f.recorded_at, f.last_us FROM files f LEFT JOIN paths p ON p.file_id = f.id "
        "GROUP BY f.id ORDER BY f.recorded_at IS NULL, f.recorded_at, f.name").fetchall()
    logs = [LogInfo(r[0], r[1], r[2] or "", r[3], r[4], bool(r[5]), r[6], r[7]) for r in rows]

    # A log whose name has no event code takes the one its folder's logs carry.
    by_folder: Dict[str, Counter] = defaultdict(Counter)
    for log in logs:
        if log.event:
            by_folder[os.path.dirname(log.path)][log.event] += 1
    for log in logs:
        if not log.event and by_folder.get(os.path.dirname(log.path)):
            log.event = by_folder[os.path.dirname(log.path)].most_common(1)[0][0]

    if scope == "official":
        logs = [log for log in logs if log.official]
    elif scope == "matches":
        logs = [log for log in logs if log.fms_attached]
    if events:
        wanted = {event.lower() for event in events}
        logs = [log for log in logs if (log.event or "").lower() in wanted]
    return logs


def describe_selection(logs: Sequence[LogInfo], scope: str) -> str:
    kind = {"official": "official match logs", "matches": "match logs",
            "all": "logs"}[scope]
    matches = len({log.match_key for log in logs})
    events = len({log.event for log in logs if log.event})
    text = f"{len(logs)} {kind}"
    if scope != "all":
        text += f" ({matches} matches"
        text += f", {events} events)" if events else ")"
    return text


# === Reading a log's fields ===================================================

class Timeline:
    """The robot's mode over a log, from the DriverStation entries."""

    def __init__(self, log: Log, end: float):
        changes = []
        for name, slot in (("/DriverStation/Enabled", 0), ("/DriverStation/Autonomous", 1)):
            field_ = log.get_field(name)
            if field_ is not None:
                changes += [(t, slot, bool(v)) for t, v in
                            zip(field_.data.timestamps, field_.data.values)]
        changes.sort(key=lambda change: change[0])
        state = [False, False]
        self.times: List[float] = []
        self.modes: List[str] = []
        for t, slot, value in changes:
            state[slot] = value
            mode = "disabled" if not state[0] else ("auto" if state[1] else "teleop")
            if self.modes and self.times[-1] == t:
                self.modes[-1] = mode
            elif not self.modes or self.modes[-1] != mode:
                self.times.append(t)
                self.modes.append(mode)
        self.end = end

    def mode_at(self, t: float) -> str:
        i = bisect.bisect_right(self.times, t) - 1
        return self.modes[i] if i >= 0 else "disabled"

    def mode_started(self, t: float) -> float:
        """When the mode in force at t began."""
        i = bisect.bisect_right(self.times, t) - 1
        return self.times[i] if i >= 0 else 0.0

    def windows(self, gate: str) -> List[Tuple[float, float]]:
        """The spans of time a gate admits."""
        wanted = GATES[gate]
        spans = []
        bounds = [0.0] + self.times + [self.end]
        modes = ["disabled"] + self.modes
        for i, mode in enumerate(modes):
            start, stop = bounds[i], min(bounds[i + 1], self.end)
            if mode in wanted and stop > start:
                if spans and spans[-1][1] == start:
                    spans[-1] = (spans[-1][0], stop)
                else:
                    spans.append((start, stop))
        return spans


def _overlap(start: float, end: float, windows: Sequence[Tuple[float, float]]) -> float:
    return sum(max(0.0, min(end, b) - max(start, a)) for a, b in windows)


class Reader:
    """Loads logs from the index for one entry pattern, storing samples on demand."""

    def __init__(self, index: LogIndex, read_only: bool = False, quiet: bool = False):
        self.index = index
        self.read_only = read_only
        self.quiet = quiet
        self.stored_now: List[str] = []
        self.unreadable: List[Tuple[str, str]] = []
        self._announced = False

    def load(self, info: LogInfo, pattern: str, position: str = "") -> Optional[Log]:
        config = {"checks": [{"entry": pattern}]}
        result = self.index.load_log(info.path, config) if info.path else None
        if result is not None and result.log is not None:
            return result.log
        reason = result.reason if result is not None else "no path to its file"
        if (result is not None and reason.startswith("needs entries")
                and not self.read_only and os.path.exists(info.path)):
            if not self._announced and not self.quiet:
                print(f"Storing the samples of {pattern} from each log's file, once; "
                      f"later questions read them from the index.", file=sys.stderr)
                self._announced = True
            if not self.quiet:
                print(f"  {position}{info.name}", file=sys.stderr)
            self.index.add(info.path, [pattern])
            self.stored_now.append(info.name)
            result = self.index.load_log(info.path, config)
            if result.log is not None:
                return result.log
            reason = result.reason
        self.unreadable.append((info.name, reason))
        return None


def log_end(log: Log, info: LogInfo) -> float:
    """Where the log ends, held back from a corrupt far-future timestamp."""
    end = log.get_last_timestamp()
    if info.last_us is not None:
        end = min(end, info.last_us / 1e6)
    return end


def matching_fields(log: Log, pattern: EntryPattern) -> List[str]:
    return sorted(key for key in log.get_field_keys()
                  if pattern.matches(key) and log.get_field(key).data.timestamps)


# === Conditions ===============================================================

def make_condition(equals: Any = None, contains: Optional[str] = None,
                   above: Optional[float] = None, below: Optional[float] = None
                   ) -> Callable[[Any], Any]:
    """A test on one value. Returns what matched (truthy) or a falsy value.

    For an array, `contains` returns the elements that contain the text, so an
    alert search shows the alert rather than every alert active at the time.
    """
    if contains is not None:
        text = contains.lower()

        def test(value: Any) -> Any:
            if isinstance(value, (list, tuple)):
                return [item for item in value if text in str(item).lower()]
            return value if text in str(value).lower() else None
        return test
    if equals is not None:
        return lambda value: value == equals
    if above is not None or below is not None:
        def test(value: Any) -> bool:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            return ((above is None or value > above)
                    and (below is None or value < below))
        return test
    return lambda value: (len(value) > 0 if isinstance(value, (list, tuple, str))
                          else bool(value))


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


# === Measures =================================================================

@dataclass
class Value:
    """One measure over one log (or match, or event)."""
    value: Optional[float]
    seconds: float = 0.0   # time behind a weighted value


def measure_samples(kind: str, timestamps: Sequence[float], values: Sequence[Any],
                    timeline: Timeline, gate: str,
                    condition: Callable[[Any], Any]) -> Value:
    """A measure of one field's samples within the gate."""
    windows = timeline.windows(gate)
    wanted = GATES[gate]
    n = len(timestamps)

    def in_gate(t: float) -> bool:
        return timeline.mode_at(t) in wanted

    if kind == "count":
        return Value(float(sum(1 for t in timestamps if in_gate(t))))
    if kind == "sum":
        total = 0.0
        for t, v in zip(timestamps, values):
            if in_gate(t):
                total += len(v) if isinstance(v, (list, tuple)) else (_number(v) or 0.0)
        return Value(total)
    if kind == "increase":
        total, previous = 0.0, None
        for t, v in zip(timestamps, values):
            number = _number(v)
            if number is None:
                continue
            if previous is not None and number > previous and in_gate(t):
                total += number - previous
            previous = number
        return Value(total)
    if kind == "rises":
        count, before = 0, False
        for t, v in zip(timestamps, values):
            now = bool(condition(v))
            if now and not before and in_gate(t):
                count += 1
            before = now
        return Value(float(count))

    # The rest weigh each value by how long it held, inside the gate.
    total = squares = seconds = 0.0
    low: Optional[float] = None
    high: Optional[float] = None
    for i in range(n):
        start = timestamps[i]
        end = timestamps[i + 1] if i + 1 < n else timeline.end
        held = _overlap(start, end, windows)
        if held <= 0:
            continue
        if kind == "time":
            if condition(values[i]):
                total += held
            continue
        number = _number(values[i])
        if number is None:
            continue
        seconds += held
        total += number * held
        squares += number * number * held
        low = number if low is None else min(low, number)
        high = number if high is None else max(high, number)
    if kind == "time":
        return Value(total)
    if kind == "integral":
        return Value(total if seconds else None)
    if kind == "mean":
        return Value(total / seconds if seconds else None, seconds)
    if kind == "rms":
        return Value(math.sqrt(squares / seconds) if seconds else None, seconds)
    if kind == "max":
        return Value(high)
    return Value(low)


def statistics_answer(index: LogIndex, logs: Sequence[LogInfo], pattern: EntryPattern) -> bool:
    """Whether per-mode statistics can answer for this pattern in these logs.

    They can when everything the pattern matches is a numeric scalar entry; then
    no samples are needed, so nothing has to be stored. A struct leaf is not an
    entry, so a pattern reaching one is answered from samples.
    """
    ids = [log.file_id for log in logs]
    types = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for name, type_ in index.conn.execute(
                f"SELECT DISTINCT name, type FROM entries WHERE file_id IN "
                f"({','.join('?' * len(chunk))})", chunk):
            if pattern.matches(name):
                types.setdefault(name, set()).add(type_)
    if not types or not all(t <= NUMERIC_SCALAR_TYPES for t in types.values()):
        return False
    # A pattern can also reach struct leaves, which the statistics do not cover.
    manifest_fields = Manifest(index, ids).matching(pattern.pattern)
    return set(manifest_fields) == set(types)


def measure_statistics(index: LogIndex, file_id: int, pattern: EntryPattern, kind: str,
                       gate: str) -> Dict[str, Value]:
    """The measure from per-mode statistics, for numeric scalar entries."""
    rows = index.conn.execute(
        "SELECT id, name, type FROM entries WHERE file_id = ?", (file_id,)).fetchall()
    matched = [(entry_id, name, type_) for entry_id, name, type_ in rows
               if pattern.matches(name)]
    modes = sorted(GATES[gate])
    out: Dict[str, Value] = {}
    for entry_id, name, _type in matched:
        stats = index.conn.execute(
            f"SELECT seconds, min, max, mean, rms FROM entry_stats WHERE entry_id = ? "
            f"AND mode IN ({','.join('?' * len(modes))})", [entry_id] + modes).fetchall()
        stats = [s for s in stats if s[0] and s[3] is not None]
        if not stats:
            continue
        seconds = sum(s[0] for s in stats)
        if kind == "integral":
            out[name] = Value(sum(s[0] * s[3] for s in stats))
        elif kind == "mean":
            out[name] = Value(sum(s[0] * s[3] for s in stats) / seconds, seconds)
        elif kind == "rms":
            out[name] = Value(math.sqrt(sum(s[0] * s[4] ** 2 for s in stats) / seconds),
                              seconds)
        elif kind == "max":
            out[name] = Value(max(s[2] for s in stats))
        else:
            out[name] = Value(min(s[1] for s in stats))
    return out


def combine(kind: str, values: Iterable[Value]) -> Value:
    """Several logs' (or matches') values as one."""
    present = [v for v in values if v.value is not None]
    if not present:
        return Value(None)
    style = MEASURES[kind][0]
    if style == "additive":
        return Value(sum(v.value for v in present))
    if kind == "max":
        return Value(max(v.value for v in present))
    if kind == "min":
        return Value(min(v.value for v in present))
    seconds = sum(v.seconds for v in present)
    if not seconds:
        return Value(None)
    if kind == "mean":
        return Value(sum(v.value * v.seconds for v in present) / seconds, seconds)
    return Value(math.sqrt(sum(v.value ** 2 * v.seconds for v in present) / seconds),
                 seconds)


@dataclass
class MeasureResult:
    kind: str
    pattern: str
    gate: str
    selection: str
    from_statistics: bool
    # (field, match key) -> value, matches in recording order
    per_match: Dict[Tuple[str, Tuple[str, str]], Value] = field(default_factory=dict)
    events: Dict[Tuple[str, str], str] = field(default_factory=dict)  # match -> event
    matches: List[Tuple[str, str]] = field(default_factory=list)
    unreadable: List[Tuple[str, str]] = field(default_factory=list)


def measure(index: LogIndex, logs: Sequence[LogInfo], kind: str, pattern_text: str,
            gate: str = "enabled", condition: Optional[Callable[[Any], Any]] = None,
            reader: Optional[Reader] = None, selection: str = "",
            combined: bool = False) -> MeasureResult:
    """`kind` of every field matching the pattern, per match, over the logs.

    With `combined`, every matching field counts as one: four drive motors'
    current integrals add up to the drivetrain's, their maxima give its maximum.
    """
    pattern = EntryPattern(pattern_text)
    condition = condition or make_condition()
    reader = reader or Reader(index)
    result = MeasureResult(kind, pattern_text, gate, selection, False)
    per_log: Dict[Tuple[str, Tuple[str, str]], List[Value]] = defaultdict(list)
    use_statistics = kind in FROM_STATISTICS and statistics_answer(index, logs, pattern)

    for number, info in enumerate(logs, 1):
        key = info.match_key
        if key not in result.events:
            result.events[key] = info.event or "-"
            result.matches.append(key)
        if use_statistics:
            found = measure_statistics(index, info.file_id, pattern, kind, gate)
        else:
            log = reader.load(info, pattern_text, f"[{number}/{len(logs)}] ")
            if log is None:
                continue
            timeline = Timeline(log, log_end(log, info))
            found = {}
            for name in matching_fields(log, pattern):
                data = log.get_field(name).data
                found[name] = measure_samples(kind, data.timestamps, data.values,
                                              timeline, gate, condition)
        if combined and found:
            found = {pattern_text: combine(kind, found.values())}
        for name, value in found.items():
            per_log[(name, key)].append(value)

    result.from_statistics = bool(use_statistics)
    for slot, values in per_log.items():
        result.per_match[slot] = combine(kind, values)
    result.unreadable = list(reader.unreadable)
    return result


# === Finding events ===========================================================

@dataclass
class Episode:
    log: LogInfo
    field: str
    start: float
    end: float
    mode: str
    into_mode: float
    value: Any

    @property
    def seconds(self) -> float:
        return self.end - self.start


def find(index: LogIndex, logs: Sequence[LogInfo], pattern_text: str,
         condition: Callable[[Any], Any], gate: str = "always",
         reader: Optional[Reader] = None) -> List[Episode]:
    """Every spell in which a matching field met the condition.

    A spell starts at the record where the condition became true and ends at the
    first record where it no longer held (or at the end of the log). One that
    overlaps the gate is reported.
    """
    pattern = EntryPattern(pattern_text)
    reader = reader or Reader(index)
    episodes: List[Episode] = []
    for number, info in enumerate(logs, 1):
        log = reader.load(info, pattern_text, f"[{number}/{len(logs)}] ")
        if log is None:
            continue
        timeline = Timeline(log, log_end(log, info))
        windows = timeline.windows(gate)
        for name in matching_fields(log, pattern):
            data = log.get_field(name).data
            open_at, matched = None, None
            for t, value in zip(data.timestamps, data.values):
                hit = condition(value)
                if hit and open_at is None:
                    open_at, matched = t, (hit if isinstance(hit, list) else value)
                elif not hit and open_at is not None:
                    episodes.append(_episode(info, name, open_at, t, matched, timeline))
                    open_at = None
            if open_at is not None:
                episodes.append(_episode(info, name, open_at, timeline.end, matched,
                                         timeline))
        if gate != "always":
            episodes = [e for e in episodes if e.log is not info
                        or _overlap(e.start, max(e.end, e.start + 1e-6), windows) > 0
                        or (e.end == e.start and timeline.mode_at(e.start) in GATES[gate])]
    return episodes


def _episode(info: LogInfo, name: str, start: float, end: float, value: Any,
             timeline: Timeline) -> Episode:
    return Episode(info, name, start, max(end, start), timeline.mode_at(start),
                   start - timeline.mode_started(start), value)


# === Output ===================================================================

def number_text(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"
    size = abs(value)
    if size >= 100:
        return f"{value:,.0f}"
    if size >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _table(rows: List[List[str]], right: Sequence[int]) -> List[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for n, row in enumerate(rows):
        cells = [cell.rjust(widths[i]) if i in right else cell.ljust(widths[i])
                 for i, cell in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return lines


def summary_rows(result: MeasureResult, by: str) -> List[Dict[str, Any]]:
    """Rows for the report: per match, per event, or per season, for each field."""
    style = MEASURES[result.kind][0]
    fields = sorted({name for name, _ in result.per_match})
    rows = []
    for name in fields:
        values = [(key, result.per_match[(name, key)]) for key in result.matches
                  if (name, key) in result.per_match
                  and result.per_match[(name, key)].value is not None]
        if by == "match":
            for key, value in values:
                rows.append({"field": name, "event": key[0], "match": key[1],
                             "value": value.value})
            continue
        groups: Dict[str, List[Value]] = defaultdict(list)
        order: List[str] = []
        for key, value in values:
            group = result.events[key] if by == "event" else "season"
            if group not in groups:
                order.append(group)
            groups[group].append(value)
        if by == "event" and len(order) > 1:
            order.append("season")
            groups["season"] = [v for _, v in values]
        for group in order:
            group_values = groups[group]
            numbers = [v.value for v in group_values]
            rows.append({
                "field": name, "group": group, "matches": len(group_values),
                "total": combine(result.kind, group_values).value
                         if style == "additive" else None,
                "per_match": sum(numbers) / len(numbers),
                "lowest": min(numbers), "highest": max(numbers),
                "overall": combine(result.kind, group_values).value
                           if style != "additive" else None,
            })
    return rows


def print_measure(result: MeasureResult, by: str) -> None:
    style = MEASURES[result.kind][0]
    source = "per-mode statistics" if result.from_statistics else "samples"
    unit = " (value x seconds)" if result.kind == "integral" else ""
    print(f"{result.kind}{unit} of {result.pattern} while {result.gate}, per match")
    print(f"{result.selection}; from {source}")
    rows = summary_rows(result, by)
    if not rows:
        print("\nNo matching field held a value in these logs.")
    fields = sorted({row["field"] for row in rows})
    for name in fields:
        mine = [row for row in rows if row["field"] == name]
        print(f"\n{name}")
        if by == "match":
            table = [["event", "match", result.kind]]
            table += [[row["event"], row["match"], number_text(row["value"])] for row in mine]
            print("\n".join(_table(table, right=[2])))
            continue
        head = ["", "matches"] + (["total"] if style == "additive" else ["overall"]) + \
               ["per match", "lowest", "highest"]
        table = [head]
        for row in mine:
            table.append([row["group"], str(row["matches"]),
                          number_text(row["total"] if style == "additive" else row["overall"]),
                          number_text(row["per_match"]), number_text(row["lowest"]),
                          number_text(row["highest"])])
        print("\n".join(_table(table, right=[1, 2, 3, 4, 5])))
    missing = len(result.matches) - len({key for _, key in result.per_match})
    if missing > 0:
        print(f"\n{missing} of {len(result.matches)} matches had no value for it.")
    for name, reason in result.unreadable:
        print(f"Not read: {name} ({reason})", file=sys.stderr)


def value_text(value: Any, limit: int = 90) -> str:
    if isinstance(value, list):
        text = "; ".join(str(item) for item in value)
    else:
        text = repr(value) if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def print_find(episodes: List[Episode], pattern: str, selection: str, limit: int) -> None:
    logs = len({id(e.log) for e in episodes})
    print(f"{pattern}: {len(episodes)} found in {logs} logs ({selection})")
    if not episodes:
        return
    shown = episodes[:limit] if limit else episodes
    fields = {e.field for e in episodes}
    table = [["event", "match", "t (s)", "when", "for (s)"]
             + (["entry"] if len(fields) > 1 else []) + ["value", "log"]]
    for e in shown:
        table.append([e.log.event or "-", e.log.match or "-", f"{e.start:.2f}",
                      f"{e.mode} +{e.into_mode:.1f}s", f"{e.seconds:.2f}"]
                     + ([e.field] if len(fields) > 1 else [])
                     + [value_text(e.value), e.log.name])
    print()
    print("\n".join(_table(table, right=[2, 4])))
    if len(shown) < len(episodes):
        print(f"\n... and {len(episodes) - len(shown)} more (--limit 0 shows all)")


def print_entries(manifest: Manifest, pattern: str, selection: str) -> int:
    names = manifest.matching(pattern)
    print(f"{pattern}: {len(names)} entr{'y' if len(names) == 1 else 'ies'} "
          f"in {selection}")
    if not names:
        close = manifest.suggestions(pattern)
        if close:
            print("Did you mean: " + ", ".join(close))
        return 1
    table = [["entry", "type", "logs", "samples stored"]]
    for name in names:
        types = sorted(t or "struct field" for t in manifest.types[name])
        logs = len(manifest.logs[name])
        stored = len(manifest.stored.get(name, ()))
        table.append([name, ", ".join(types), f"{logs}/{manifest.log_count}",
                      "all" if stored == logs else ("none" if not stored else
                                                    f"{stored} logs")])
    print()
    print("\n".join(_table(table, right=[2])))
    return 0


# === Command line =============================================================

def _parse_value(text: str) -> Any:
    """A command-line value as JSON when it is JSON (true, 12, "x"), else text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def build_parser() -> argparse.ArgumentParser:
    """The command line, also used to read the questions in a questions file."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--index", metavar="PATH",
                        help=f"index file (default: {DEFAULT_INDEX_NAME} in the log folder)")
    common.add_argument("--logs", choices=("official", "matches", "all"),
                        help="official = qualification and elimination matches with "
                             "the field attached; matches = any with the field "
                             "attached; all = every log")
    common.add_argument("--event", action="append", default=[], metavar="CODE",
                        help="only this event (its code, e.g. johnson); repeatable")
    common.add_argument("--read-only", action="store_true",
                        help="never store samples in the index; skip logs that lack them")
    condition = argparse.ArgumentParser(add_help=False)
    condition.add_argument("--equals", metavar="VALUE",
                           help="the value to look for (true, 12, OK, ...)")
    condition.add_argument("--contains", metavar="TEXT",
                           help="text to look for, ignoring case; for an array, in any "
                                "element")
    condition.add_argument("--above", type=float, metavar="N")
    condition.add_argument("--below", type=float, metavar="N")
    condition.add_argument("--while", dest="gate", choices=sorted(GATES),
                           help="robot modes to count (measure default: enabled; "
                                "find default: always)")

    parser = argparse.ArgumentParser(
        description="Ask a season's log index questions: find events, and measure "
                    "things per match.")
    parser.add_argument("log_folder", help="folder the index was built for "
                                           "(see log_index.py build)")
    commands = parser.add_subparsers(dest="command", required=True)

    find_parser = commands.add_parser(
        "find", parents=[common, condition],
        help="when an entry met a condition: event, match, time, how long")
    find_parser.add_argument("entry", help="entry name or pattern (* within a "
                                           "segment, ** across segments)")
    find_parser.add_argument("--limit", type=int, default=50,
                             help="rows to show; 0 for all (default 50)")

    measure_parser = commands.add_parser(
        "measure", parents=[common, condition],
        help="a measure of an entry per match, by event",
        epilog="measures: " + "; ".join(f"{k}: {v[1]}" for k, v in MEASURES.items()))
    measure_parser.add_argument("kind", choices=list(MEASURES), metavar="MEASURE",
                                help=", ".join(MEASURES))
    measure_parser.add_argument("entry", help="entry name or pattern; a pattern "
                                              "reports each matching entry")
    measure_parser.add_argument("--by", choices=("event", "match", "season"),
                                default="event", help="rows per event (default), per "
                                                      "match, or one for the season")
    measure_parser.add_argument("--combine", action="store_true",
                                help="treat every entry the pattern matches as one "
                                     "(e.g. all four drive motors)")
    measure_parser.add_argument("--json", action="store_true",
                                help="print the rows as JSON")

    entries_parser = commands.add_parser(
        "entries", parents=[common], help="which entries match a pattern, their types, "
                                          "and in how many logs")
    entries_parser.add_argument("entry", help="entry name or pattern")
    return parser


def default_scope(args: argparse.Namespace) -> str:
    return args.logs or ("official" if args.command == "measure" else "all")


def condition_from(args: argparse.Namespace) -> Callable[[Any], Any]:
    return make_condition(_parse_value(args.equals) if args.equals is not None else None,
                          args.contains, args.above, args.below)


def run(args: argparse.Namespace, index: LogIndex) -> int:
    """Answer one parsed command from an open index, printing the answer."""
    scope = default_scope(args)
    logs = select_logs(index, scope, args.event)
    selection = describe_selection(logs, scope)
    if not logs:
        print(f"No {selection.split(' ', 1)[1]} in the index"
              + (f" for {', '.join(args.event)}" if args.event else ""))
        return 1
    if args.command == "entries":
        return print_entries(Manifest(index, [log.file_id for log in logs]),
                             args.entry, selection)

    test = condition_from(args)
    reader = Reader(index, read_only=args.read_only)
    if args.command == "find":
        episodes = find(index, logs, args.entry, test, args.gate or "always", reader)
        print_find(episodes, args.entry, selection, args.limit)
        return 0

    result = measure(index, logs, args.kind, args.entry, args.gate or "enabled",
                     test, reader, selection, combined=args.combine)
    if args.json:
        print(json.dumps({"measure": result.kind, "entry": result.pattern,
                          "while": result.gate, "selection": result.selection,
                          "rows": summary_rows(result, args.by)}, indent=1))
    else:
        print_measure(result, args.by)
    return 0


def open_index(path: str, log_folder: str) -> Optional[LogIndex]:
    """The index, or None after saying why it cannot be opened."""
    try:
        return LogIndex(path)
    except FileNotFoundError:
        print(f"No index at {path}; build one with: log_index.py build {log_folder}",
              file=sys.stderr)
    except (IndexVersionError, NotAnIndexError) as error:
        print(f"Error: {error}", file=sys.stderr)
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    index = open_index(args.index or default_index_path(args.log_folder), args.log_folder)
    if index is None:
        return 2
    try:
        return run(args, index)
    finally:
        index.close()


if __name__ == "__main__":
    sys.exit(main())
