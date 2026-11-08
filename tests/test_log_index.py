#! /usr/bin/env python3
"""Tests for the log index (log_index.py) and analysis.py --index.

The contract is that a Log replayed from the index is the Log reading the
.wpilog would have built, so the analyzer's output is identical either way.
These tests write small synthetic logs that exercise the parts of that
contract real logs reach only by accident: a config whose filters cut the end
of the log, a struct leaf reached through a "**" pattern, an entry the config
captures but never reads, and the cases where the index must decline and say
why. tests/test_integration.py runs the goldens through the index on the real
season logs.
"""

import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import log_index  # noqa: E402
from analysis import compute_checks, process_log_file  # noqa: E402
from capture import MANDATORY_ENTRIES, config_entry_names, filters_from_config  # noqa: E402
from log_index import (IndexVersionError, LogIndex, NotAnIndexError,  # noqa: E402
                       parse_log_name)
from wpilog_writer import WpilogWriter  # noqa: E402

ANALYSIS = REPO_ROOT / "analysis.py"
LOG_INDEX = REPO_ROOT / "log_index.py"

SECOND = 1_000_000
CYCLE = 20_000  # 50 Hz

MOTOR_SCHEMA = b"double temp;bool deviceConnected"

# Unfiltered checks, including "/**/*Connected*", which could be a leaf of any
# entry and so captures every record in a full read.
CHECKS_CONFIG = {
    "checks": [
        {"name": "Disconnected", "entry": "/**/*Connected*", "expect": {"always": True}},
        {"name": "Alert", "entry": "/RealOutputs/Alerts/errors", "expect": "empty",
         "while": "afterFirstEnable"},
        {"name": "No tags", "entry": "/Vision/*/UpdatePoseCount",
         "expect": {"minIncrease": 1}, "while": "afterFirstEnable"},
        {"name": "Roller hot", "entry": "/Intake/RollerTemp", "expect": {"above": 40},
         "unit": "C", "while": "afterFirstEnable"},
        {"name": "Motor hot", "entry": "/Intake/Motor/temp", "expect": {"above": 40},
         "unit": "C"},
    ]
}

# Filtered to enabled teleop with FMS attached, so the records a full read
# captures stop when the robot is disabled - before the log ends.
FILTERED_CONFIG = {
    "enabled": True,
    "fmsAttached": True,
    "robotMode": "teleop",
    "timeAnalysis": [
        {"startEntry": "/RealOutputs/Mode", "startValue": "INTAKE",
         "endEntry": "/RealOutputs/Mode", "endValue": "SHOOT",
         "calculations": [{"type": "count", "name": "Cycles"},
                          {"type": "average", "name": "Average cycle"}]},
    ],
    "valueAnalysis": [
        {"entry": "/Intake/RollerTemp", "entryUnit": "C",
         "triggerEntry": "/RealOutputs/Mode", "triggerValue": "SHOOT",
         "calculations": [{"type": "max", "name": "Hottest shot"}]},
    ],
    "checks": [
        {"name": "Disconnected", "entry": "/**/*Connected*", "expect": {"always": True}},
    ],
}

# Reads only the state tier, so a plain build serves it.
STATE_CONFIG = {
    "checks": [
        {"name": "Flywheel", "entry": "/Shooter/FlywheelConnected",
         "expect": {"always": True}, "while": "enabled"},
        {"name": "Alert", "entry": "/RealOutputs/Alerts/errors", "expect": "empty"},
    ]
}


def write_match_log(path, seconds=20, roller_offset=0.0, duplicate_id=False):
    """A small but complete match: FMS, auto, teleop, then disabled.

    Timeline (seconds): FMS attaches at 1, enabled 2-15 with auto until 5. The
    flywheel disconnects at 6 for half a second and again at 14 for good. A
    high-rate "busy" double nobody reads keeps logging until the very end,
    after everything a config reads has stopped: that is the record a full
    read ends on and a partial replay does not load.
    """
    w = WpilogWriter()
    enabled = w.start("/DriverStation/Enabled", "boolean")
    auto = w.start("/DriverStation/Autonomous", "boolean")
    fms = w.start("/DriverStation/FMSAttached", "boolean")
    schema = w.start("/.schema/struct:Motor", "structschema")
    proto = w.start("/.schema/proto:Foo", "proto:FileDescriptorProto")
    motor = w.start("/Intake/Motor", "struct:Motor")
    roller = w.start("/Intake/RollerTemp", "double")
    flywheel = w.start("/Shooter/FlywheelConnected", "boolean")
    poses = w.start("/Vision/A/UpdatePoseCount", "int64")
    mode = w.start("/RealOutputs/Mode", "string")
    alerts = w.start("/RealOutputs/Alerts/errors", "string[]")
    busy = w.start("/RealOutputs/Busy", "double")
    if duplicate_id:
        w.start("/RealOutputs/Busy", "double", entry=busy)

    w.raw(schema, 0, MOTOR_SCHEMA)
    w.raw(proto, 0, b"\x0a\x07foo.proto")
    w.boolean(enabled, 0, False)
    w.boolean(auto, 0, False)
    w.boolean(fms, 0, False)
    w.string_array(alerts, 0, [])
    w.string(mode, 0, "IDLE")

    for ts in range(0, seconds * SECOND, CYCLE):
        t = ts / SECOND
        if ts == 1 * SECOND:
            w.boolean(fms, ts, True)
        if ts == 2 * SECOND:
            w.boolean(enabled, ts, True)
            w.boolean(auto, ts, True)
        if ts == 5 * SECOND:
            w.boolean(auto, ts, False)
        if ts == 15 * SECOND:
            w.boolean(enabled, ts, False)
        if ts == 7 * SECOND:
            w.string_array(alerts, ts, ["Roller jammed"])
        if ts == int(7.5 * SECOND):
            w.string_array(alerts, ts, [])
        for start, value in ((6, "INTAKE"), (8, "SHOOT"), (9, "INTAKE"), (11.5, "SHOOT"),
                             (12, "IDLE")):
            if ts == int(start * SECOND):
                w.string(mode, ts, value)

        temp = 25.0 + roller_offset + t * 1.5
        if t < 17:
            w.double(roller, ts, temp)
            connected = not (8 <= t < 8.4)
            w.raw(motor, ts, struct_motor(temp + 1.0, connected))
        if t < 18:
            w.boolean(flywheel, ts, not (6 <= t < 6.5 or t >= 14))
        if t < 12 and ts % (10 * CYCLE) == 0:
            w.int64(poses, ts, ts // (10 * CYCLE))
        w.double(busy, ts, t)
    return w.save(path)


def struct_motor(temp, connected):
    import struct
    return struct.pack("<d?", temp, connected)


def run(*args, check=True):
    completed = subprocess.run([sys.executable, *map(str, args)], cwd=str(REPO_ROOT),
                               capture_output=True, text=True, timeout=120)
    if check and completed.returncode != 0:
        raise AssertionError(f"{args} exited {completed.returncode}\n"
                             f"{completed.stdout}\n{completed.stderr}")
    return completed


class Workspace(unittest.TestCase):
    """A temporary log folder with two synthetic matches."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.match_a = write_match_log(self.logs / "akit_26-04-30_14-46-03_johnson_q12.wpilog")
        self.match_b = write_match_log(self.logs / "akit_26-04-30_16-20-07_johnson_q22.wpilog",
                                       roller_offset=8.0)
        self.db = self.tmp / "index.sqlite"

    def config_file(self, config, name="config.json"):
        path = self.tmp / name
        path.write_text(json.dumps(config))
        return path

    def build(self, *configs):
        args = [LOG_INDEX, "build", self.logs, "--index", self.db]
        for config in configs:
            args += ["--config", config]
        return run(*args)

    def full_log(self, path, config):
        with mock.patch("builtins.print"):
            return process_log_file(str(path), set(MANDATORY_ENTRIES),
                                    config_entry_names(config), *filters_from_config(config))


class ProtobufSchemaTest(Workspace):
    """Defect #4: a protobuf schema entry raised IndexError and failed the log."""

    def test_a_log_with_a_protobuf_schema_is_read(self):
        log = self.full_log(self.match_a, CHECKS_CONFIG)
        self.assertIn("/Intake/RollerTemp", log.get_field_keys())

    def test_struct_schemas_beside_it_still_decode(self):
        log = self.full_log(self.match_a, CHECKS_CONFIG)
        field = log.get_field("/Intake/Motor/deviceConnected")
        self.assertIsNotNone(field)
        self.assertIn(False, field.data.values)


class ReplayParityTest(Workspace):
    """analysis.py prints the same report from the index as from the files."""

    def assert_same_report(self, config):
        config_path = self.config_file(config)
        expected = run(ANALYSIS, self.logs, config_path)
        self.assertEqual(expected.stderr, "")
        actual = run(ANALYSIS, self.logs, config_path, "--index", self.db)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertIn(f"Index: 2 of 2 logs read from {self.db}", actual.stderr)

    def test_unfiltered_checks_through_a_double_star_pattern(self):
        self.build(self.config_file(CHECKS_CONFIG))
        self.assert_same_report(CHECKS_CONFIG)

    def test_filtered_time_and_value_analyses(self):
        self.build(self.config_file(FILTERED_CONFIG))
        self.assert_same_report(FILTERED_CONFIG)

    def test_state_tier_config_needs_no_config_at_build(self):
        self.build()
        self.assert_same_report(STATE_CONFIG)

    def test_the_log_ends_where_a_full_read_would_end(self):
        # The flywheel spell open at the end is measured to the log's end, so
        # this is what keeps that duration right. The busy entry is captured by
        # "/**" but not read, so the replay does not load the record the full
        # read ends on and must be told where it was.
        self.build(self.config_file(CHECKS_CONFIG), self.config_file(FILTERED_CONFIG, "f.json"))
        index = LogIndex(str(self.db))
        self.addCleanup(index.close)
        for config in (CHECKS_CONFIG, FILTERED_CONFIG):
            with self.subTest(filtered=config is FILTERED_CONFIG):
                full = self.full_log(self.match_a, config)
                replayed = index.load_log(str(self.match_a), config).log
                self.assertNotIn("/RealOutputs/Busy", replayed.get_field_keys())
                self.assertEqual(replayed.get_last_timestamp(), full.get_last_timestamp())
                self.assertEqual(compute_checks(replayed, "a", config["checks"]).findings,
                                 compute_checks(full, "a", config["checks"]).findings)

    def test_the_end_differs_between_filters(self):
        # Guards the test above: if the filters did not move the end, it would
        # pass without exercising anything.
        self.assertNotEqual(self.full_log(self.match_a, CHECKS_CONFIG).get_last_timestamp(),
                            self.full_log(self.match_a, FILTERED_CONFIG).get_last_timestamp())

    def test_only_the_entries_the_config_reads_are_loaded(self):
        self.build(self.config_file(CHECKS_CONFIG))
        index = LogIndex(str(self.db))
        self.addCleanup(index.close)
        keys = set(index.load_log(str(self.match_a), CHECKS_CONFIG).log.get_field_keys())
        self.assertIn("/Intake/Motor/deviceConnected", keys)
        self.assertIn("/Shooter/FlywheelConnected", keys)
        self.assertNotIn("/RealOutputs/Mode", keys)

    def test_ingest_messages_are_replayed(self):
        write_match_log(self.match_a, duplicate_id=True)
        self.build()
        self.assert_same_report(STATE_CONFIG)


class CoverageTest(Workspace):
    """Where the index cannot reproduce a read, it declines and says why."""

    def test_unstored_numeric_entries_fall_back_to_the_file(self):
        self.build()
        config_path = self.config_file(CHECKS_CONFIG)
        expected = run(ANALYSIS, self.logs, config_path).stdout
        result = run(ANALYSIS, self.logs, config_path, "--index", self.db)
        self.assertEqual(result.stdout, expected)
        self.assertIn("Index: 0 of 2 logs read", result.stderr)
        self.assertIn("needs entries not stored: /Intake/Motor, /Intake/RollerTemp",
                      result.stderr)
        self.assertIn(f"--config {config_path}", result.stderr)

    def test_building_with_the_config_then_serves_it(self):
        self.build()
        output = self.build(self.config_file(CHECKS_CONFIG)).stdout
        self.assertIn("extended: stored 2 more entries", output)
        index = LogIndex(str(self.db))
        self.addCleanup(index.close)
        self.assertIsNotNone(index.load_log(str(self.match_a), CHECKS_CONFIG).log)

    def test_rebuilding_changes_nothing(self):
        config = self.config_file(CHECKS_CONFIG)
        self.build(config)
        output = self.build(config).stdout
        self.assertIn("2 unchanged", output)
        self.assertNotIn("indexed", output.replace("2 unchanged", ""))

    def test_a_missing_index_is_reported_and_every_file_read(self):
        config_path = self.config_file(STATE_CONFIG)
        expected = run(ANALYSIS, self.logs, config_path).stdout
        result = run(ANALYSIS, self.logs, config_path, "--index", self.db)
        self.assertEqual(result.stdout, expected)
        self.assertIn(f"No index at {self.db}; reading every .wpilog", result.stderr)

    def test_the_default_index_lives_in_the_log_folder(self):
        run(LOG_INDEX, "build", self.logs)
        self.assertTrue((self.logs / ".log-index.sqlite").is_file())
        result = run(ANALYSIS, self.logs, self.config_file(STATE_CONFIG), "--index")
        self.assertIn("Index: 2 of 2 logs read", result.stderr)

    def test_an_unindexed_log_is_read_from_its_file(self):
        self.build()
        # Different contents: an identical copy would be recognised as indexed.
        write_match_log(self.logs / "akit_26-05-01_10-00-00_johnson_q40.wpilog",
                        roller_offset=3.0)
        result = run(ANALYSIS, self.logs, self.config_file(STATE_CONFIG), "--index", self.db)
        self.assertIn("Index: 2 of 3 logs read", result.stderr)
        self.assertIn("akit_26-05-01_10-00-00_johnson_q40.wpilog (not indexed)",
                      result.stderr)

    def test_conflicting_types_for_one_name_are_read_from_the_file(self):
        w = WpilogWriter()
        first = w.start("/X", "double")
        w.double(first, 1, 1.0)
        second = w.start("/X", "string")
        w.string(second, 2, "two")
        path = w.save(self.logs / "akit_26-05-01_11-00-00.wpilog")
        index = LogIndex.open_for_build(str(self.db))[0]
        self.addCleanup(index.close)
        index.add(str(path))
        result = index.load_log(str(path), STATE_CONFIG)
        self.assertIsNone(result.log)
        self.assertIn("two types", result.reason)


class IdentityTest(Workspace):
    """Files are known by content, not by name."""

    def setUp(self):
        super().setUp()
        self.index = LogIndex.open_for_build(str(self.db))[0]
        self.addCleanup(self.index.close)
        self.index.add(str(self.match_a))

    def test_a_renamed_copy_is_recognised(self):
        copy = self.tmp / "renamed.wpilog"
        shutil.copy(self.match_a, copy)
        self.assertEqual(self.index.file_id_for(str(copy)),
                         self.index.file_id_for(str(self.match_a)))
        self.assertEqual(self.index.add(str(copy)).action, "unchanged")

    def test_changed_contents_are_a_different_file(self):
        write_match_log(self.match_a, seconds=21)
        self.assertIsNone(self.index.file_id_for(str(self.match_a)))
        self.assertEqual(self.index.add(str(self.match_a)).action, "indexed")

    def test_a_log_growing_during_extraction_is_skipped(self):
        real_scan = log_index._scan

        def scan_while_growing(buffer, keep=None):
            result = real_scan(buffer, keep)
            with open(self.match_b, "ab") as handle:
                handle.write(b"\0" * 32)
            return result

        with mock.patch.object(log_index, "_scan", scan_while_growing):
            result = self.index.add(str(self.match_b))
        self.assertEqual((result.action, result.detail), ("skipped", "still being written"))
        self.assertIsNone(self.index.file_id_for(str(self.match_b)))

    def test_the_manifest_describes_the_match(self):
        row = self.index.files()[0]
        self.assertEqual((row["event"], row["match"]), ("johnson", "q12"))
        self.assertEqual(row["fms_attached"], 1)
        self.assertAlmostEqual(row["enabled_seconds"], 13.0)
        auto, teleop, disabled = self.index.conn.execute(
            "SELECT auto_seconds, teleop_seconds, disabled_seconds FROM files").fetchone()
        self.assertAlmostEqual(auto, 3.0)
        self.assertAlmostEqual(teleop, 10.0)
        self.assertAlmostEqual(disabled, 19.98 - 13.0)

    def test_state_samples_are_searchable(self):
        found = self.index.conn.execute(
            "SELECT s.ts_us FROM samples s JOIN entries e ON e.id = s.entry_id "
            "WHERE e.name = '/RealOutputs/Mode' AND s.value = 'SHOOT'").fetchall()
        self.assertEqual([ts for (ts,) in found], [8 * SECOND, int(11.5 * SECOND)])


class ModeStatisticsTest(unittest.TestCase):
    """Numeric values are summarised per robot mode, weighted by time held.

    AdvantageKit writes a value only when it changes. In this log a current
    sits at 0 A for the 10 s before enabling and is written once; a mean over
    samples would count that 10 s as one reading.
    """

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        w = WpilogWriter()
        enabled = w.start("/DriverStation/Enabled", "boolean")
        auto = w.start("/DriverStation/Autonomous", "boolean")
        current = w.start("/Intake/RollerCurrent", "double")
        clock = w.start("/Clock", "int64")
        w.boolean(enabled, 0, False)
        w.boolean(auto, 0, False)
        w.double(current, 0, 0.0)            # disabled, held 10 s
        w.boolean(enabled, 10 * SECOND, True)
        w.boolean(auto, 10 * SECOND, True)
        w.double(current, 10 * SECOND, 10.0)  # auto, 1 s
        w.double(current, 11 * SECOND, 30.0)  # auto 1 s, then teleop 4 s
        w.boolean(auto, 12 * SECOND, False)
        w.double(current, 16 * SECOND, 50.0)  # teleop 4 s, then disabled 10 s
        w.boolean(enabled, 20 * SECOND, False)
        w.int64(clock, 30 * SECOND, 30)       # the log ends here
        path = w.save(tmp / "akit_26-02-14_18-00-00.wpilog")
        self.index = LogIndex.open_for_build(str(tmp / "index.sqlite"))[0]
        self.addCleanup(self.index.close)
        self.index.add(str(path))

    def stats(self, mode):
        return self.index.conn.execute(
            "SELECT samples, seconds, min, max, mean, rms FROM entry_stats s "
            "JOIN entries e ON e.id = s.entry_id "
            "WHERE e.name = '/Intake/RollerCurrent' AND s.mode = ?", (mode,)).fetchone()

    def assert_stats(self, mode, samples, seconds, low, high, mean, rms):
        actual = self.stats(mode)
        self.assertEqual(actual[0], samples)
        for got, want in zip(actual[1:], (seconds, low, high, mean, rms)):
            self.assertAlmostEqual(got, want)

    def test_auto(self):
        self.assert_stats("auto", 2, 2.0, 10.0, 30.0, 20.0, math.sqrt(500))

    def test_teleop_includes_a_value_carried_over_from_auto(self):
        self.assert_stats("teleop", 1, 8.0, 30.0, 50.0, 40.0, math.sqrt(1700))

    def test_disabled_weights_by_time_and_holds_to_the_end(self):
        # 0 A for 10 s before enabling, 50 A for the 10 s after.
        self.assert_stats("disabled", 1, 20.0, 0.0, 50.0, 25.0, math.sqrt(1250))

    def test_time_in_each_mode(self):
        self.assertEqual(self.index.conn.execute(
            "SELECT auto_seconds, teleop_seconds, disabled_seconds, enabled_seconds "
            "FROM files").fetchone(), (2.0, 8.0, 20.0, 10.0))

    def test_only_numeric_scalars_are_summarised(self):
        names = {name for (name,) in self.index.conn.execute(
            "SELECT DISTINCT e.name FROM entry_stats s JOIN entries e ON e.id = s.entry_id")}
        self.assertEqual(names, {"/Intake/RollerCurrent", "/Clock"})


class SeasonLibraryTest(unittest.TestCase):
    """One index for a season, built from logs spread over many folders."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.practice = self.tmp / "practice" / "2026-02-14"
        self.event = self.tmp / "events" / "wimil"
        for folder in (self.practice, self.event, self.tmp / "events" / ".cache"):
            folder.mkdir(parents=True)
        write_match_log(self.practice / "akit_26-02-14_18-00-00.wpilog", roller_offset=1.0)
        write_match_log(self.event / "akit_26-03-07_10-00-00_wimil_q3.wpilog")
        write_match_log(self.tmp / "events" / ".cache" / "akit_26-03-07_11-00-00.wpilog",
                        roller_offset=2.0)
        self.db = self.tmp / "season.sqlite"

    def build(self, *sources, check=True):
        return run(LOG_INDEX, "build", *sources, "--index", self.db, check=check)

    def names(self):
        index = LogIndex(str(self.db))
        try:
            return sorted(row["name"] for row in index.files())
        finally:
            index.close()

    def test_folders_are_walked_and_hidden_ones_skipped(self):
        self.build(self.tmp / "practice", self.tmp / "events")
        self.assertEqual(self.names(), ["akit_26-02-14_18-00-00.wpilog",
                                        "akit_26-03-07_10-00-00_wimil_q3.wpilog"])

    def test_later_builds_add_to_the_same_index(self):
        self.build(self.tmp / "practice")
        output = self.build(self.tmp / "practice", self.tmp / "events").stdout
        self.assertIn("1 indexed, 1 unchanged", output)

    def test_a_copy_in_another_folder_counts_once(self):
        shutil.copy(self.event / "akit_26-03-07_10-00-00_wimil_q3.wpilog",
                    self.practice / "copied.wpilog")
        self.build(self.tmp / "practice", self.tmp / "events")
        self.assertEqual(len(self.names()), 2)

    def test_several_folders_need_a_named_index(self):
        result = run(LOG_INDEX, "build", self.tmp / "practice", self.tmp / "events",
                     check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--index", result.stderr)

    def test_list_needs_only_the_index(self):
        self.build(self.tmp / "practice")
        self.assertIn("1 logs in", run(LOG_INDEX, "list", "--index", self.db).stdout)


class VersionTest(unittest.TestCase):
    """An index is rebuilt across format changes, and nothing else is touched."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = self.tmp / "index.sqlite"

    def test_another_version_is_refused_for_reading(self):
        LogIndex.open_for_build(str(self.db))[0].close()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        with self.assertRaises(IndexVersionError):
            LogIndex(str(self.db))

    def test_another_version_is_rebuilt_for_building(self):
        LogIndex.open_for_build(str(self.db))[0].close()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        index, rebuilt = LogIndex.open_for_build(str(self.db))
        index.close()
        self.assertIn("rebuilt", rebuilt)
        self.assertEqual(log_index.index_version(str(self.db)), log_index.SCHEMA_VERSION)

    def test_a_file_that_is_not_an_index_is_never_replaced(self):
        for content in (b"WPILOG not an index", b""):
            with self.subTest(content=content):
                self.db.write_bytes(content)
                with self.assertRaises(NotAnIndexError):
                    LogIndex.open_for_build(str(self.db))
                self.assertEqual(self.db.read_bytes(), content)

    def test_rebuild_refuses_a_file_that_is_not_an_index(self):
        logs = self.tmp / "logs"
        logs.mkdir()
        write_match_log(logs / "a.wpilog")
        self.db.write_bytes(b"precious")
        result = run(LOG_INDEX, "build", logs, "--index", self.db, "--rebuild", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a log index", result.stderr)
        self.assertEqual(self.db.read_bytes(), b"precious")


class LogNameTest(unittest.TestCase):

    def test_names(self):
        cases = {
            "akit_26-05-01_22-45-20_johnson_q121.wpilog": ("johnson", "q121"),
            "akit_26-04-30_16-35-17_johnson.wpilog": ("johnson", None),
            "akit_26-05-01_20-28-19_q105.wpilog": (None, "q105"),
            "akit_26-04-29_21-55-44.wpilog": (None, None),
            "akit_25-04-19_09-36-19_curie_e6.wpilog": ("curie", "e6"),
            "FRC_20260501_133809_WIMIL_Q12.wpilog": ("WIMIL", "Q12"),
            "akit_26-10-20_00-57-11_wimil.truncated.wpilog": ("wimil", None),
            "akit_3f9a2c.wpilog": (None, None),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_log_name(name), expected)


if __name__ == "__main__":
    unittest.main()
