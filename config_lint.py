#! /usr/bin/env python3
"""Check an analysis config against the logs it will run on, before running it.

    ./config_lint.py checks2026.json ./logs
    ./config_lint.py config2026.json ./logs --index ~/season2026.sqlite

A config that names an entry wrongly does not fail: the analysis runs, matches
nothing, and reports nothing - the worst possible feedback, and found only after
a multi-minute run (claude/ROADMAP.md §12.1). Everything needed to catch it is
in the log index's manifest, which lists every entry in every log with its type,
the field names it produces once decoded, and the robot states it was recorded
in. So this reads only the manifest, and takes a second rather than minutes.

What it reports:

- **error**: something that guarantees an analysis or check silently does
  nothing - an entry or pattern that matches nothing in any log, a value that
  can never equal what the entry holds ("12" against a double), a check that
  needs numbers pointed at a string, or an entry that is never recorded in the
  robot states the config's filters keep.
- **warning**: a key the tool does not read (usually a typo), or an unknown
  calculation, gate, statistic or severity.
- **note**: an entry present in only some of the logs, which is often a rename
  part-way through a season rather than a mistake.

Logs not yet in the index are indexed first (once; see log_index.py).
"""

import argparse
import difflib
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from capture import MANDATORY_ENTRIES, filters_from_config, passes_filter
from entry_patterns import EntryPattern
from log_index import (DEFAULT_INDEX_NAME, LogIndex, Manifest, NotAnIndexError,
                       _decode_state, default_index_path, find_logs)

__all__ = ["Problem", "lint_config", "config_warnings", "main"]

# What the analyzer reads, by where it appears. Anything else is ignored by the
# tool, which is how a misspelt key ("startValeu") silently does nothing.
TOP_KEYS = {"enabled", "fmsAttached", "robotMode", "field", "timeAnalysis",
            "valueAnalysis", "checks"}
TIME_KEYS = {"startEntry", "startValue", "endEntry", "endValue", "calculations"}
VALUE_KEYS = {"entry", "entryUnit", "triggerEntry", "triggerValue", "calculations"}
CHECK_KEYS = {"name", "entry", "expect", "severity", "while", "unit", "excludeEntry",
              "excludeMessage", "expectEntries", "clearBelow", "minDuration", "compare",
              "statistic", "deviation", "minimumSiblings"}
EXPECT_KEYS = {"always", "never", "atLeastOnce", "alwaysOneOf", "alwaysStartsWith",
               "above", "below", "minIncrease"}
EXPECT_WORDS = {"empty", "present"}
DEVIATION_KEYS = {"aboveSiblingsBy", "belowSiblingsBy"}
CALCULATIONS = {"average", "max", "min", "abs_average", "abs_max", "abs_min", "count",
                "outlier_2std", "abs_outlier_2std"}
ROBOT_MODES = {"both", "auto", "teleop"}
GATES = {"enabled", "disabled", "afterFirstEnable", "any"}
STATISTICS = {"peak", "min", "mean", "rise"}
SEVERITIES = {"error", "warning", "info"}

NUMERIC_TYPES = {"int", "int64", "float", "double"}


@dataclass
class Problem:
    severity: str   # "error", "warning" or "note"
    where: str      # e.g. "timeAnalysis[1].endValue"
    message: str

    def __str__(self) -> str:
        return f"{self.severity:7} {self.where}: {self.message}"


def type_class(type_str: Optional[str]) -> Optional[str]:
    """What kind of value an entry type holds, as far as comparisons go."""
    if type_str is None:
        return None
    if type_str == "boolean":
        return "boolean"
    if type_str in NUMERIC_TYPES:
        return "number"
    if type_str in ("string", "json"):
        return "string"
    if type_str.endswith("[]"):
        return "array"
    return "other"


def _value_class(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "other"


class _Linter:
    def __init__(self, config: Dict[str, Any], manifest: Manifest):
        self.config = config
        self.manifest = manifest
        self.problems: List[Problem] = []
        self._noted: Set[str] = set()
        self.filter_enabled, self.filter_fms, self.robot_mode = filters_from_config(config)

    def add(self, severity: str, where: str, message: str) -> None:
        if severity == "note":
            # Notes describe the logs, not a place in the config: say each once
            # even when start and end name the same entry.
            if message in self._noted:
                return
            self._noted.add(message)
        self.problems.append(Problem(severity, where, message))

    def unknown_keys(self, where: str, item: Dict[str, Any], known: Set[str]) -> None:
        for key in item:
            if key not in known:
                close = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
                hint = f"; did you mean {close[0]!r}?" if close else ""
                self.add("warning", f"{where}.{key}",
                         f"not a setting the tool reads, so it has no effect{hint}")

    def one_of(self, where: str, value: Any, allowed: Set[str], what: str) -> None:
        if value is not None and value not in allowed:
            self.add("warning", where, f"{value!r} is not a known {what} "
                                       f"({', '.join(sorted(allowed))})")

    # --- entries ----------------------------------------------------------------

    def entry(self, where: str, pattern: Any, exclude: Sequence[str] = ()) -> List[str]:
        """The fields a configured entry name or pattern matches, reporting misses."""
        if not isinstance(pattern, str) or not pattern:
            self.add("error", where, "must be an entry name")
            return []
        fields = self.manifest.matching(pattern, exclude)
        if not fields:
            close = self.manifest.suggestions(pattern)
            hint = f"; did you mean {' or '.join(repr(c) for c in close)}?" if close else ""
            self.add("error", where, f"{pattern!r} matches no entry in any of the "
                                     f"{self.manifest.log_count} logs{hint}")
            return []

        present = set().union(*(self.manifest.logs[f] for f in fields))
        if len(present) < self.manifest.log_count:
            self.add("note", where, f"{pattern!r} is in {len(present)} of "
                                    f"{self.manifest.log_count} logs")

        if pattern not in MANDATORY_ENTRIES:
            states = set().union(*(self.manifest.states[f] for f in fields))
            if states and not any(passes_filter(*_decode_state(state), self.filter_enabled,
                                                self.filter_fms, self.robot_mode)
                                  for state in states):
                records = sum(self.manifest.records[f] for f in fields)
                boot_only = (records <= len(present)
                             and all(not _decode_state(state)[0] for state in states))
                if boot_only:
                    # AdvantageKit writes a value only when it changes, so an
                    # entry written once, while disabled, is its starting value,
                    # held all match: nothing happened to analyse, which is not
                    # a mistake in the config.
                    self.add("note", where,
                             f"{pattern!r} never changed: it was written once per log, "
                             f"before enabling, and held that value throughout")
                else:
                    self.add("error", where,
                             f"{pattern!r} is only ever recorded while {_describe(states)}, "
                             f"and the config's filters ({self._filters()}) drop every one "
                             f"of those records")
        return fields

    def _filters(self) -> str:
        parts = []
        if self.filter_enabled:
            parts.append("enabled")
        if self.filter_fms:
            parts.append("fmsAttached")
        if self.robot_mode != "both":
            parts.append(f"robotMode {self.robot_mode}")
        return ", ".join(parts)

    def classes(self, fields: Iterable[str]) -> Set[Optional[str]]:
        """The value classes of the fields; None stands for a struct leaf."""
        found: Set[Optional[str]] = set()
        for name in fields:
            found.update(type_class(t) for t in self.manifest.types[name])
        return found

    def comparable(self, where: str, fields: List[str], value: Any) -> None:
        """A configured value must be able to equal what the entry holds."""
        if not fields or isinstance(value, (list, dict)) or value is None:
            return
        wanted = _value_class(value)
        known = {c for c in self.classes(fields) if c is not None}
        if not known or "array" in known or "other" in known:
            return
        # Python compares booleans and numbers as numbers (True == 1), so only a
        # string on one side and a non-string on the other can never be equal.
        if (wanted == "string") != ("string" in known) and len(known) == 1:
            kind = next(iter(known))
            self.add("error", where,
                     f"{value!r} is a {wanted}, but {_entry_list(fields)} holds "
                     f"{_article(kind)} {kind}, so they are never equal")

    def numeric(self, where: str, fields: List[str], why: str) -> None:
        known = {c for c in self.classes(fields) if c is not None}
        if known and "number" not in known and "boolean" not in known:
            kinds = " or ".join(sorted(known))
            self.add("error", where, f"{why}, but {_entry_list(fields)} holds {kinds}")

    # --- sections ---------------------------------------------------------------

    def run(self) -> List[Problem]:
        self.unknown_keys("config", self.config, TOP_KEYS)
        self.one_of("config.robotMode", self.config.get("robotMode"), ROBOT_MODES,
                    "robotMode")
        for i, analysis in enumerate(self.config.get("timeAnalysis", [])):
            self.time_analysis(f"timeAnalysis[{i}]", analysis)
        for i, analysis in enumerate(self.config.get("valueAnalysis", [])):
            self.value_analysis(f"valueAnalysis[{i}]", analysis)
        for i, rule in enumerate(self.config.get("checks", [])):
            name = rule.get("name")
            self.check(f"checks[{i}]" + (f" ({name})" if name else ""), rule)
        return self.problems

    def calculations(self, where: str, calculations: Any) -> None:
        for j, calc in enumerate(calculations or []):
            self.one_of(f"{where}.calculations[{j}].type", calc.get("type"),
                        CALCULATIONS, "calculation")

    def time_analysis(self, where: str, analysis: Dict[str, Any]) -> None:
        self.unknown_keys(where, analysis, TIME_KEYS)
        for role, value_key in (("startEntry", "startValue"), ("endEntry", "endValue")):
            fields = self.entry(f"{where}.{role}", analysis.get(role))
            if value_key in analysis:
                self.comparable(f"{where}.{value_key}", fields, analysis[value_key])
        self.calculations(where, analysis.get("calculations"))

    def value_analysis(self, where: str, analysis: Dict[str, Any]) -> None:
        self.unknown_keys(where, analysis, VALUE_KEYS)
        fields = self.entry(f"{where}.entry", analysis.get("entry"))
        if any(calc.get("type") != "count" for calc in analysis.get("calculations") or []):
            self.numeric(f"{where}.entry", fields, "its calculations need numbers")
        if analysis.get("triggerEntry"):
            trigger = self.entry(f"{where}.triggerEntry", analysis["triggerEntry"])
            if "triggerValue" in analysis:
                self.comparable(f"{where}.triggerValue", trigger, analysis["triggerValue"])
        self.calculations(where, analysis.get("calculations"))

    def check(self, where: str, rule: Dict[str, Any]) -> None:
        self.unknown_keys(where, rule, CHECK_KEYS)
        self.one_of(f"{where}.while", rule.get("while"), GATES, "gate")
        self.one_of(f"{where}.severity", rule.get("severity"), SEVERITIES, "severity")
        exclude = rule.get("excludeEntry") or []
        fields = self.entry(f"{where}.entry", rule.get("entry"), exclude)

        expected = rule.get("expectEntries") or []
        pattern = EntryPattern(rule.get("entry") or "")
        if expected and pattern.has_wildcard:
            seen = {pattern.captures(name) for name in fields}
            for item in expected:
                wanted = tuple(item) if isinstance(item, (list, tuple)) else (item,)
                if wanted not in seen:
                    self.add("error", f"{where}.expectEntries",
                             f"{pattern.substitute(wanted)!r} is in none of the logs")

        if rule.get("compare") is not None:
            self.one_of(f"{where}.compare", rule.get("compare"), {"siblings"}, "comparison")
            self.one_of(f"{where}.statistic", rule.get("statistic"), STATISTICS, "statistic")
            deviation = rule.get("deviation") or {}
            self.unknown_keys(f"{where}.deviation", deviation, DEVIATION_KEYS)
            self.numeric(f"{where}.entry", fields, "comparing siblings needs numbers")
            minimum = rule.get("minimumSiblings", 2)
            if fields and len(fields) < minimum:
                self.add("error", f"{where}.entry",
                         f"{rule.get('entry')!r} matches {len(fields)} entr"
                         f"{'y' if len(fields) == 1 else 'ies'}; comparing siblings "
                         f"needs at least {minimum}")
            return

        expect = rule.get("expect")
        if isinstance(expect, str):
            self.one_of(f"{where}.expect", expect, EXPECT_WORDS, "expectation")
        elif isinstance(expect, dict):
            self.unknown_keys(f"{where}.expect", expect, EXPECT_KEYS)
            for key in ("always", "never", "atLeastOnce"):
                if key in expect:
                    self.comparable(f"{where}.expect.{key}", fields, expect[key])
            for value in expect.get("alwaysOneOf") or []:
                self.comparable(f"{where}.expect.alwaysOneOf", fields, value)
            if "alwaysStartsWith" in expect:
                known = {c for c in self.classes(fields) if c is not None}
                if known and "string" not in known:
                    self.add("error", f"{where}.expect.alwaysStartsWith",
                             f"a prefix needs text, but {_entry_list(fields)} holds "
                             f"{' or '.join(sorted(known))}")
            for key in ("above", "below", "minIncrease"):
                if key in expect:
                    self.numeric(f"{where}.expect.{key}", fields, "a threshold needs numbers")
        elif expect is not None:
            self.add("warning", f"{where}.expect", "should be \"empty\", \"present\" or "
                                                   "an object such as {\"always\": true}")


def _article(word: str) -> str:
    return "an" if word[:1] in "aeiou" else "a"


def _entry_list(fields: List[str]) -> str:
    return repr(fields[0]) if len(fields) == 1 else f"{len(fields)} matching entries"


def _describe(states: Set[int]) -> str:
    """The robot modes a set of DriverStation states covers, in words."""
    modes = set()
    for state in states:
        enabled, autonomous, _fms = _decode_state(state)
        if not enabled:
            modes.add("disabled")
        elif autonomous:
            modes.add("in auto")
        else:
            modes.add("in teleop")
    order = ["disabled", "in auto", "in teleop"]
    return " or ".join(mode for mode in order if mode in modes)


def lint_config(config: Dict[str, Any], manifest: Manifest) -> List[Problem]:
    """Every problem in a config, judged against the logs the manifest covers."""
    return _Linter(config, manifest).run()


def config_warnings(config: Dict[str, Any], index: LogIndex, logs: Sequence[str]) -> List[str]:
    """Lines for analysis.py to print before a run: errors and warnings only.

    Says nothing unless every log is already indexed. Judged against only some
    of them, an entry that exists solely in the others would look missing.
    """
    try:
        ids, skipped = indexed_file_ids(index, logs, build=False)
        if skipped or not ids:
            return []
        problems = [p for p in lint_config(config, Manifest(index, ids))
                    if p.severity != "note"]
    except Exception as error:  # a lint must never stop the analysis it serves
        return [f"Config not checked ({error})"]
    if not problems:
        return []
    errors = sum(1 for p in problems if p.severity == "error")
    lines = [f"Config check: {errors} error{'s' if errors != 1 else ''}, "
             f"{len(problems) - errors} warning{'s' if len(problems) - errors != 1 else ''} "
             f"(details: config_lint.py)"]
    lines += [f"  {problem}" for problem in problems]
    return lines


def indexed_file_ids(index: LogIndex, logs: Sequence[str], build: bool,
                     progress=None) -> Tuple[List[int], List[str]]:
    """The index's file ids for these logs, indexing any that are missing if
    `build`. Returns (ids, names of logs that could not be used)."""
    ids, skipped = [], []
    for number, path in enumerate(logs, 1):
        file_id = index.file_id_for(path)
        if file_id is None and build:
            if progress:
                progress(number, len(logs), path)
            index.add(path)
            file_id = index.file_id_for(path)
        if file_id is None:
            skipped.append(os.path.basename(path))
        elif file_id not in ids:
            ids.append(file_id)
    return ids, skipped


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check an analysis config against the logs it will run on, using "
                    "the log index's manifest (indexing any logs not yet in it).")
    parser.add_argument("config", help="analysis config JSON file")
    parser.add_argument("log_folder", help="folder of .wpilog files the config will run on "
                                           "(subfolders included)")
    parser.add_argument("--index", metavar="PATH",
                        help=f"index file (default: {DEFAULT_INDEX_NAME} in the log folder)")
    args = parser.parse_args(argv)

    try:
        with open(args.config) as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(f"Error loading config file: {error}", file=sys.stderr)
        return 2
    logs = [path for path in find_logs([args.log_folder])
            if not path.endswith("_sim.wpilog")]
    if not logs:
        print(f"No log files found in {args.log_folder}", file=sys.stderr)
        return 2

    path = args.index or default_index_path(args.log_folder)
    try:
        index, rebuilt = LogIndex.open_for_build(path)
    except NotAnIndexError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    if rebuilt:
        print(rebuilt, file=sys.stderr)

    def progress(number: int, total: int, log_path: str) -> None:
        print(f"Indexing {os.path.basename(log_path)} ({number}/{total}) ...",
              file=sys.stderr)

    try:
        ids, skipped = indexed_file_ids(index, logs, build=True, progress=progress)
        manifest = Manifest(index, ids)
    finally:
        index.close()

    problems = lint_config(config, manifest)
    print(f"{args.config} against {len(ids)} logs in {args.log_folder}")
    if skipped:
        print(f"  (not usable: {', '.join(skipped)})")
    for problem in problems:
        print(problem)
    counts = {severity: sum(1 for p in problems if p.severity == severity)
              for severity in ("error", "warning", "note")}
    summary = ", ".join(f"{n} {severity}{'s' if n != 1 else ''}"
                        for severity, n in counts.items() if n)
    print(summary or "No problems found")
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
