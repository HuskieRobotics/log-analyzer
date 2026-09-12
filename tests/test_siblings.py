#! /usr/bin/env python3
"""Unit tests for sibling comparison.

Peers share a match, so they share ambient temperature, how hard the robot was
driven and how long it was enabled. Comparing among them cancels those
confounders - which is why it resolves a smaller deviation than comparing an
entry against its own history, and needs no history at all.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Log import Log  # noqa: E402
from analysis import EnabledGate, compute_checks, match_statistic  # noqa: E402


def drivetrain(peaks, start=20.0, enabled=((0.0, True),)):
    """A log where each corner ramps from `start` to its given peak."""
    log = Log()
    for timestamp, value in enabled:
        log.put_boolean("/DriverStation/Enabled", timestamp, value)
    for corner, peak in peaks.items():
        log.put_number(f"/Drivetrain/{corner}/DriveTemp", 1.0, start)
        log.put_number(f"/Drivetrain/{corner}/DriveTemp", 50.0, peak)
        log.put_number(f"/Drivetrain/{corner}/DriveTemp", 100.0, peak - 1)
    return log


def rule(**extra):
    base = {"name": "Hot peer", "entry": "/Drivetrain/*/DriveTemp",
            "compare": "siblings", "statistic": "peak", "unit": "C",
            "deviation": {"aboveSiblingsBy": 5}, "severity": "warning"}
    base.update(extra)
    return [base]


def findings(log, rules):
    return compute_checks(log, "a.wpilog", rules).findings


class StatisticTest(unittest.TestCase):

    def setUp(self):
        self.log = drivetrain({"FL": 48.0})
        self.gate = EnabledGate(self.log)
        self.samples = self.log.get_field(
            "/Drivetrain/FL/DriveTemp").get_number(0.0, 200.0)

    def stat(self, name, gate="any"):
        return match_statistic(self.samples, name, self.gate, gate)

    def test_peak_min_mean_and_rise(self):
        self.assertEqual(self.stat("peak"), 48.0)
        self.assertEqual(self.stat("min"), 20.0)
        self.assertEqual(self.stat("rise"), 28.0)      # 48 peak - 20 start
        self.assertAlmostEqual(self.stat("mean"), (20.0 + 48.0 + 47.0) / 3)

    def test_an_unknown_statistic_is_not_invented(self):
        self.assertIsNone(self.stat("median"))

    def test_the_gate_restricts_which_samples_count(self):
        log = drivetrain({"FL": 48.0}, enabled=((0.0, False), (60.0, True)))
        gate = EnabledGate(log)
        samples = log.get_field(
            "/Drivetrain/FL/DriveTemp").get_number(0.0, 200.0)
        # Only the 100 s sample (47) falls inside the enabled window.
        self.assertEqual(match_statistic(samples, "peak", gate, "enabled"), 47.0)


class SiblingComparisonTest(unittest.TestCase):

    def test_peers_in_agreement_report_nothing(self):
        self.assertEqual(findings(drivetrain(
            {"FL": 46.0, "FR": 45.0, "BL": 45.0, "BR": 47.0}), rule()), [])

    def test_one_hot_corner_is_flagged(self):
        found = findings(drivetrain(
            {"FL": 45.0, "FR": 46.0, "BL": 45.0, "BR": 62.0}), rule())
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].entry.endswith("/BR/DriveTemp"))
        self.assertIn("17.0 C above its peers", found[0].detail)

    def test_the_detail_names_the_peers_it_compared_against(self):
        found = findings(drivetrain(
            {"FL": 45.0, "FR": 46.0, "BL": 45.0, "BR": 62.0}), rule())
        self.assertIn("median 45 C of BL, FL, FR", found[0].detail)

    def test_the_outlier_cannot_inflate_its_own_baseline(self):
        """The reference is the median of the *other* peers, so a single very
        hot corner is measured against the cool ones, not against itself."""
        found = findings(drivetrain(
            {"FL": 40.0, "FR": 40.0, "BL": 40.0, "BR": 90.0}), rule())
        self.assertIn("50.0 C above", found[0].detail)

    def test_two_hot_corners_are_both_flagged(self):
        found = findings(drivetrain(
            {"FL": 40.0, "FR": 40.0, "BL": 60.0, "BR": 60.0}), rule())
        self.assertEqual(sorted(f.entry.split("/")[2] for f in found), ["BL", "BR"])

    def test_a_cold_outlier_is_found_with_belowSiblingsBy(self):
        found = findings(drivetrain({"FL": 45.0, "FR": 46.0, "BL": 45.0, "BR": 20.0}),
                         rule(deviation={"belowSiblingsBy": 5}))
        self.assertEqual(len(found), 1)
        self.assertIn("below its peers", found[0].detail)

    def test_a_deviation_at_the_threshold_does_not_fire(self):
        found = findings(drivetrain(
            {"FL": 45.0, "FR": 45.0, "BL": 45.0, "BR": 50.0}), rule())
        self.assertEqual(found, [])

    def test_too_few_peers_is_reported_not_silent(self):
        """Silence would be indistinguishable from "all peers agree"."""
        found = findings(drivetrain({"FL": 45.0, "FR": 46.0}), rule())
        self.assertEqual(len(found), 1)
        self.assertIn("only 2 peer(s) with data", found[0].detail)

    def test_the_minimum_peer_count_is_configurable(self):
        self.assertEqual(findings(drivetrain({"FL": 45.0, "FR": 46.0}),
                                  rule(minimumSiblings=2)), [])

    def test_a_pattern_matching_nothing_is_silent_here(self):
        log = drivetrain({"FL": 45.0, "FR": 45.0, "BL": 45.0})
        self.assertEqual(findings(log, rule(entry="/Nope/*/Temp")), [])

    def test_rise_can_be_used_instead_of_peak(self):
        """Peers start together so rise adds nothing here (see roadmap 9.4), but
        it must still work when asked for."""
        log = Log()
        log.put_boolean("/DriverStation/Enabled", 0.0, True)
        # BR rises 50 C where its peers rise 25 C; note that by *peak* BR would
        # be only 15 C above, so the two statistics disagree here on purpose.
        for corner, (start, peak) in {"FL": (20.0, 45.0), "FR": (20.0, 46.0),
                                      "BL": (20.0, 45.0), "BR": (10.0, 60.0)}.items():
            log.put_number(f"/Drivetrain/{corner}/DriveTemp", 1.0, start)
            log.put_number(f"/Drivetrain/{corner}/DriveTemp", 50.0, peak)
        found = findings(log, rule(statistic="rise"))
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].entry.endswith("/BR/DriveTemp"))
        self.assertIn("rise 50", found[0].detail)

    def test_a_non_numeric_entry_is_skipped_not_crashed(self):
        log = Log()
        log.put_boolean("/DriverStation/Enabled", 0.0, True)
        for corner in ("FL", "FR", "BL"):
            log.put_string(f"/Drivetrain/{corner}/DriveTemp", 0.0, "warm")
        found = findings(log, rule())
        self.assertEqual(len(found), 1)
        self.assertIn("0 peer(s) with data", found[0].detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
