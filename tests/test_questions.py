#! /usr/bin/env python3
"""Tests for questions.py: keeping the team's questions, and checking them early.

The check exists to catch, in the first practice sessions, the two ways the 2026
logs failed questions asked at the end of the season: an entry never logged at
all, and an entry logged but never set (the power distribution panel's totals,
a drivetrain speed stuck at 0). A synthetic log carries one of each.
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import questions  # noqa: E402
from log_index import LogIndex, Manifest  # noqa: E402
from questions import (Question, check_question, load_questions, parse_ask,  # noqa: E402
                       recent_logs)
from season import Reader  # noqa: E402
from test_log_index import CYCLE, SECOND, write_match_log  # noqa: E402
from wpilog_writer import WpilogWriter  # noqa: E402


def write_practice_log(path, enable=True):
    """A practice session: a working counter and speed, a total never set (0
    throughout), and a boolean that held false (no brownout)."""
    w = WpilogWriter()
    enabled = w.start("/DriverStation/Enabled", "boolean")
    auto = w.start("/DriverStation/Autonomous", "boolean")
    distance = w.start("/Drivetrain/DistanceMeters", "double")
    energy = w.start("/PowerDistribution/TotalEnergy", "double")
    brownout = w.start("/SystemStats/BrownedOut", "boolean")
    w.boolean(enabled, 0, False)
    w.boolean(auto, 0, False)
    w.double(energy, 0, 0.0)
    w.boolean(brownout, 0, False)
    if enable:
        w.boolean(enabled, 1 * SECOND, True)
    for ts in range(0, 10 * SECOND, CYCLE):
        w.double(distance, ts, ts / SECOND * 2.0)  # 2 m/s
    w.boolean(enabled, 9 * SECOND, False)
    return w.save(path)


class QuestionsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.logs = cls.tmp / "logs"
        cls.logs.mkdir()
        write_match_log(cls.logs / "akit_26-04-30_14-46-03_johnson_q12.wpilog")
        write_practice_log(cls.logs / "akit_27-01-10_18-00-00.wpilog")
        write_practice_log(cls.logs / "akit_27-01-12_18-00-00.wpilog", enable=False)
        cls.db = cls.tmp / "index.sqlite"
        index = LogIndex(str(cls.db), create=True)
        for path in sorted(cls.logs.glob("*.wpilog")):
            index.add(str(path))
        index.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.index = LogIndex(str(self.db))
        self.addCleanup(self.index.close)

    def verdict(self, ask, latest=None, since=None):
        logs = recent_logs(self.index, latest, since)
        manifest = Manifest(self.index, [log.file_id for log in logs])
        question = Question("q", ask)
        question.args, question.problem = parse_ask(ask)
        return check_question(question, self.index, logs, manifest,
                              Reader(self.index, quiet=True))

    # --- parsing --------------------------------------------------------------------

    def test_an_ask_is_a_season_command(self):
        args, problem = parse_ask("measure increase '/Energy/*/Joules' --combine")
        self.assertIsNone(problem)
        self.assertEqual((args.command, args.kind, args.entry, args.combine),
                         ("measure", "increase", "/Energy/*/Joules", True))

    def test_a_bad_ask_says_why(self):
        _args, problem = parse_ask("measure median /X")
        self.assertIn("invalid choice: 'median'", problem)
        _args, problem = parse_ask("season.py ./logs measure max /X")
        self.assertIn("only what follows the log folder", problem)
        _args, problem = parse_ask("find '/X")
        self.assertIsNotNone(problem)

    def test_a_question_without_an_ask_is_invalid(self):
        path = self.tmp / "q.json"
        path.write_text(json.dumps({"questions": [{"question": "Why?"}]}))
        [question] = load_questions(str(path))
        self.assertEqual(question.problem, "has no \"ask\"")

    def test_the_shipped_question_files_parse(self):
        for name in ("questions2026.json", "questions2027.json"):
            for question in load_questions(str(REPO_ROOT / name)):
                self.assertIsNone(question.problem, f"{name}: {question.text}")

    # --- which logs -----------------------------------------------------------------

    def test_the_newest_logs_or_those_since_a_date(self):
        names = [log.name for log in recent_logs(self.index, 2, None)]
        self.assertEqual(names, ["akit_27-01-10_18-00-00.wpilog",
                                 "akit_27-01-12_18-00-00.wpilog"])
        self.assertEqual(len(recent_logs(self.index, None, "2027-01-11")), 1)
        self.assertEqual(len(recent_logs(self.index, None, None)), 3)

    # --- verdicts -------------------------------------------------------------------

    def test_a_logged_value_is_ready(self):
        verdict = self.verdict("measure increase /Drivetrain/DistanceMeters", latest=2)
        self.assertEqual(verdict.status, "ready", verdict.details)
        self.assertIn("in 2 of 2 logs", verdict.details)

    def test_an_entry_not_logged_is_missing_with_the_nearest_names(self):
        verdict = self.verdict("measure increase /Drivetrain/DistanceMeter", latest=2)
        self.assertEqual(verdict.status, "missing")
        self.assertIn("nearest: /Drivetrain/DistanceMeters", verdict.details[0])

    def test_a_number_that_never_changes_needs_checking(self):
        # The 2026 failure: present in every log, never set.
        verdict = self.verdict("measure increase /PowerDistribution/TotalEnergy", latest=2)
        self.assertEqual(verdict.status, "check")
        self.assertTrue(verdict.details[0].startswith("never changed"))

    def test_a_boolean_that_held_false_is_fine(self):
        # No brownout is good news, not missing logging.
        verdict = self.verdict("measure rises /SystemStats/BrownedOut", latest=2)
        self.assertEqual(verdict.status, "ready", verdict.details)

    def test_no_value_while_enabled_needs_checking(self):
        verdict = self.verdict("measure max /Drivetrain/DistanceMeters", latest=1)
        self.assertEqual(verdict.status, "check")
        self.assertIn("no value while enabled", verdict.details[0])

    def test_a_find_reports_how_often(self):
        verdict = self.verdict("find /RealOutputs/Alerts/errors --contains jammed")
        self.assertEqual(verdict.status, "ready")
        self.assertIn("found 1 time in these logs", verdict.details)

    # --- command line ---------------------------------------------------------------

    def run_cli(self, command, items, *extra):
        path = self.tmp / "questions.json"
        path.write_text(json.dumps({"questions": items}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = questions.main([command, str(path), str(self.logs), "--index",
                                   str(self.db), *extra])
        return code, out.getvalue()

    def test_check_exits_one_when_something_is_missing(self):
        code, out = self.run_cli("check", [
            {"question": "Distance", "ask": "measure increase /Drivetrain/DistanceMeters"},
            {"question": "Energy", "askedBy": "Geoff",
             "ask": "measure increase '/Energy/*/Joules'"},
        ], "--latest", "2")
        self.assertEqual(code, 1)
        self.assertIn("checked against the 2 newest logs (2027-01-10 to 2027-01-12)", out)
        self.assertIn("missing  Energy  (Geoff)", out)
        self.assertIn("1 missing, 1 ready", out)

    def test_check_exits_zero_with_only_things_to_check(self):
        code, out = self.run_cli("check", [
            {"question": "Energy", "ask": "measure increase /PowerDistribution/TotalEnergy"},
        ], "--since", "2027-01-01")
        self.assertEqual(code, 0)
        self.assertIn("1 check", out)

    def test_answer_runs_each_question(self):
        code, out = self.run_cli("answer", [
            {"question": "Pose updates", "ask": "measure increase /Vision/A/UpdatePoseCount"},
            {"question": "Broken", "ask": "measure median /X"},
        ])
        self.assertEqual(code, 1)
        self.assertIn("1. Pose updates", out)
        self.assertRegex(out, r"johnson\s+1\s+50")
        self.assertIn("2. Broken", out)
        self.assertIn("Cannot ask it", out)


if __name__ == "__main__":
    unittest.main()
