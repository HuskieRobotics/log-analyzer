#! /usr/bin/env python3
"""Tests for the config linter (config_lint.py).

Each mistake the linter exists for produced a clean run with empty results when
it happened for real (claude/ROADMAP.md §12.1). These tests make each one on
purpose against small synthetic logs, and check that a correct config raises
nothing that would make people learn to ignore the linter.
"""

import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import config_lint  # noqa: E402
from config_lint import config_warnings, lint_config  # noqa: E402
from log_index import LogIndex, Manifest  # noqa: E402
from test_log_index import (CYCLE, FILTERED_CONFIG, SECOND, STATE_CONFIG,  # noqa: E402
                            write_match_log)
from wpilog_writer import WpilogWriter  # noqa: E402

ANALYSIS = REPO_ROOT / "analysis.py"


def write_auto_only_log(path):
    """A match in which /Auto/Progress is written only during auto, and
    /Intake/Stalled only once, at boot."""
    w = WpilogWriter()
    enabled = w.start("/DriverStation/Enabled", "boolean")
    auto = w.start("/DriverStation/Autonomous", "boolean")
    fms = w.start("/DriverStation/FMSAttached", "boolean")
    progress = w.start("/Auto/Progress", "double")
    stalled = w.start("/Intake/Stalled", "boolean")
    w.boolean(enabled, 0, False)
    w.boolean(auto, 0, False)
    w.boolean(fms, 0, True)
    w.boolean(stalled, 0, False)
    w.boolean(enabled, 1 * SECOND, True)
    w.boolean(auto, 1 * SECOND, True)
    for ts in range(1 * SECOND, 3 * SECOND, CYCLE):
        w.double(progress, ts, ts / SECOND)
    w.boolean(auto, 3 * SECOND, False)
    w.boolean(enabled, 6 * SECOND, False)
    return w.save(path)


class LintTest(unittest.TestCase):
    """Two synthetic matches and an auto-only log, indexed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.logs = cls.tmp / "logs"
        cls.logs.mkdir()
        cls.paths = [
            write_match_log(cls.logs / "akit_26-04-30_14-46-03_johnson_q12.wpilog"),
            write_match_log(cls.logs / "akit_26-04-30_16-20-07_johnson_q22.wpilog",
                            roller_offset=8.0),
            write_auto_only_log(cls.logs / "akit_26-04-30_18-00-00_johnson_q30.wpilog"),
        ]
        cls.db = cls.tmp / "index.sqlite"
        index = LogIndex(str(cls.db), create=True)
        for path in cls.paths:
            index.add(str(path))
        index.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.index = LogIndex(str(self.db))
        self.addCleanup(self.index.close)
        ids = [self.index.file_id_for(str(path)) for path in self.paths]
        self.manifest = Manifest(self.index, ids)

    def lint(self, config):
        return lint_config(config, self.manifest)

    def only(self, config, severity):
        return [p for p in self.lint(config) if p.severity == severity]

    # --- a correct config is quiet ------------------------------------------------

    def test_correct_configs_raise_no_errors_or_warnings(self):
        for config in (STATE_CONFIG, FILTERED_CONFIG):
            problems = [p for p in self.lint(config) if p.severity != "note"]
            self.assertEqual(problems, [], config)

    # --- the three failures of §12.1 ----------------------------------------------

    def test_a_string_against_a_number_is_never_equal(self):
        config = {"timeAnalysis": [{"startEntry": "/Vision/A/UpdatePoseCount",
                                    "startValue": "12", "endEntry": "/RealOutputs/Mode",
                                    "endValue": "SHOOT"}]}
        [error] = self.only(config, "error")
        self.assertEqual(error.where, "timeAnalysis[0].startValue")
        self.assertIn("never equal", error.message)

    def test_a_number_against_a_string_is_never_equal(self):
        config = {"valueAnalysis": [{"entry": "/Intake/RollerTemp",
                                     "triggerEntry": "/RealOutputs/Mode", "triggerValue": 3,
                                     "calculations": [{"type": "max"}]}]}
        [error] = self.only(config, "error")
        self.assertEqual(error.where, "valueAnalysis[0].triggerValue")

    def test_a_boolean_against_a_number_is_allowed(self):
        # Python compares True and 1 as equal, so this does match.
        config = {"checks": [{"entry": "/Vision/A/UpdatePoseCount",
                              "expect": {"never": True}}]}
        self.assertEqual(self.only(config, "error"), [])

    def test_an_entry_recorded_only_in_auto_under_a_teleop_filter(self):
        config = {"enabled": True, "robotMode": "teleop",
                  "valueAnalysis": [{"entry": "/Auto/Progress",
                                     "calculations": [{"type": "max"}]}]}
        [error] = self.only(config, "error")
        self.assertIn("only ever recorded while in auto", error.message)
        self.assertIn("robotMode teleop", error.message)

    def test_the_same_entry_is_fine_without_the_filter(self):
        config = {"enabled": True, "robotMode": "both",
                  "valueAnalysis": [{"entry": "/Auto/Progress",
                                     "calculations": [{"type": "max"}]}]}
        self.assertEqual(self.only(config, "error"), [])

    def test_a_value_written_once_at_boot_is_a_note_not_an_error(self):
        # AdvantageKit writes only changes: one record before enabling means the
        # value held all match. Nothing happened to analyse; not a mistake.
        config = {"enabled": True,
                  "timeAnalysis": [{"startEntry": "/Intake/Stalled", "startValue": True,
                                    "endEntry": "/Intake/Stalled", "endValue": False}]}
        self.assertEqual(self.only(config, "error"), [])
        notes = self.only(config, "note")
        self.assertTrue(any("never changed" in n.message for n in notes), notes)

    def test_a_misspelt_entry_suggests_the_real_one(self):
        config = {"checks": [{"entry": "/Intake/RollerTmp", "expect": {"above": 40}}]}
        [error] = self.only(config, "error")
        self.assertIn("matches no entry in any of the 3 logs", error.message)
        self.assertIn("'/Intake/RollerTemp'", error.message)

    # --- the rest ---------------------------------------------------------------

    def test_a_misspelt_key_is_a_warning_with_a_suggestion(self):
        config = {"enabeld": True,
                  "timeAnalysis": [{"startEntry": "/RealOutputs/Mode", "startValeu": "INTAKE",
                                    "endEntry": "/RealOutputs/Mode", "endValue": "SHOOT"}]}
        warnings = self.only(config, "warning")
        self.assertEqual({w.where for w in warnings},
                         {"config.enabeld", "timeAnalysis[0].startValeu"})
        self.assertTrue(all("did you mean" in w.message for w in warnings))

    def test_a_threshold_on_text_is_an_error(self):
        config = {"checks": [{"entry": "/RealOutputs/Mode", "expect": {"above": 3}}]}
        [error] = self.only(config, "error")
        self.assertIn("needs numbers", error.message)

    def test_a_threshold_on_a_struct_leaf_is_not_judged(self):
        # The manifest knows a leaf exists, not its type.
        config = {"checks": [{"entry": "/Intake/Motor/temp", "expect": {"above": 3}}]}
        self.assertEqual(self.only(config, "error"), [])

    def test_a_prefix_on_a_number_is_an_error(self):
        config = {"checks": [{"entry": "/Intake/RollerTemp",
                              "expect": {"alwaysStartsWith": "event-"}}]}
        self.assertEqual(len(self.only(config, "error")), 1)

    def test_an_expected_wildcard_match_that_never_appears(self):
        config = {"checks": [{"entry": "/Vision/*/UpdatePoseCount",
                              "expectEntries": ["A", "B"], "expect": {"minIncrease": 1}}]}
        [error] = self.only(config, "error")
        self.assertIn("'/Vision/B/UpdatePoseCount' is in none of the logs", error.message)

    def test_siblings_need_at_least_two(self):
        config = {"checks": [{"entry": "/Intake/Roller*", "compare": "siblings",
                              "statistic": "peak", "deviation": {"aboveSiblingsBy": 5}}]}
        [error] = self.only(config, "error")
        self.assertIn("needs at least 2", error.message)

    def test_unknown_calculation_gate_and_severity_are_warnings(self):
        config = {"valueAnalysis": [{"entry": "/Intake/RollerTemp",
                                     "calculations": [{"type": "median"}]}],
                  "checks": [{"entry": "/RealOutputs/Mode", "expect": "empty",
                              "while": "enabeled", "severity": "fatal"}]}
        wheres = {w.where for w in self.only(config, "warning")}
        self.assertEqual(wheres, {"valueAnalysis[0].calculations[0].type",
                                  "checks[0].while", "checks[0].severity"})

    def test_an_entry_in_some_logs_is_a_note(self):
        config = {"checks": [{"entry": "/Auto/Progress", "expect": {"below": 100}}]}
        notes = self.only(config, "note")
        self.assertTrue(any("is in 1 of 3 logs" in n.message for n in notes))

    def test_a_note_about_an_entry_is_said_once(self):
        config = {"timeAnalysis": [{"startEntry": "/Auto/Progress", "startValue": 1.0,
                                    "endEntry": "/Auto/Progress", "endValue": 2.0}]}
        self.assertEqual(len(self.only(config, "note")), 1)

    # --- before an analysis ---------------------------------------------------------

    def test_analysis_lines_carry_errors_and_warnings_but_not_notes(self):
        config = {"checks": [{"entry": "/Intake/RollerTmp", "expect": {"above": 40}},
                             {"entry": "/Auto/Progress", "expect": {"below": 100}}]}
        lines = config_warnings(config, self.index, [str(p) for p in self.paths])
        self.assertTrue(lines[0].startswith("Config check: 1 error, 0 warnings"))
        self.assertEqual(len(lines), 2)

    def test_analysis_lines_are_silent_until_every_log_is_indexed(self):
        extra = self.tmp / "unindexed.wpilog"
        write_match_log(extra, roller_offset=3.0)
        config = {"checks": [{"entry": "/Intake/RollerTmp", "expect": {"above": 40}}]}
        logs = [str(p) for p in self.paths] + [str(extra)]
        self.assertEqual(config_warnings(config, self.index, logs), [])


class CommandLineTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        write_match_log(self.logs / "akit_26-04-30_14-46-03_johnson_q12.wpilog")

    def lint(self, config):
        path = self.tmp / "config.json"
        path.write_text(json.dumps(config))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = config_lint.main([str(path), str(self.logs)])
        return code, out.getvalue()

    def test_indexes_the_logs_and_exits_zero_when_clean(self):
        code, out = self.lint(STATE_CONFIG)
        self.assertEqual(code, 0, out)
        self.assertIn("against 1 logs", out)
        self.assertIn("No problems found", out)
        self.assertTrue((self.logs / ".log-index.sqlite").exists())

    def test_exits_one_on_an_error(self):
        config = copy.deepcopy(STATE_CONFIG)
        config["checks"][0]["entry"] = "/Shooter/FlywheelConected"
        code, out = self.lint(config)
        self.assertEqual(code, 1)
        self.assertIn("1 error", out)

    def test_analysis_with_an_index_reports_the_config_on_stderr(self):
        config = {"checks": [{"name": "Typo", "entry": "/Intake/RollerTmp",
                              "expect": {"above": 40}}]}
        path = self.tmp / "config.json"
        path.write_text(json.dumps(config))
        db = self.tmp / "index.sqlite"
        args = [sys.executable, str(ANALYSIS), str(self.logs), str(path), "--index", str(db)]
        subprocess.run(args, capture_output=True, text=True, check=True, timeout=120)
        second = subprocess.run(args, capture_output=True, text=True, check=True, timeout=120)
        self.assertIn("Config check: 1 error", second.stderr)
        self.assertNotIn("Config check", second.stdout)


if __name__ == "__main__":
    unittest.main()
