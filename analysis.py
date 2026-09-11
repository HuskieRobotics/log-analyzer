#! /usr/bin/env python3

import argparse
import json
import re
import mmap
import os
import sys
import bisect
import statistics
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any, Union
from datalog import DataLogReader
from Log import Log, LoggableType
from entry_patterns import EntryPattern, expand_roles
from report_output import (FileReport, Report, ReportSection,
                           now_text, render_html, render_json)

# Constants for structured types
STRUCT_PREFIX = "struct:"

# Set to True for detailed output
VERBOSE = False


# === Computed results =========================================================
# These carry everything an analysis produced, with no formatting decisions baked
# in, so the same computation can be rendered as terminal text, HTML or JSON.

@dataclass
class ValueLocation:
    """Where a particular value occurred."""
    log_file_name: str
    timestamp: float


@dataclass
class CalculationResult:
    """One configured calculation, computed."""
    calc_type: str
    name: str
    unit: str
    value: Optional[Union[int, float]] = None
    locations: List[ValueLocation] = field(default_factory=list)
    # Outlier calculations yield several values, each with its own locations.
    outliers: List[Tuple[float, List[ValueLocation]]] = field(default_factory=list)
    error: Optional[str] = None
    unknown_type: bool = False


@dataclass
class AnalysisResult:
    """Everything one analysis produced, across one file or across all files."""
    is_aggregate: bool
    total_count: int
    all_values: List[Union[int, float, str, bool]]
    unit: str
    has_values: bool
    has_numeric: bool
    calculations: List[CalculationResult] = field(default_factory=list)


@dataclass
class PerFileCounts:
    """Per-file match counts for an aggregated analysis."""
    files_processed: int
    counts: List[int]
    show_detail: bool
    average: Optional[float] = None
    min_count: Optional[int] = None
    min_file: Optional[str] = None
    max_count: Optional[int] = None
    max_file: Optional[str] = None


# === Computation ==============================================================

def find_value_locations(results: List[Tuple[str, List[Union[int, float, str, bool]], List[float]]],
                         target: Union[int, float], use_abs: bool) -> List[ValueLocation]:
    """Find where a value occurs, reporting its first position in each file.

    Args:
        results: List of tuples containing log file name, data, and timestamps
        target: The value to locate
        use_abs: Whether to match against absolute values

    Returns:
        One ValueLocation per file containing the value
    """
    found = []
    for log_file_name, file_data, timestamps in results:
        haystack = ([abs(x) for x in file_data if isinstance(x, (int, float))]
                    if use_abs else file_data)
        if target in haystack:
            found.append(ValueLocation(log_file_name, timestamps[haystack.index(target)]))
    return found


def compute_calculation(calc: Dict[str, Any],
                        results: List[Tuple[str, List[Union[int, float, str, bool]], List[float]]],
                        numeric_values: List[Union[int, float]],
                        abs_numeric_values: List[Union[int, float]],
                        value_unit: str) -> CalculationResult:
    """Compute one configured calculation over already-filtered numeric values."""
    calc_type = calc.get('type')
    computed = CalculationResult(
        calc_type=calc_type,
        name=calc.get('name', f'{calc_type} calculation'),
        unit=value_unit,
    )

    if calc_type == 'average':
        computed.value = sum(numeric_values) / len(numeric_values)
    elif calc_type == 'max':
        computed.value = max(numeric_values)
        computed.locations = find_value_locations(results, computed.value, use_abs=False)
    elif calc_type == 'min':
        computed.value = min(numeric_values)
        computed.locations = find_value_locations(results, computed.value, use_abs=False)
    elif calc_type == 'abs_average':
        computed.value = sum(abs_numeric_values) / len(abs_numeric_values)
    elif calc_type == 'abs_max':
        computed.value = max(abs_numeric_values)
        computed.locations = find_value_locations(results, computed.value, use_abs=True)
    elif calc_type == 'abs_min':
        computed.value = min(abs_numeric_values)
        computed.locations = find_value_locations(results, computed.value, use_abs=True)
    elif calc_type == 'count':
        computed.value = len(numeric_values)
    elif calc_type in ('outlier_2std', 'abs_outlier_2std'):
        use_abs = calc_type == 'abs_outlier_2std'
        source = abs_numeric_values if use_abs else numeric_values
        if len(source) < 2:
            computed.error = "Cannot calculate with less than 2 values"
        else:
            mean = statistics.mean(source)
            stddev = statistics.stdev(source)
            for outlier in [x for x in source if abs(x - mean) > 2 * stddev]:
                computed.outliers.append(
                    (outlier, find_value_locations(results, outlier, use_abs)))
    else:
        computed.unknown_type = True

    return computed


def compute_analysis(results: List[Tuple[str, List[Union[int, float, str, bool]], List[float]]],
                     calculations: List[Dict[str, Any]],
                     value_unit: str = "") -> AnalysisResult:
    """Compute an analysis without rendering it.

    Args:
        results: List of tuples containing log file name, data (time differences
            or values), and timestamps; one entry per log file
        calculations: List of calculation configs from the analysis config
        value_unit: Unit for values (e.g. "s" for time differences, "m" for meters)

    Returns:
        An AnalysisResult holding every computed figure and its locations
    """
    all_data = []
    for _, file_data, _ in results:
        all_data.extend(file_data)

    computed = AnalysisResult(
        is_aggregate=len(results) > 1,
        total_count=len(all_data),
        all_values=all_data,
        unit=value_unit,
        has_values=bool(all_data),
        has_numeric=False,
    )
    if not all_data:
        return computed

    # bool is a subclass of int, so booleans are treated as numeric here, as
    # they always have been.
    numeric_values = [v for v in all_data if isinstance(v, (int, float))]
    abs_numeric_values = [abs(v) for v in numeric_values]
    computed.has_numeric = bool(numeric_values)
    if not numeric_values:
        return computed

    computed.calculations = [
        compute_calculation(calc, results, numeric_values, abs_numeric_values, value_unit)
        for calc in calculations
    ]
    return computed


def compute_per_file_counts(results: List[Tuple[str, List[Union[int, float, str, bool]], List[float]]],
                            calculations: List[Dict[str, Any]]) -> PerFileCounts:
    """Compute per-file match-count statistics for an aggregated analysis.

    Args:
        results: List of tuples containing log file name, data, and timestamps; one per file
        calculations: List of calculation configs; the per-file detail is only
            reported when a "count" calculation was requested

    Returns:
        A PerFileCounts holding the counts and, when requested, the extremes
    """
    counts = [len(data) for _, data, _ in results]
    show_detail = bool(counts) and "count" in [calc.get('type') for calc in calculations]
    computed = PerFileCounts(
        files_processed=len(results), counts=counts, show_detail=show_detail)

    if show_detail:
        computed.min_count = min(counts)
        computed.max_count = max(counts)
        computed.min_file = results[counts.index(computed.min_count)][0]
        computed.max_file = results[counts.index(computed.max_count)][0]
        computed.average = sum(counts) / len(counts)

    return computed


# === Checks ===================================================================
# A check states what normal looks like and reports the deviation. Unlike the
# time and value analyses, which report what happened, a check can also report
# what did *not* happen - an entry that never appeared, or a signal that never
# reached its expected state. Absence is not self-interpreting: a stall flag that
# never fires is good news, a camera that never reports is not, so every rule
# declares its own expectation.

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class CheckFinding:
    """One deviation from a rule's stated expectation."""
    rule_name: str
    severity: str
    entry: str
    detail: str
    log_file_name: str
    timestamp: Optional[float] = None
    occurrences: int = 1
    file_count: int = 1


@dataclass
class CheckReport:
    """Every finding from one log file, plus what was actually examined."""
    findings: List[CheckFinding] = field(default_factory=list)
    rules_run: int = 0
    entries_checked: int = 0


class EnabledGate:
    """Answers whether the robot was enabled at a given timestamp."""

    def __init__(self, log: Log):
        field_data = log.get_field("/DriverStation/Enabled")
        data = field_data.data if field_data else None
        self.timestamps = list(data.timestamps) if data else []
        self.values = list(data.values) if data else []
        # Everything before the robot is first enabled is boot: threads starve,
        # NT connections drop, JIT is still running. That noise recurs every
        # match and hides real problems.
        self.first_enabled_at = next(
            (timestamp for timestamp, value in zip(self.timestamps, self.values)
             if value), None)

    def is_enabled_at(self, timestamp: float) -> bool:
        """The most recent Enabled value at or before this timestamp."""
        if not self.timestamps:
            return False
        index = bisect.bisect_right(self.timestamps, timestamp) - 1
        return bool(self.values[index]) if index >= 0 else False

    def windows(self, gate: str, last_timestamp: float) -> List[Tuple[float, float]]:
        """The time spans a gate admits, for measuring durations rather than
        filtering samples.

        Filtering samples and *then* building intervals would split one
        excursion that spans a brief disable into two. Building intervals over
        every sample and clipping their duration to these windows keeps the
        count right while excluding time the gate does not admit.
        """
        if gate not in ("enabled", "disabled", "afterFirstEnable"):
            return [(0.0, last_timestamp)]
        if gate == "afterFirstEnable":
            if self.first_enabled_at is None:
                return []
            return [(self.first_enabled_at, last_timestamp)]

        want = gate == "enabled"
        spans, start = [], None
        for timestamp, value in zip(self.timestamps, self.values):
            if bool(value) == want and start is None:
                start = timestamp
            elif bool(value) != want and start is not None:
                spans.append((start, timestamp))
                start = None
        if start is not None:
            spans.append((start, last_timestamp))
        return spans

    def applies(self, gate: str, timestamp: float) -> bool:
        """Whether a sample passes the rule's "while" gate.

        "afterFirstEnable" is usually what a post-match report wants rather than
        "enabled": sticky faults are published after the fact, so a fault caused
        during a match commonly appears only once the robot has been disabled
        again. Gating strictly on "enabled" discards them.
        """
        if gate == "enabled":
            return self.is_enabled_at(timestamp)
        if gate == "disabled":
            return not self.is_enabled_at(timestamp)
        if gate == "afterFirstEnable":
            return (self.first_enabled_at is not None
                    and timestamp >= self.first_enabled_at)
        return True


@dataclass
class Excursion:
    """One unbroken spell past a threshold."""
    start: float
    end: float
    peak: float
    peak_at: float
    unresolved: bool = False       # still past the limit when the log ended
    counted: float = 0.0           # duration the gate admits

    @property
    def span(self) -> float:
        return self.end - self.start


def overlap_seconds(start: float, end: float,
                    windows: List[Tuple[float, float]]) -> float:
    """How much of [start, end) falls inside any window."""
    return sum(max(0.0, min(end, high) - max(start, low)) for low, high in windows)


def find_excursions(timestamps: List[float], values: List[Any],
                    enter: float, clear: float, above: bool,
                    last_timestamp: float) -> List[Excursion]:
    """Group samples into spells past a threshold.

    A sample's value is taken to hold until the next sample, which is what these
    entries actually mean: temperatures are logged on change, so a long gap is a
    long hold rather than missing data. It also means a crossing is only located
    to within one sample interval.

    `clear` provides hysteresis: a spell opens at `enter` but does not close
    until the value comes back past `clear`. Without it a reading resting on the
    limit produces a burst of one-sample spells, exactly when it is most
    marginal.
    """
    def past(value: float) -> bool:
        return value > enter if above else value < enter

    def cleared(value: float) -> bool:
        return value < clear if above else value > clear

    excursions: List[Excursion] = []
    open_at: Optional[float] = None
    peak = peak_at = 0.0

    for index, timestamp in enumerate(timestamps):
        value = values[index]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if open_at is None:
            if past(value):
                open_at, peak, peak_at = timestamp, value, timestamp
        else:
            if (value > peak) if above else (value < peak):
                peak, peak_at = value, timestamp
            if cleared(value):
                excursions.append(Excursion(open_at, timestamp, peak, peak_at))
                open_at = None

    if open_at is not None:
        # Still past the limit when logging stopped. In the pit that is the more
        # alarming case: the next match starts from there.
        excursions.append(
            Excursion(open_at, last_timestamp, peak, peak_at, unresolved=True))
    return excursions


def describe_excursions(excursions: List[Excursion], enter: float, above: bool,
                        unit: str = "") -> str:
    """One line summarising every spell past the limit for one entry."""
    total = sum(e.counted for e in excursions)
    longest = max(excursions, key=lambda e: e.counted)
    peak_holder = max(excursions, key=lambda e: e.peak if above else -e.peak)
    suffix = f" {unit}" if unit else ""
    parts = [f"{'above' if above else 'below'} {enter:g}{suffix} for {total:.1f} s"]
    if len(excursions) > 1:
        # With a single spell the total is the longest, so saying both is noise.
        parts.append(f"across {len(excursions)} intervals")
        parts.append(f"longest {longest.counted:.1f} s")
    parts.append(f"peak {peak_holder.peak:g}{suffix} at {peak_holder.peak_at:.1f} s")
    if any(e.unresolved for e in excursions):
        parts.append("still past the limit when the log ended")
    return ", ".join(parts)


def match_statistic(samples: Any, statistic: str, gate: "EnabledGate",
                    gate_name: str) -> Optional[float]:
    """Reduce one entry's samples for a match to a single comparable number.

    Args:
        samples: The LogValueSet for the entry
        statistic: "peak", "min", "mean" or "rise". "rise" is measured from the
            first sample the gate admits, not from the log's first sample - with
            "while": "enabled" it is therefore the rise during the match, and
            note that a range read is half-open, so a sample at exactly the range
            start is not included
        gate: Supplies the robot-state gate
        gate_name: The rule's "while" clause

    Returns:
        The statistic, or None if the entry held no numeric samples the gate admits
    """
    values = [value for timestamp, value in zip(samples.timestamps, samples.values)
              if isinstance(value, (int, float)) and not isinstance(value, bool)
              and gate.applies(gate_name, timestamp)]
    if not values:
        return None
    if statistic == "peak":
        return max(values)
    if statistic == "min":
        return min(values)
    if statistic == "mean":
        return sum(values) / len(values)
    if statistic == "rise":
        return max(values) - values[0]
    return None


def compare_siblings(rule: Dict[str, Any], matched: List[str], log: Log,
                     gate: "EnabledGate", gate_name: str, last_timestamp: float,
                     log_file_name: str) -> List[CheckFinding]:
    """Flag an entry that stands apart from its peers within the same match.

    Peers share the match, so they share ambient temperature, how hard the robot
    was driven and how long it was enabled. Comparing among them cancels those
    confounders, which is why this resolves a smaller deviation than comparing an
    entry against its own history - and needs no history at all.

    The reference is the median of the *other* peers, so an outlier cannot pull
    its own baseline towards itself.
    """
    statistic = rule.get("statistic", "peak")
    deviation = rule.get("deviation") or {}
    above_by = deviation.get("aboveSiblingsBy")
    below_by = deviation.get("belowSiblingsBy")
    minimum_siblings = int(rule.get("minimumSiblings", 3))
    unit = rule.get("unit", "")
    suffix = f" {unit}" if unit else ""
    name = rule.get("name", rule.get("entry", ""))
    severity = rule.get("severity", "warning")
    pattern = EntryPattern(rule["entry"])

    def label(entry: str) -> str:
        captured = pattern.captures(entry)
        return "/".join(captured) if captured else entry.rsplit("/", 1)[-1]

    values: Dict[str, float] = {}
    for entry in matched:
        field_data = log.get_field(entry)
        samples = get_field_values(field_data, 0.0, last_timestamp) if field_data else None
        if samples is None:
            continue
        value = match_statistic(samples, statistic, gate, gate_name)
        if value is not None:
            values[entry] = value

    if len(values) < minimum_siblings:
        # Silence here would be indistinguishable from "all peers agree", which
        # is the trap absence checks exist to avoid.
        return [CheckFinding(
            name, severity, rule["entry"],
            f"only {len(values)} peer(s) with data; {minimum_siblings} needed to "
            f"compare", log_file_name)]

    findings = []
    for entry, value in sorted(values.items()):
        others = [other for key, other in values.items() if key != entry]
        reference = statistics.median(others)
        delta = value - reference
        peers = ", ".join(sorted(label(key) for key in values if key != entry))
        if above_by is not None and delta > float(above_by):
            findings.append(CheckFinding(
                name, severity, entry,
                f"{statistic} {value:g}{suffix} is {delta:.1f}{suffix} above its "
                f"peers (median {reference:g}{suffix} of {peers})", log_file_name))
        elif below_by is not None and -delta > float(below_by):
            findings.append(CheckFinding(
                name, severity, entry,
                f"{statistic} {value:g}{suffix} is {-delta:.1f}{suffix} below its "
                f"peers (median {reference:g}{suffix} of {peers})", log_file_name))
    return findings


def check_sample_findings(expectation: Any, value: Any) -> List[str]:
    """Return a detail string for each way this sample violates the expectation.

    Args:
        expectation: The rule's "expect" clause
        value: One logged sample

    Returns:
        Zero or more detail strings; empty means the sample is fine
    """
    if expectation == "empty":
        # Array entries such as AdvantageKit's Alerts: each element is its own
        # concern, so each becomes its own finding.
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return [] if not value else [str(value)]

    if isinstance(expectation, dict):
        if "always" in expectation and value != expectation["always"]:
            return [f"is {value!r}, expected {expectation['always']!r}"]
        if "never" in expectation and value == expectation["never"]:
            return [f"is {value!r}"]

    return []


def compute_checks(log: Log, log_file_name: str,
                   check_configs: List[Dict[str, Any]]) -> CheckReport:
    """Run every check rule against one log, without rendering anything.

    Args:
        log: The Log for one file
        log_file_name: Name of that file, carried on each finding
        check_configs: Rule dictionaries from the config

    Returns:
        A CheckReport holding the findings, de-duplicated per (entry, detail)
    """
    report = CheckReport()
    field_names = log.get_field_keys()
    gate = EnabledGate(log)
    last_timestamp = log.get_last_timestamp()

    for rule in check_configs:
        pattern_text = rule.get("entry")
        if not pattern_text:
            continue
        report.rules_run += 1

        name = rule.get("name", pattern_text)
        severity = rule.get("severity", "warning")
        expectation = rule.get("expect", "present")
        gate_name = rule.get("while", "any")

        pattern = EntryPattern(pattern_text)
        matched = pattern.expand(field_names)

        # A broad pattern can reach a subtree whose paths are ephemeral - NT
        # client entries carry a session id that changes on every reconnect, so
        # they are unbounded in number and useless as a per-device signal.
        excludes = rule.get("excludeEntry") or []
        if isinstance(excludes, str):
            excludes = [excludes]
        if excludes:
            excluded = [EntryPattern(text) for text in excludes]
            matched = [name for name in matched
                       if not any(e.matches(name) for e in excluded)]

        # A wildcard can only expand over entries that exist, so an entry that
        # never arrived produces no rule and no finding - silence exactly where a
        # concern belongs. "expectEntries" closes that gap by declaring the set
        # the rule expects to find, named by what the wildcard should have
        # matched (e.g. ["BCH", "BCL", "BL", "BR"]).
        expected_entries = rule.get("expectEntries") or []
        if expected_entries and pattern.has_wildcard:
            seen = {pattern.captures(entry) for entry in matched}
            seen.discard(None)
            for item in expected_entries:
                wanted = tuple(item) if isinstance(item, (list, tuple)) else (item,)
                if wanted not in seen:
                    report.findings.append(CheckFinding(
                        name, severity, pattern.substitute(wanted),
                        "entry not present in this log", log_file_name))

        if rule.get("compare") == "siblings":
            if matched:
                report.entries_checked += len(matched)
                report.findings.extend(compare_siblings(
                    rule, matched, log, gate, gate_name, last_timestamp,
                    log_file_name))
            continue

        # Absence for a rule that only asks for presence.
        if not matched:
            if expectation == "present" and not expected_entries:
                report.findings.append(CheckFinding(
                    name, severity, pattern_text,
                    "entry not present in this log", log_file_name))
            continue

        for entry in matched:
            report.entries_checked += 1
            field_data = log.get_field(entry)
            samples = get_field_values(field_data, 0.0, last_timestamp)
            if samples is None:
                continue

            if expectation == "present":
                continue  # presence already established by the match

            if isinstance(expectation, dict) and (
                    "above" in expectation or "below" in expectation):
                above = "above" in expectation
                enter = expectation["above"] if above else expectation["below"]
                clear = rule.get("clearBelow" if above else "clearAbove", enter)
                minimum = float(rule.get("minDuration", 0.0))

                excursions = find_excursions(
                    samples.timestamps, samples.values, float(enter), float(clear),
                    above, last_timestamp)
                # Clip durations to the gate rather than filtering samples, so a
                # spell that straddles a brief disable stays one spell.
                admitted = gate.windows(gate_name, last_timestamp)
                for excursion in excursions:
                    excursion.counted = overlap_seconds(
                        excursion.start, excursion.end, admitted)
                # Drop a spell the gate admits none of, but keep one whose
                # span is genuinely zero - a value that crosses on the final
                # sample means the robot finished past the limit, which matters
                # more in the pit than a long spell that recovered. minDuration
                # filters only when it was actually asked for.
                excursions = [e for e in excursions
                              if not (e.counted == 0 and e.span > 0)
                              and (minimum <= 0 or e.counted > minimum)]

                if excursions:
                    report.findings.append(CheckFinding(
                        name, severity, entry,
                        describe_excursions(excursions, float(enter), above,
                                            rule.get("unit", "")),
                        log_file_name,
                        timestamp=min(e.start for e in excursions),
                        occurrences=len(excursions)))
                continue

            # Collapse repeats: the same alert logged every cycle is one concern.
            seen: Dict[str, CheckFinding] = {}
            reached = False
            wanted = (expectation.get("atLeastOnce")
                      if isinstance(expectation, dict) else None)

            for timestamp, value in zip(samples.timestamps, samples.values):
                if not gate.applies(gate_name, timestamp):
                    continue
                if isinstance(expectation, dict) and "atLeastOnce" in expectation:
                    if value == wanted:
                        reached = True
                    continue
                for detail in check_sample_findings(expectation, value):
                    if detail in seen:
                        seen[detail].occurrences += 1
                    else:
                        seen[detail] = CheckFinding(
                            name, severity, entry, detail, log_file_name, timestamp)

            if isinstance(expectation, dict) and "atLeastOnce" in expectation:
                if not reached:
                    report.findings.append(CheckFinding(
                        name, severity, entry,
                        f"never reached {wanted!r}", log_file_name))
            else:
                report.findings.extend(seen.values())

    return report


def merge_check_findings(reports: List[CheckReport]) -> List[CheckFinding]:
    """Combine per-file findings into one cross-file list.

    Findings for the same rule, entry and detail are merged, keeping the earliest
    occurrence and summing counts, so a recurring concern reads as one line.
    """
    merged: Dict[Tuple[str, str, str], CheckFinding] = {}
    files_seen: Dict[Tuple[str, str, str], set] = {}

    for report in reports:
        for finding in report.findings:
            key = (finding.rule_name, finding.entry, finding.detail)
            files_seen.setdefault(key, set()).add(finding.log_file_name)
            if key not in merged:
                merged[key] = CheckFinding(**vars(finding))
            else:
                merged[key].occurrences += finding.occurrences
    for key, finding in merged.items():
        finding.file_count = len(files_seen[key])
    return sort_check_findings(list(merged.values()))


def sort_check_findings(findings: List[CheckFinding]) -> List[CheckFinding]:
    """Order findings so the most severe concerns are read first."""
    return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9),
                                           f.rule_name, f.entry, f.detail))


# === Text rendering ===========================================================

def format_value_locations(locations: List[ValueLocation], is_aggregate: bool) -> List[str]:
    """Render the locations under a calculation as text lines."""
    return [
        f"    @ {location.timestamp:.6f} s "
        f"{f'in {location.log_file_name}' if is_aggregate else ''}"
        for location in locations
    ]


def format_calculation(calc: CalculationResult, is_aggregate: bool) -> List[str]:
    """Render one computed calculation as text lines."""
    if calc.unknown_type:
        return [f"  Unknown calculation type: {calc.calc_type}"]

    if calc.calc_type in ('outlier_2std', 'abs_outlier_2std'):
        if calc.error:
            return [f"  {calc.name}: {calc.error}"]
        lines = []
        for value, locations in calc.outliers:
            lines.append(f"  {calc.name}: {value:.6f} {calc.unit}")
            lines.extend(format_value_locations(locations, is_aggregate))
        return lines

    if calc.calc_type == 'count':
        return [f"  {calc.name}: {calc.value}"]

    lines = [f"  {calc.name}: {calc.value:.6f} {calc.unit}"]
    lines.extend(format_value_locations(calc.locations, is_aggregate))
    return lines


def format_analysis(computed: AnalysisResult) -> List[str]:
    """Render a computed analysis as text lines."""
    if not computed.has_values:
        return ["No values found for this analysis"]

    if computed.is_aggregate:
        lines = [f"  Total values captured across all files: {computed.total_count}"]
        if VERBOSE:
            lines.append(f"  All values: {[f'{v:.6f} {computed.unit}' for v in computed.all_values]}")
    else:
        lines = [f"  Total values captured in this file: {computed.total_count}"]
        if VERBOSE:
            lines.append(f"  Values captured: {[f'{v:.6f} {computed.unit}' for v in computed.all_values]}")

    if not computed.has_numeric:
        lines.append("  No numeric values found for calculations")
        return lines

    for calc in computed.calculations:
        lines.extend(format_calculation(calc, computed.is_aggregate))
    return lines


def format_per_file_counts(computed: PerFileCounts) -> List[str]:
    """Render computed per-file counts as text lines."""
    lines = [f"  Files processed: {computed.files_processed}"]
    if not computed.show_detail:
        return lines
    lines.append(f"  Average matched values per file: {computed.average:.2f}")
    lines.append(f"  Minimum matched values in any file: {computed.min_count} in {computed.min_file}")
    lines.append(f"  Maximum matched values in any file: {computed.max_count} in {computed.max_file}")
    return lines


def format_check_findings(findings: List[CheckFinding], is_aggregate: bool) -> List[str]:
    """Render check findings as text lines, most severe first."""
    if not findings:
        return ["  No concerns found."]

    lines = []
    heading = None
    for finding in sort_check_findings(findings):
        # One heading per rule and entry; the details sit beneath it, so a rule
        # that fired twenty times reads as one concern rather than twenty.
        this_heading = (finding.severity, finding.rule_name, finding.entry)
        if this_heading != heading:
            heading = this_heading
            lines.append(f"  [{finding.severity.upper()}] {finding.rule_name} ({finding.entry})")
        lines.append(f"    {finding.detail}")

        parts = []
        if finding.occurrences > 1:
            parts.append(f"x{finding.occurrences}")
        if is_aggregate and finding.file_count > 1:
            parts.append(f"in {finding.file_count} files")
        if finding.timestamp is not None:
            where = f"first @ {finding.timestamp:.6f} s" if parts else f"@ {finding.timestamp:.6f} s"
            parts.append(where)
            if is_aggregate:
                parts.append(f"in {finding.log_file_name}")
        if parts:
            lines.append(f"      {', '.join(parts)}")
    return lines


def print_check_findings(findings: List[CheckFinding], is_aggregate: bool) -> None:
    """Compute-free rendering of check findings to stdout."""
    for line in format_check_findings(findings, is_aggregate):
        print(line)


# === Text output ==============================================================

def print_results_and_calculations(results: List[Tuple[str, List[Union[int, float, str, bool]], List[float]]],
                                   calculations: List[Dict[str, Any]],
                                   value_unit: str = "") -> None:
    """Compute an analysis and print it as text."""
    for line in format_analysis(compute_analysis(results, calculations, value_unit)):
        print(line)


def print_per_file_counts(results: List[Tuple[str, List[Union[int, float, str, bool]], List[float]]],
                          calculations: List[Dict[str, Any]]) -> None:
    """Compute per-file match counts and print them as text."""
    for line in format_per_file_counts(compute_per_file_counts(results, calculations)):
        print(line)


def get_field_values(field, start: float, end: float):
    """Read a field's values over a time range, whatever its logged type.

    Args:
        field: The LogField to read
        start: Range start (exclusive)
        end: Range end (inclusive)

    Returns:
        The field's LogValueSet over the range, or None if its type is one the
        analyzers cannot compare against a configured value.
    """
    getters = {
        LoggableType.STRING: field.get_string,
        LoggableType.BOOLEAN: field.get_boolean,
        LoggableType.NUMBER: field.get_number,
        LoggableType.BOOLEAN_ARRAY: field.get_boolean_array,
        LoggableType.NUMBER_ARRAY: field.get_number_array,
        LoggableType.STRING_ARRAY: field.get_string_array,
    }
    getter = getters.get(field.get_type())
    return getter(start, end) if getter else None

# Log names normally carry their own start time. AdvantageKit writes
# "akit_26-05-01_13-38-09_johnson_q67.wpilog"; WPILib's DataLogManager writes
# "FRC_20260501_133809.wpilog". Reading the name is more trustworthy than the
# file's mtime, which reflects when it was copied rather than when it was
# recorded, and which plain scp resets.
LOG_NAME_TIMESTAMPS = (
    re.compile(r"(?P<year>\d{2})-(?P<month>\d{2})-(?P<day>\d{2})_"
               r"(?P<hour>\d{2})-(?P<minute>\d{2})-(?P<second>\d{2})"),
    re.compile(r"(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})_"
               r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"),
)


def log_recorded_at(path: str) -> datetime:
    """When a log was recorded, from its name if possible, else its mtime.

    Args:
        path: Path to a .wpilog file

    Returns:
        A datetime usable as a sort key. Names without a recognisable timestamp
        fall back to the file's modification time, which for a freshly synced
        log is when it was copied - still the right order in the pit, just less
        trustworthy if old logs are re-copied.
    """
    name = os.path.basename(path)
    for pattern in LOG_NAME_TIMESTAMPS:
        found = pattern.search(name)
        if not found:
            continue
        parts = {key: int(value) for key, value in found.groupdict().items()}
        if parts["year"] < 100:
            parts["year"] += 2000
        try:
            return datetime(parts["year"], parts["month"], parts["day"],
                            parts["hour"], parts["minute"], parts["second"])
        except ValueError:
            continue  # matched digits that are not a real date
    try:
        return datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return datetime.min


def log_contains_match(log_file_path: str) -> bool:
    """Whether the robot was ever FMS-attached in this log, i.e. it is a match.

    The robot logs whenever it is powered, so a synced folder holds pit sessions
    alongside matches. Nothing in the file name reliably distinguishes them, and
    neither does whether the robot was enabled or for how long - a pit session
    can be enabled far longer than a match. FMS attachment is the clean signal.

    Reading stops at the first attached record, so a match costs almost nothing
    (the flag turns true within the first minute of the log). Proving the
    negative needs the whole file, which is the price of skipping it.
    """
    try:
        with open(log_file_path, "rb") as handle:
            buffer = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            reader = DataLogReader(buffer)
            if not reader:
                return False
            fms_entries = set()
            for record in reader:
                if record.entry == 0:
                    if record.isStart():
                        data = record.getStartData()
                        if (data.name == "/DriverStation/FMSAttached"
                                and data.type == "boolean"):
                            fms_entries.add(data.entry)
                    elif record.isFinish():
                        fms_entries.discard(record.getFinishEntry())
                    continue
                if record.entry in fms_entries and record.getBoolean():
                    return True
    except (OSError, ValueError, TypeError):
        return False
    return False


def partition_match_logs(log_files: List[str]) -> Tuple[List[str], List[str]]:
    """Split paths into (match logs, everything else), preserving order."""
    matches, others = [], []
    for path in log_files:
        (matches if log_contains_match(path) else others).append(path)
    return matches, others


@dataclass
class LatestLog:
    """The newest finished log, and any newer ones still being written.

    The names of the skipped logs are reported rather than interpreted. An
    AdvantageKit name usually carries the event and match number
    ("akit_26-05-01_22-45-20_johnson_q121"), so seeing it tells the reader
    whether the robot is still logging a match or just sitting in the pit -
    a judgement the tool has no sound way to make on its own.
    """
    path: Optional[str] = None
    still_writing: List[str] = field(default_factory=list)


def select_latest_log(log_files: List[str],
                      settle_seconds: float = 0.0) -> LatestLog:
    """Return the most recently recorded log that is not still being written.

    The robot logs continuously while powered, so once it is back in the pit and
    switched on it has already opened a *new* log. The match that needs checking
    is therefore the newest **finished** log, not the newest one.

    Args:
        log_files: Candidate paths
        settle_seconds: Gap over which to watch for a file growing; 0 skips the
            check, which is right when the folder is sync_logs.py's output (it
            renames into place, so every file there is complete)

    Returns:
        A LatestLog whose path is None if every candidate is still growing. Ties
        break on the file name so the choice is deterministic.
    """
    ordered = sorted(log_files,
                     key=lambda path: (log_recorded_at(path),
                                       os.path.basename(path)),
                     reverse=True)
    if not ordered:
        return LatestLog()
    if settle_seconds <= 0:
        return LatestLog(path=ordered[0])

    def size_of(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return -1

    # One sleep for all candidates, then take the newest that did not change.
    before = {path: size_of(path) for path in ordered}
    time.sleep(settle_seconds)
    growing = []
    for path in ordered:
        if size_of(path) == before[path]:
            return LatestLog(path=path, still_writing=growing)
        growing.append(path)
    return LatestLog(still_writing=growing)


TIME_ANALYSIS_ROLES = ("startEntry", "endEntry")
VALUE_ANALYSIS_ROLES = ("entry", "triggerEntry")


def expand_analyses(configs: List[Dict[str, Any]], roles: Tuple[str, ...],
                    field_names: List[str]) -> List[Tuple[Tuple, Dict[str, Any]]]:
    """Expand each configured analysis into concrete analyses.

    An entry name containing no wildcard is a literal and yields exactly one
    expansion, so configs without patterns behave precisely as before. A pattern
    yields one expansion per matched entry, paired across roles by what the
    wildcards matched.

    Args:
        configs: The analysis configs as written
        roles: The keys naming entries in this kind of analysis
        field_names: Concrete field names available in this log

    Returns:
        List of (key, concrete config). The key embeds the config's position and
        its concrete entry names, so it is stable across log files and can be
        used to aggregate them.
    """
    expanded = []
    for index, analysis in enumerate(configs):
        role_values = {role: analysis.get(role) for role in roles}
        for expansion in expand_roles(role_values, field_names):
            concrete = dict(analysis)
            concrete.update(expansion)
            expanded.append(((index,) + tuple(concrete.get(r) for r in roles), concrete))
    return expanded


def analyze_file_records(log: Log, log_file_name: str, time_analysis_configs: List[Dict[str, Any]]) -> List[Tuple[Tuple, Dict[str, Any], Tuple[str, List[float], List[float]]]]:
    """
    Analyze file records and return time differences and start timestamps for each analysis configuration.

    Args:
        log: The Log object containing the analyzed data
        log_file_name: Name of the log file being analyzed
        time_analysis_configs: List of analysis configuration dictionaries

    Returns:
        List of (key, concrete config, (log file name, time differences, start
        timestamps)). A config naming entries with wildcards contributes one
        entry per matched entry; the key is stable across log files.
    """
    all_analysis_results = {}
    all_analysis_configs = {}

    for key, analysis in expand_analyses(time_analysis_configs, TIME_ANALYSIS_ROLES,
                                         log.get_field_keys()):
        analysis_idx = key[0]
        all_analysis_configs[key] = analysis
        start_entry = analysis.get('startEntry')
        start_value = analysis.get('startValue')
        end_entry = analysis.get('endEntry')
        end_value = analysis.get('endValue')
        calculations = analysis.get('calculations', [])
        
        if not all([start_entry, end_entry, calculations]):
            all_analysis_results[key] = (log_file_name, [], [])
            continue
        
        # Find time differences between start and end events
        time_differences = []
        start_timestamps = []

        # Get the field and timestamps for the start entry
        start_field = log.get_field(start_entry)
        end_field = log.get_field(end_entry)

        if not start_field or not end_field:
            print(f"  Skipping analysis {analysis_idx} due to missing fields: {start_entry} or {end_entry}")
            all_analysis_results[key] = (log_file_name, [], [])
            continue

        start_log_values = get_field_values(start_field, 0.0, log.get_last_timestamp())
        if start_log_values is None:
            print(f"  Skipping analysis {analysis_idx} due to unsupported type for: {start_entry} of {start_field.get_type()}")
            all_analysis_results[key] = (log_file_name, [], [])
            continue
            
        start_timestamp = 0.0

        for i, timestamp in enumerate(start_log_values.timestamps):
            if start_log_values.values[i] == start_value:
                start_timestamp = timestamp
                next_timestamp = log.get_last_timestamp()
                for j, next_ts in enumerate(start_log_values.timestamps):
                    if next_ts > start_timestamp and start_log_values.values[j] == start_value:
                        next_timestamp = next_ts
                        break

                end_log_values = get_field_values(end_field, start_timestamp, next_timestamp)
                if end_log_values is None:
                    print(f"  Skipping analysis {analysis_idx} due to unsupported type for: {end_entry} of {end_field.get_type()}")
                    all_analysis_results[key] = (log_file_name, [], [])
                    continue

                for k, end_timestamp in enumerate(end_log_values.timestamps):
                    if end_log_values.values[k] == end_value:
                        time_diff = end_timestamp - start_timestamp
                        time_differences.append(time_diff)
                        start_timestamps.append(start_timestamp)
                        break
        
        all_analysis_results[key] = (log_file_name, time_differences, start_timestamps)

    return [(key, all_analysis_configs[key], result)
            for key, result in all_analysis_results.items()]

def analyze_value_records(log: Log, log_file_name: str, value_analysis_configs: List[Dict[str, Any]]) -> List[Tuple[Tuple, Dict[str, Any], Tuple[str, List[Union[int, float, str, bool]], List[float]]]]:
    """
    Analyze file records and return captured values and timestamps for each value analysis configuration.
    
    Args:
        log: the log to analyze for values
        value_analysis_configs: List of value analysis configuration dictionaries
        
    Returns:
        List of (key, concrete config, (log file name, captured values,
        timestamps)). A config naming entries with wildcards contributes one
        entry per matched entry; the key is stable across log files.
    """
    all_value_results = {}
    all_value_configs = {}

    for key, analysis in expand_analyses(value_analysis_configs, VALUE_ANALYSIS_ROLES,
                                         log.get_field_keys()):
        analysis_idx = key[0]
        all_value_configs[key] = analysis
        entry_name = analysis.get('entry')
        trigger_entry = analysis.get('triggerEntry')
        trigger_value = analysis.get('triggerValue')
        calculations = analysis.get('calculations', [])
        
        if not all([entry_name, trigger_entry, calculations]) or trigger_value is None:
            all_value_results[key] = (log_file_name, [], [])
            continue
        
        # Find values when trigger condition is met
        captured_values = []
        end_timestamps = []

        # Get the field and timestamps for the start entry
        trigger_field = log.get_field(trigger_entry)
        field = log.get_field(entry_name)

        if not trigger_field or not field:
            print(f"  Skipping analysis {analysis_idx} due to missing fields: {trigger_entry} or {entry_name}")
            all_value_results[key] = (log_file_name, [], [])
            continue

        trigger_log_values = get_field_values(trigger_field, 0.0, log.get_last_timestamp())
        if trigger_log_values is None:
            print(f"  Skipping analysis {analysis_idx} due to unsupported type for: {trigger_entry} of {trigger_field.get_type()}")
            all_value_results[key] = (log_file_name, [], [])
            continue
            
        start_timestamp = 0.0

        for i, timestamp in enumerate(trigger_log_values.timestamps):
            if trigger_log_values.values[i] == trigger_value:
                end_timestamp = timestamp
                
                log_values = get_field_values(field, start_timestamp, end_timestamp)
                if log_values is None:
                    print(f"  Skipping analysis {analysis_idx} due to unsupported type for: {entry_name} of {field.get_type()}")
                    all_value_results[key] = (log_file_name, [], [])
                    continue

                if len(log_values.values) > 0:
                    captured_values.append(log_values.values[-1])
                    end_timestamps.append(end_timestamp)
                start_timestamp = timestamp  # Update start timestamp for next trigger match
        
        all_value_results[key] = (log_file_name, captured_values, end_timestamps)

    return [(key, all_value_configs[key], result)
            for key, result in all_value_results.items()]

def process_log_file(log_file_path: str, mandatory_entries: Set[str], target_entry_names: Set[str], 
                     filter_enabled: bool = False, filter_fms_attached: bool = False, robot_mode: str = 'both') -> Log:
    """
    Process a single log file and return captured records and final driver station state.
    Args:
        log_file_path: Path to the log file to process
        mandatory_entries: Set of mandatory entry names to always capture
        target_entry_names: Set of target entry names to capture based on filtering configuration
        filter_enabled: Whether to filter records based on driver station enabled state
        filter_fms_attached: Whether to filter records based on FMS attached state
        robot_mode: Robot mode filter ('auto', 'teleop', or 'both')
        Returns: Log object containing captured records and final driver station state
    """

    def should_capture_record(driver_station_enabled: Optional[bool], driver_station_autonomous: Optional[bool], 
                            driver_station_fms_attached: Optional[bool]) -> bool:
        """Check if records should be captured based on current DriverStation state.
        Args:
            driver_station_enabled: Current enabled state of the DriverStation
            driver_station_autonomous: Current autonomous state of the DriverStation
            driver_station_fms_attached: Current FMS attached state of the DriverStation
        Returns:
            bool: True if the record should be captured, False otherwise.
        """
        # Check enabled filter
        if filter_enabled and driver_station_enabled is not None and not driver_station_enabled:
            return False
        
        # Check FMS attached filter
        if filter_fms_attached and driver_station_fms_attached is not None and not driver_station_fms_attached:
            return False
        
        # Check robot mode filter
        if robot_mode == "auto" and driver_station_autonomous is not None and not driver_station_autonomous:
            return False
        elif robot_mode == "teleop" and driver_station_autonomous is not None and driver_station_autonomous:
            return False
        # If robot_mode is "both" or any condition is not set, allow capture
        
        return True


    print(f"\nProcessing: {os.path.basename(log_file_path)}")
    
    with open(log_file_path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        reader = DataLogReader(mm)
        if not reader:
            print(f"  Warning: {os.path.basename(log_file_path)} is not a valid log file")
            return Log()

        entries = {}
        log = Log()

        # Whether each entry name is mandatory / targeted / a struct schema depends
        # only on the name, which is fixed when the entry is declared. Deciding it
        # once per entry rather than once per record avoids re-running the substring
        # scans over the configured names for every one of millions of data records.
        entry_flags = {}

        # Patterns are compiled once per file, then tested once per entry (not
        # once per record), which keeps matching off the hot path.
        mandatory_patterns = [EntryPattern(name) for name in mandatory_entries]
        target_patterns = [EntryPattern(name) for name in target_entry_names]

        def flags_for(name: str) -> Tuple[bool, bool, bool]:
            """Return (is_mandatory, is_target, is_schema) for an entry name."""
            return (any(pattern.could_contain(name) for pattern in mandatory_patterns),
                    any(pattern.could_contain(name) for pattern in target_patterns),
                    ".schema" in name)

        # Track most recent values of DriverStation entries for filtering
        driver_station_enabled = None
        driver_station_autonomous = None
        driver_station_fms_attached = None

        for record in reader:
            # Control records all have entry 0; testing that once keeps the far more
            # common data-record path down to a single comparison.
            if record.entry == 0:
                if record.isStart():
                    try:
                        data = record.getStartData()
                        if data.entry in entries:
                            print("...DUPLICATE entry ID, overriding")

                        entries[data.entry] = data
                        entry_flags[data.entry] = flags_for(data.name)

                    except TypeError:
                        print("Start(INVALID)")

                elif record.isFinish():
                    try:
                        entry = record.getFinishEntry()
                        if entry not in entries:
                            print("...ID not found")
                        else:
                            del entries[entry]
                            entry_flags.pop(entry, None)
                    except TypeError:
                        print("Finish(INVALID)")
                elif record.isSetMetadata():
                    try:
                        data = record.getSetMetadataData()
                        if data.entry not in entries:
                            print("...ID not found")
                    except TypeError:
                        print("SetMetadata(INVALID)")
                else:
                    print("Unrecognized control record")
            else:
                entry = entries.get(record.entry)
                if entry is None:
                    continue

                is_mandatory, is_target, is_schema = entry_flags[record.entry]

                # Update DriverStation state tracking for filtering
                try:
                    if entry.name == "/DriverStation/Enabled" and entry.type == "boolean":
                        driver_station_enabled = record.getBoolean()
                    elif entry.name == "/DriverStation/Autonomous" and entry.type == "boolean":
                        driver_station_autonomous = record.getBoolean()
                    elif entry.name == "/DriverStation/FMSAttached" and entry.type == "boolean":
                        driver_station_fms_attached = record.getBoolean()
                except TypeError:
                    # If we can't read the value, continue without updating state
                    pass

                if is_schema:
                    # If the entry is a schema entry, we may want to capture it differently
                    log.struct_decoder.add_schema(entry.name.split("struct:")[1], record.getBytes())

                # Check if this record matches any target entry names and meets filtering criteria
                if is_mandatory or (is_target and should_capture_record(driver_station_enabled, driver_station_autonomous, driver_station_fms_attached)):
                    timestamp = record.timestamp / 1000000
                    key = entry.name
                    type_str = entry.type
                    
                    if type_str == "boolean":
                        log.put_boolean(key, timestamp, record.getBoolean())
                    elif type_str in ("int", "int64"):
                        log.put_number(key, timestamp, record.getInteger())
                    elif type_str == "float":
                        log.put_number(key, timestamp, record.getFloat())
                    elif type_str == "double":
                        log.put_number(key, timestamp, record.getDouble())
                    elif type_str == "string":
                        log.put_string(key, timestamp, record.getString())
                    elif type_str == "boolean[]":
                        log.put_boolean_array(key, timestamp, record.getBooleanArray())
                    elif type_str in ("int[]", "int64[]"):
                        log.put_number_array(key, timestamp, record.getIntegerArray())
                    elif type_str == "float[]":
                        log.put_number_array(key, timestamp, record.getFloatArray())
                    elif type_str == "double[]":
                        log.put_number_array(key, timestamp, record.getDoubleArray())
                    elif type_str == "string[]":
                        log.put_string_array(key, timestamp, record.getStringArray())
                    elif type_str == "json":
                        log.put_json(key, timestamp, record.getString())
                    elif type_str == "msgpack":
                        log.put_msgpack(key, timestamp, record.data)  # getRaw() equivalent
                    else:  # Default to raw
                        if type_str.startswith(STRUCT_PREFIX):
                            schema_type = type_str.split(STRUCT_PREFIX)[1]
                            if schema_type.endswith("[]"):
                                log.put_struct(key, timestamp, record.data, schema_type[:-2], True)
                            else:
                                log.put_struct(key, timestamp, record.data, schema_type, False)
                        else:
                            log.put_raw(key, timestamp, record.data)
                            # Note: CustomSchemas functionality not implemented in Python version
                    
        return log

def main() -> None:
    """Main analysis function."""
    global VERBOSE

    parser = argparse.ArgumentParser(
        description="Analyze a folder of WPILib .wpilog files.",
        usage="analysis.py <log_folder> <config_json_file> [options]")
    parser.add_argument("log_folder", help="directory containing .wpilog files")
    parser.add_argument("config_json_file", help="JSON configuration file")
    parser.add_argument("--html", metavar="PATH",
                        help="also write a self-contained HTML report here")
    parser.add_argument("--json", metavar="PATH", dest="json_path",
                        help="also write the whole run as JSON here")
    parser.add_argument("--refresh", type=int, default=30, metavar="SECONDS",
                        help="HTML auto-refresh interval, 0 to disable "
                             "(default 30)")
    parser.add_argument("--matches-only", action="store_true",
                        help="skip logs the robot recorded outside a match "
                             "(pit sessions), which otherwise dilute every "
                             "per-file average")
    parser.add_argument("--latest", action="store_true",
                        help="analyze only the most recent finished log, so a "
                             "page left open always shows the newest match")
    parser.add_argument("--settle-seconds", type=float, default=1.0,
                        metavar="SECONDS",
                        help="with --latest, gap over which to check that the "
                             "chosen log is not still growing; 0 skips the check "
                             "(default 1)")
    parser.add_argument("--verbose", action="store_true",
                        help="include captured values and a record summary")
    args = parser.parse_args()

    VERBOSE = args.verbose

    log_folder = args.log_folder
    if not os.path.isdir(log_folder):
        print(f"Error: {log_folder} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Load configuration from JSON file
    try:
        with open(args.config_json_file, 'r') as config_file:
            config = json.load(config_file)
            
            # Load filtering criteria
            filter_on_enabled = config.get('enabled', False)
            filter_on_fms_attached = config.get('fmsAttached', False)
            filter_on_robot_mode = config.get('robotMode', 'both')  # 'auto', 'teleop', or 'both'
            
            # Load analysis configurations
            time_analysis_configs = config.get('timeAnalysis', [])
            value_analysis_configs = config.get('valueAnalysis', [])
            check_configs = config.get('checks', [])
            
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading config file: {e}", file=sys.stderr)
        sys.exit(1)
    
    target_entry_names = set([])

    # Always capture these entry names regardless of JSON configuration
    mandatory_entries = {"/DriverStation/Enabled", "/DriverStation/Autonomous", "/DriverStation/FMSAttached"}
    target_entry_names.update(mandatory_entries)
    
    # Add analysis entries to target entries to ensure they're captured
    for analysis in time_analysis_configs:
        start_entry = analysis.get('startEntry')
        end_entry = analysis.get('endEntry')
        if start_entry:
            target_entry_names.add(start_entry)
        if end_entry:
            target_entry_names.add(end_entry)
    
    # Add value analysis entries to target entries
    for analysis in value_analysis_configs:
        entry = analysis.get('entry')
        trigger_entry = analysis.get('triggerEntry')
        if entry:
            target_entry_names.add(entry)
        if trigger_entry:
            target_entry_names.add(trigger_entry)

    # Check rules name entries too, so they must be captured
    for rule in check_configs:
        if rule.get('entry'):
            target_entry_names.add(rule['entry'])

    # Get list of files to process
    log_files = []
    for filename in os.listdir(log_folder):
        if filename.endswith('.wpilog'):
            log_files.append(os.path.join(log_folder, filename))
    
    if not log_files:
        print(f"No log files found in {log_folder}", file=sys.stderr)
        sys.exit(1)

    selection = f"{len(log_files)} log files"
    still_writing = []
    non_matches = []
    if args.matches_only:
        log_files, skipped = partition_match_logs(sorted(log_files))
        non_matches = [os.path.basename(path) for path in skipped]
        if not log_files:
            for name in non_matches:
                print(f"Not a match (no FMS connection): {name}", file=sys.stderr)
            print("No match logs found; nothing to analyze.", file=sys.stderr)
            sys.exit(0)
        selection = f"{len(log_files)} match logs"

    if args.latest:
        latest = select_latest_log(log_files, settle_seconds=args.settle_seconds)
        still_writing = [os.path.basename(path) for path in latest.still_writing]
        if latest.path is None:
            for name in still_writing:
                print(f"Still being written: {name}", file=sys.stderr)
            print("Every log is still being written; nothing finished to analyze.",
                  file=sys.stderr)
            sys.exit(0)
        log_files = [latest.path]
        selection = f"most recent finished match: {os.path.basename(latest.path)}"
    
     # Print filtering criteria and final states
    print(f"\n=== FILTERING CRITERIA ===")
    print(f"Filter for enabled: {filter_on_enabled}")
    print(f"Filter for FMS attached: {filter_on_fms_attached}")
    print(f"Filter for robot mode: {filter_on_robot_mode}")

    print(f"\n=== ANALYSIS ===")
    for name in non_matches:
        print(f"Not a match, skipped: {name}")
    if args.latest:
        # Name the newer logs rather than guess at what they are: the reader can
        # tell a match from an idle pit session at a glance from the name.
        for name in still_writing:
            print(f"Newer log still being written: {name}")
        print(f"Analyzing the most recent finished match only:")
    else:
        print(f"Found {len(log_files)} log files to process:")
    for log_file in sorted(log_files):
        print(f"  {os.path.basename(log_file)}")

    # Aggregated data across all files, keyed by expansion (a config that names
    # entries with wildcards contributes one key per matched entry).
    all_logs = []  # List to store records from all files
    processed_files = []  # Base names, in processing order
    run_report = Report(
        log_folder=log_folder, config_path=args.config_json_file,
        filters={"enabled": filter_on_enabled,
                 "fmsAttached": filter_on_fms_attached,
                 "robotMode": filter_on_robot_mode},
        selection=selection,
        still_writing=still_writing,
        non_matches=non_matches,
        generated_at=now_text())
    check_reports = []  # One CheckReport per file
    aggregated_time_analysis_results = {}  # key -> {log file name: result}
    aggregated_value_analysis_results = {}  # key -> {log file name: result}
    time_analysis_expansions = {}  # key -> concrete config, in first-seen order
    value_analysis_expansions = {}  # key -> concrete config, in first-seen order

    # Process all log files
    for log_file in sorted(log_files):
        log = process_log_file(log_file, mandatory_entries, target_entry_names, 
                               filter_on_enabled, filter_on_fms_attached, filter_on_robot_mode)
        all_logs.append(log)
        processed_files.append(os.path.basename(log_file))
        file_report = FileReport(name=os.path.basename(log_file))
        run_report.files.append(file_report)

        # Run the check rules for this file
        if check_configs:
            report = compute_checks(log, os.path.basename(log_file), check_configs)
            check_reports.append(report)
            file_report.findings = report.findings
            print(f"\n=== CHECK RESULTS FOR {os.path.basename(log_file)} ===\n")
            print_check_findings(report.findings, is_aggregate=False)

        # Analyze time records and aggregate for later cross-file analysis
        if time_analysis_configs:
            time_analysis_results = analyze_file_records(log, os.path.basename(log_file), time_analysis_configs)

            # Aggregate results for later cross-file analysis (even empty results)
            for key, concrete, result in time_analysis_results:
                time_analysis_expansions.setdefault(key, concrete)
                aggregated_time_analysis_results.setdefault(key, {})[result[0]] = result

        # Analyze value records and aggregate for later cross-file analysis  
        if value_analysis_configs:
            value_analysis_results = analyze_value_records(log, os.path.basename(log_file), value_analysis_configs)
            
            # Aggregate results for later cross-file analysis (even empty results)
            for key, concrete, result in value_analysis_results:
                value_analysis_expansions.setdefault(key, concrete)
                aggregated_value_analysis_results.setdefault(key, {})[result[0]] = result

        # Perform cycle time analysis calculations on individual file data
        if time_analysis_configs:
            print(f"\n=== TIME ANALYSIS RESULTS FOR {os.path.basename(log_file)} ===")
            
            for _key, analysis, results in time_analysis_results:
                start_entry = analysis.get('startEntry')
                start_value = analysis.get('startValue')
                end_entry = analysis.get('endEntry')
                end_value = analysis.get('endValue')
                calculations = analysis.get('calculations', [])

                if not all([start_entry, end_entry, calculations]):
                    print(f"Skipping incomplete analysis configuration")
                    continue

                title = f"{start_entry} ({start_value}) -> {end_entry} ({end_value})"
                print(f"\nAnalyzing: {title}")

                # Computed once, then rendered; the emitters consume the same object.
                computed = compute_analysis([results], calculations, "s")
                file_report.sections.append(
                    ReportSection(kind="time", title=title, unit="s", result=computed))
                for line in format_analysis(computed):
                    print(line)

        # Perform value analysis calculations on individual file data
        if value_analysis_configs:
            print(f"\n=== VALUE ANALYSIS RESULTS FOR {os.path.basename(log_file)} ===")
            
            for _key, analysis, results in value_analysis_results:
                entry_name = analysis.get('entry')
                entry_unit = analysis.get('entryUnit', "")
                trigger_entry = analysis.get('triggerEntry')
                trigger_value = analysis.get('triggerValue')
                calculations = analysis.get('calculations', [])

                if not all([entry_name, trigger_entry, calculations]) or trigger_value is None:
                    print(f"Skipping incomplete value analysis configuration")
                    continue

                title = f"{entry_name} when {trigger_entry} = {trigger_value}"
                print(f"\nAnalyzing: {title}")

                computed = compute_analysis([results], calculations, entry_unit)
                file_report.sections.append(
                    ReportSection(kind="value", title=title, unit=entry_unit,
                                  result=computed))
                for line in format_analysis(computed):
                    print(line)

    # Aggregate the checks across all files
    if check_configs and check_reports:
        print(f"\n=== AGGREGATED CHECK RESULTS ACROSS ALL FILES ===\n")
        run_report.aggregate_findings = merge_check_findings(check_reports)
        print_check_findings(run_report.aggregate_findings, is_aggregate=True)

    # Perform aggregated analysis across all files
    if time_analysis_configs and aggregated_time_analysis_results:
        print(f"\n=== AGGREGATED TIME ANALYSIS RESULTS ACROSS ALL FILES ===")
        
        # Sorted so expansion order does not depend on which file happened to
        # contain which entry first. Names may be None for an incomplete config,
        # so compare them as strings.
        for key, analysis in sorted(time_analysis_expansions.items(),
                                    key=lambda item: (item[0][0],
                                                      tuple(str(part) for part in item[0][1:]))):
            start_entry = analysis.get('startEntry')
            start_value = analysis.get('startValue')
            end_entry = analysis.get('endEntry')
            end_value = analysis.get('endValue')
            calculations = analysis.get('calculations', [])

            if not all([start_entry, end_entry, calculations]):
                print(f"Skipping incomplete analysis configuration")
                continue

            print(f"\nAggregated Analysis: {start_entry} ({start_value}) -> {end_entry} ({end_value})")

            # Every processed file is represented, so per-file counts stay
            # meaningful when a wildcard matched in some files but not others.
            by_file = aggregated_time_analysis_results.get(key, {})
            all_results_by_file = [by_file.get(name, (name, [], []))
                                   for name in processed_files]
            
            if all_results_by_file:
                counts = compute_per_file_counts(all_results_by_file, calculations)
                computed = compute_analysis(all_results_by_file, calculations, "s")
                run_report.aggregate_sections.append(
                    ReportSection(kind="time",
                                  title=f"{start_entry} ({start_value}) -> "
                                        f"{end_entry} ({end_value})",
                                  unit="s", result=computed, counts=counts))
                for line in format_per_file_counts(counts):
                    print(line)
                for line in format_analysis(computed):
                    print(line)
            else:
                print(f"  No complete cycles found for this analysis across all files")

    # Perform aggregated value analysis across all files
    if value_analysis_configs and aggregated_value_analysis_results:
        print(f"\n=== AGGREGATED VALUE ANALYSIS RESULTS ACROSS ALL FILES ===")
        
        for key, analysis in sorted(value_analysis_expansions.items(),
                                    key=lambda item: (item[0][0],
                                                      tuple(str(part) for part in item[0][1:]))):
            entry_name = analysis.get('entry')
            entry_unit = analysis.get('entryUnit', "")
            trigger_entry = analysis.get('triggerEntry')
            trigger_value = analysis.get('triggerValue')
            calculations = analysis.get('calculations', [])

            if not all([entry_name, trigger_entry, calculations]) or trigger_value is None:
                print(f"Skipping incomplete value analysis configuration")
                continue

            print(f"\nAggregated Value Analysis: {entry_name} when {trigger_entry} = {trigger_value}")

            by_file = aggregated_value_analysis_results.get(key, {})
            all_values_by_file = [by_file.get(name, (name, [], []))
                                  for name in processed_files]
            
            if all_values_by_file:
                counts = compute_per_file_counts(all_values_by_file, calculations)
                computed = compute_analysis(all_values_by_file, calculations, entry_unit)
                run_report.aggregate_sections.append(
                    ReportSection(kind="value",
                                  title=f"{entry_name} when {trigger_entry} = "
                                        f"{trigger_value}",
                                  unit=entry_unit, result=computed, counts=counts))
                for line in format_per_file_counts(counts):
                    print(line)
                for line in format_analysis(computed):
                    print(line)
                    
            else:
                print(f"  No values captured for this analysis across all files")

    # Emit the non-terminal outputs, from the same computed objects the text
    # rendering used.
    if args.html:
        path = Path(args.html)
        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_html(run_report, refresh_seconds=args.refresh),
                        encoding="utf-8")
        print(f"\nWrote HTML report to {path}")

    if args.json_path:
        path = Path(args.json_path)
        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_json(run_report), encoding="utf-8")
        print(f"Wrote JSON report to {path}")

    # Print summary of captured records
    if(VERBOSE):
        print(f"\n=== CAPTURED RECORDS SUMMARY ===")
        print(f"Total captured logs: {len(all_logs)}")
        print(f"Target entry names: {sorted(target_entry_names)}")
        print(f"Mandatory entries (always captured): {sorted(mandatory_entries)}")
        config_only_entries = target_entry_names - mandatory_entries
        if config_only_entries:
            print(f"Additional entries from JSON config: {sorted(config_only_entries)}")
        else:
            print("Additional entries from JSON config: None")
    
    if(VERBOSE):
        if all_logs:
            print(f"\nCaptured logs by entry name:")
            entry_counts = {}
            for log in all_logs:
                for key in log.get_field_keys():
                    if key not in entry_counts:
                        entry_counts[key] = 0
                    entry_counts[key] += len(log.get_field(key).get_timestamps())

            for entry_name in sorted(entry_counts.keys()):
                print(f"  {entry_name}: {entry_counts[entry_name]} records")
        else:
            print("No records captured matching the specified entry names.")

if __name__ == "__main__":
    main()