#! /usr/bin/env python3
"""Tests for season.py: finding events and measuring per match from the index.

The synthetic match (tests/test_log_index.py, write_match_log) has a known
timeline, so every measure here is checked against a value worked out by hand:
enabled 2-15 s with auto until 5 s; the flywheel disconnects at 6 s for half a
second and at 14 s for good; an alert is raised at 7 s and cleared at 7.5 s; the
mode string changes at 6, 8, 9, 11.5 and 12 s; a pose counter counts up by one
every 0.2 s until 12 s.
"""

import contextlib
import io
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import season  # noqa: E402
from log_index import LogIndex  # noqa: E402
from season import (Reader, Timeline, find, make_condition, measure,  # noqa: E402
                    select_logs, summary_rows)
from test_log_index import write_match_log  # noqa: E402

Q12 = "akit_26-04-30_14-46-03_johnson_q12.wpilog"
Q22 = "akit_26-04-30_16-20-07_johnson_q22.wpilog"
Q36_FIRST = "akit_26-04-30_19-14-09_johnson_q36.wpilog"
Q36_SECOND = "akit_26-04-30_19-18-09_johnson_q36.wpilog"
NO_CODE = "akit_26-04-30_20-00-00_q50.wpilog"      # an event log missing its code
PRACTICE = "akit_26-04-29_12-00-00_johnson_p3.wpilog"
PIT = "akit_26-04-30_09-00-00_johnson.wpilog"
HOME = "akit_26-04-20_18-00-00.wpilog"


class SeasonTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        event = cls.tmp / "logs" / "Championship"
        home = cls.tmp / "logs" / "Home"
        event.mkdir(parents=True)
        home.mkdir(parents=True)
        # Each log differs a little, so each is its own file to the index.
        for offset, name in enumerate((Q12, Q22, Q36_FIRST, Q36_SECOND, NO_CODE, PRACTICE)):
            write_match_log(event / name, roller_offset=float(offset))
        write_match_log(event / PIT, roller_offset=10.0, fms_attached=False)
        write_match_log(home / HOME, roller_offset=11.0, fms_attached=False)
        cls.db = cls.tmp / "index.sqlite"
        index = LogIndex(str(cls.db), create=True)
        for path in sorted((cls.tmp / "logs").rglob("*.wpilog")):
            index.add(str(path))
        index.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.index = LogIndex(str(self.db))
        self.addCleanup(self.index.close)

    def official(self):
        return select_logs(self.index, "official")

    def per_match(self, kind, entry, **options):
        result = measure(self.index, self.official(), kind, entry,
                         reader=Reader(self.index, quiet=True), **options)
        return result, {key[1]: value.value for (_name, key), value in result.per_match.items()}

    # --- which logs ---------------------------------------------------------------

    def test_official_matches_leave_out_practice_pit_and_home(self):
        names = [log.name for log in self.official()]
        self.assertEqual(sorted(names), sorted([Q12, Q22, Q36_FIRST, Q36_SECOND, NO_CODE]))

    def test_a_log_without_an_event_code_takes_its_folders(self):
        [log] = [log for log in self.official() if log.name == NO_CODE]
        self.assertEqual(log.event, "johnson")

    def test_two_logs_of_one_match_are_one_match(self):
        keys = {log.match_key for log in self.official()}
        self.assertEqual(len(keys), 4)

    def test_scopes_and_events(self):
        self.assertEqual(len(select_logs(self.index, "matches")), 6)
        self.assertEqual(len(select_logs(self.index, "all")), 8)
        self.assertEqual(len(select_logs(self.index, "all", ["JOHNSON"])), 7)

    # --- measures from samples ------------------------------------------------------

    def test_increase_of_a_counter_while_enabled(self):
        # One per 0.2 s from 2.0 s (enabled) to 11.8 s: 50 per log.
        result, values = self.per_match("increase", "/Vision/A/UpdatePoseCount")
        self.assertEqual(values["q12"], 50)
        self.assertEqual(values["q36"], 100)  # two logs, one match

    def test_increase_only_in_auto(self):
        # 2.0 to 4.8 s: 15 steps.
        _result, values = self.per_match("increase", "/Vision/A/UpdatePoseCount",
                                         gate="auto")
        self.assertEqual(values["q12"], 15)

    def test_rises_and_time_of_a_condition(self):
        disconnected = make_condition(equals=False)
        _r, rises = self.per_match("rises", "/Shooter/FlywheelConnected",
                                   condition=disconnected)
        _r, seconds = self.per_match("time", "/Shooter/FlywheelConnected",
                                     condition=disconnected)
        self.assertEqual(rises["q12"], 2)
        self.assertAlmostEqual(seconds["q12"], 0.5 + 1.0, places=6)  # 6-6.5, 14-15

    def test_count_of_changes(self):
        _r, values = self.per_match("count", "/RealOutputs/Mode")
        self.assertEqual(values["q12"], 5)

    def test_sum_counts_the_elements_of_an_array(self):
        _r, values = self.per_match("sum", "/RealOutputs/Alerts/errors")
        self.assertEqual(values["q12"], 1)

    def test_a_struct_leaf_is_stored_once_then_read_from_the_index(self):
        logs = self.official()
        reader = Reader(self.index, quiet=True)
        result = measure(self.index, logs, "max", "/Intake/Motor/temp", reader=reader)
        self.assertFalse(result.from_statistics)
        self.assertEqual(len(reader.stored_now), 5)
        # temp + 1 for the struct, 25 + offset + 1.5 t, highest just before 15 s.
        q12 = result.per_match[("/Intake/Motor/temp", ("johnson", "q12"))].value
        self.assertAlmostEqual(q12, 26.0 + 1.5 * 14.98, places=6)
        again = Reader(self.index, quiet=True)
        measure(self.index, logs, "max", "/Intake/Motor/temp", reader=again)
        self.assertEqual(again.stored_now, [])

    def test_read_only_skips_what_is_not_stored(self):
        reader = Reader(self.index, read_only=True, quiet=True)
        # A count needs samples; a double's are not stored until asked for.
        result = measure(self.index, self.official(), "count", "/RealOutputs/Busy",
                         reader=reader)
        self.assertEqual(result.per_match, {})
        self.assertEqual(len(reader.unreadable), 5)

    # --- measures from statistics ---------------------------------------------------

    def test_statistics_answer_a_numeric_scalar(self):
        result, values = self.per_match("mean", "/Intake/RollerTemp")
        self.assertTrue(result.from_statistics)
        # 25 + 1.5 t held over 2-15 s, sampled every 20 ms: the left-hand mean.
        expected = 25.0 + 1.5 * (2.0 + 15.0 - 0.02) / 2
        self.assertAlmostEqual(values["q12"], expected, places=3)

    def test_statistics_and_samples_agree(self):
        stats, from_stats = self.per_match("integral", "/Intake/RollerTemp")
        self.assertTrue(stats.from_statistics)
        log_info = [log for log in self.official() if log.name == Q12][0]
        log = Reader(self.index, quiet=True).load(log_info, "/Intake/RollerTemp")
        data = log.get_field("/Intake/RollerTemp").data
        from_samples = season.measure_samples(
            "integral", data.timestamps, data.values,
            Timeline(log, season.log_end(log, log_info)), "enabled", make_condition())
        self.assertAlmostEqual(from_stats["q12"], from_samples.value, places=3)

    def test_combined_entries_add_up(self):
        result = measure(self.index, self.official(), "integral", "/Intake/Roller*",
                         reader=Reader(self.index, quiet=True), combined=True)
        _r, alone = self.per_match("integral", "/Intake/RollerTemp")
        [(name, _key)] = [k for k in result.per_match if k[1][1] == "q12"]
        self.assertEqual(name, "/Intake/Roller*")
        self.assertAlmostEqual(result.per_match[(name, ("johnson", "q12"))].value,
                               alone["q12"])

    # --- per match and per event ----------------------------------------------------

    def test_an_event_row_gives_total_per_match_and_extremes(self):
        result, _values = self.per_match("increase", "/Vision/A/UpdatePoseCount")
        [row] = summary_rows(result, "event")
        self.assertEqual((row["group"], row["matches"], row["total"]), ("johnson", 4, 250))
        self.assertEqual((row["per_match"], row["lowest"], row["highest"]), (62.5, 50, 100))

    def test_a_weighted_measure_has_no_total(self):
        result, _values = self.per_match("mean", "/Intake/RollerTemp")
        [row] = summary_rows(result, "season")
        self.assertIsNone(row["total"])
        self.assertIsNotNone(row["overall"])

    # --- finding --------------------------------------------------------------------

    def test_find_reports_each_spell_with_its_time_and_mode(self):
        episodes = find(self.index, select_logs(self.index, "all"),
                        "/RealOutputs/Alerts/errors", make_condition(contains="JAMMED"),
                        reader=Reader(self.index, quiet=True))
        self.assertEqual(len(episodes), 8)
        first = [e for e in episodes if e.log.name == Q12][0]
        self.assertAlmostEqual(first.start, 7.0)
        self.assertAlmostEqual(first.seconds, 0.5)
        self.assertEqual(first.mode, "teleop")
        self.assertAlmostEqual(first.into_mode, 2.0)
        self.assertEqual(first.value, ["Roller jammed"])

    def test_find_within_a_gate(self):
        found = find(self.index, self.official(), "/Shooter/FlywheelConnected",
                     make_condition(equals=False), gate="auto",
                     reader=Reader(self.index, quiet=True))
        self.assertEqual(found, [])
        found = find(self.index, self.official(), "/Shooter/FlywheelConnected",
                     make_condition(equals=False), gate="enabled",
                     reader=Reader(self.index, quiet=True))
        self.assertEqual(len(found), 10)  # two per log

    def test_a_threshold_condition(self):
        found = find(self.index, self.official()[:1], "/Vision/A/UpdatePoseCount",
                     make_condition(above=40), reader=Reader(self.index, quiet=True))
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].start, 41 * 0.2)

    # --- command line ---------------------------------------------------------------

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = season.main([str(self.tmp / "logs"), *args, "--index", str(self.db)])
        return code, out.getvalue()

    def test_measure_prints_a_table_per_event(self):
        code, out = self.run_cli("measure", "increase", "/Vision/A/UpdatePoseCount")
        self.assertEqual(code, 0)
        self.assertIn("5 official match logs (4 matches, 1 events)", out)
        self.assertRegex(out, r"johnson\s+4\s+250\s+62\.5\s+50\s+100")

    def test_measure_as_json(self):
        code, out = self.run_cli("measure", "rises", "/Shooter/FlywheelConnected",
                                 "--equals", "false", "--by", "match", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)["rows"]
        self.assertEqual({row["match"]: row["value"] for row in rows},
                         {"q12": 2, "q22": 2, "q36": 4, "q50": 2})

    def test_find_on_the_command_line(self):
        code, out = self.run_cli("find", "/RealOutputs/Alerts/errors", "--contains", "jammed",
                                 "--logs", "official")
        self.assertEqual(code, 0)
        self.assertIn("5 found in 5 logs", out)
        self.assertIn("teleop +2.0s", out)

    def test_entries_lists_types_and_coverage(self):
        code, out = self.run_cli("entries", "/Intake/**")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"/Intake/RollerTemp\s+double\s+8/8")
        self.assertRegex(out, r"/Intake/Motor/temp\s+struct field\s+8/8")

    def test_entries_suggests_a_near_miss(self):
        code, out = self.run_cli("entries", "/Intake/RollerTmp")
        self.assertEqual(code, 1)
        self.assertIn("Did you mean: /Intake/RollerTemp", out)


if __name__ == "__main__":
    unittest.main()
