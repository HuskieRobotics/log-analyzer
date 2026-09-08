#! /usr/bin/env python3

import json
import mmap
import os
import sys
import bisect
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any, Union
from datalog import DataLogReader
from Log import Log, LoggableType
from entry_patterns import EntryPattern, expand_roles

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

    def is_enabled_at(self, timestamp: float) -> bool:
        """The most recent Enabled value at or before this timestamp."""
        if not self.timestamps:
            return False
        index = bisect.bisect_right(self.timestamps, timestamp) - 1
        return bool(self.values[index]) if index >= 0 else False

    def applies(self, gate: str, timestamp: float) -> bool:
        """Whether a sample passes the rule's "while" gate."""
        if gate == "enabled":
            return self.is_enabled_at(timestamp)
        if gate == "disabled":
            return not self.is_enabled_at(timestamp)
        return True


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
    if len(sys.argv) != 3:
        print("Usage: analysis.py <log_folder> <config_json_file>", file=sys.stderr)
        sys.exit(1)

    log_folder = sys.argv[1]
    if not os.path.isdir(log_folder):
        print(f"Error: {log_folder} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Load configuration from JSON file
    try:
        with open(sys.argv[2], 'r') as config_file:
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
    
     # Print filtering criteria and final states
    print(f"\n=== FILTERING CRITERIA ===")
    print(f"Filter for enabled: {filter_on_enabled}")
    print(f"Filter for FMS attached: {filter_on_fms_attached}")
    print(f"Filter for robot mode: {filter_on_robot_mode}")

    print(f"\n=== ANALYSIS ===")
    print(f"Found {len(log_files)} log files to process:")
    for log_file in sorted(log_files):
        print(f"  {os.path.basename(log_file)}")

    # Aggregated data across all files, keyed by expansion (a config that names
    # entries with wildcards contributes one key per matched entry).
    all_logs = []  # List to store records from all files
    processed_files = []  # Base names, in processing order
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

        # Run the check rules for this file
        if check_configs:
            report = compute_checks(log, os.path.basename(log_file), check_configs)
            check_reports.append(report)
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

                print(f"\nAnalyzing: {start_entry} ({start_value}) -> {end_entry} ({end_value})")

                # Print found cycles and perform calculations for this file
                print_results_and_calculations([results], calculations, value_unit="s")

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

                print(f"\nAnalyzing: {entry_name} when {trigger_entry} = {trigger_value}")

                # Print captured values and perform calculations for this file
                print_results_and_calculations([results], calculations, value_unit=entry_unit)

    # Aggregate the checks across all files
    if check_configs and check_reports:
        print(f"\n=== AGGREGATED CHECK RESULTS ACROSS ALL FILES ===\n")
        print_check_findings(merge_check_findings(check_reports), is_aggregate=True)

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
                print_per_file_counts(all_results_by_file, calculations)

                # Print aggregated cycles summary and perform calculations
                print_results_and_calculations(all_results_by_file, calculations, value_unit="s")
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
                print_per_file_counts(all_values_by_file, calculations)

                print_results_and_calculations(all_values_by_file, calculations, value_unit=entry_unit)
                    
            else:
                print(f"  No values captured for this analysis across all files")

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