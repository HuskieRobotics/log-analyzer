#! /usr/bin/env python3
"""Unit tests for threshold and duration checks.

Peak alone does not answer "is this motor too hot": a motor touching 71 C for two
seconds is fine, sitting at 62 C for four minutes is not. These pin the exposure
measurement, and the parts of it that are easy to get wrong.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Log import Log  # noqa: E402
from analysis import compute_checks, find_excursions, overlap_seconds  # noqa: E402


def log_with(samples, enabled=((0.0, True),)):
    log = Log()
    for timestamp, value in enabled:
        log.put_boolean("/DriverStation/Enabled", timestamp, value)
    for timestamp, value in samples:
        log.put_number("/Motor/Temp", timestamp, value)
    return log


def rule(**extra):
    base = {"name": "Too hot", "entry": "/Motor/Temp",
            "expect": {"above": 60}, "severity": "warning"}
    base.update(extra)
    return [base]


def only_finding(log, rules):
    findings = compute_checks(log, "a.wpilog", rules).findings
    return findings[0] if findings else None


class ExcursionTest(unittest.TestCase):
    """find_excursions in isolation."""

    def test_a_value_that_never_crosses_yields_nothing(self):
        stamps, values = [0.0, 10.0, 20.0], [50.0, 55.0, 59.0]
        self.assertEqual(find_excursions(stamps, values, 60, 60, True, 30.0), [])

    def test_duration_runs_to_the_sample_that_clears(self):
        """A sample holds until the next one; these entries log on change."""
        stamps, values = [0.0, 10.0, 40.0], [50.0, 70.0, 50.0]
        found = find_excursions(stamps, values, 60, 60, True, 50.0)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].start, found[0].end), (10.0, 40.0))

    def test_peak_and_its_timestamp_are_kept(self):
        stamps, values = [0.0, 10.0, 20.0, 30.0], [50.0, 65.0, 71.0, 50.0]
        found = find_excursions(stamps, values, 60, 60, True, 40.0)
        self.assertEqual((found[0].peak, found[0].peak_at), (71.0, 20.0))

    def test_hysteresis_keeps_a_wobble_as_one_spell(self):
        """Resting on the limit otherwise produces a burst of one-sample spells,
        exactly when the reading is most marginal."""
        stamps = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0]
        values = [50.0, 61.0, 59.0, 62.0, 58.0, 61.0]
        without = find_excursions(stamps, values, 60, 60, True, 60.0)
        with_hysteresis = find_excursions(stamps, values, 60, 55, True, 60.0)
        self.assertEqual(len(without), 3)
        self.assertEqual(len(with_hysteresis), 1)

    def test_an_open_spell_at_end_of_log_is_marked_unresolved(self):
        stamps, values = [0.0, 10.0], [50.0, 70.0]
        found = find_excursions(stamps, values, 60, 60, True, 100.0)
        self.assertTrue(found[0].unresolved)
        self.assertEqual(found[0].end, 100.0)

    def test_below_is_the_mirror_image(self):
        stamps, values = [0.0, 10.0, 30.0], [12.5, 11.0, 12.4]
        found = find_excursions(stamps, values, 11.5, 11.5, False, 40.0)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].peak, 11.0)

    def test_non_numeric_samples_are_ignored(self):
        stamps, values = [0.0, 10.0, 20.0], ["hot", 70.0, "cold"]
        found = find_excursions(stamps, values, 60, 60, True, 30.0)
        self.assertEqual(len(found), 1)


class OverlapTest(unittest.TestCase):

    def test_full_and_partial_and_none(self):
        self.assertEqual(overlap_seconds(0, 10, [(0, 10)]), 10)
        self.assertEqual(overlap_seconds(0, 10, [(5, 20)]), 5)
        self.assertEqual(overlap_seconds(0, 10, [(20, 30)]), 0)

    def test_several_windows_sum(self):
        self.assertEqual(overlap_seconds(0, 100, [(0, 10), (50, 60)]), 20)


class GateClippingTest(unittest.TestCase):
    """The gate must clip durations, not filter samples."""

    def setUp(self):
        # Hot throughout; the robot is disabled for 20 s in the middle.
        self.log = log_with(
            [(0.0, 50.0), (10.0, 70.0), (100.0, 50.0)],
            enabled=((0.0, True), (40.0, False), (60.0, True), (100.0, False)))

    def test_it_stays_one_spell_across_the_disable(self):
        finding = only_finding(self.log, rule(**{"while": "enabled"}))
        self.assertEqual(finding.occurrences, 1,
                         "filtering samples would have split this into two")

    def test_the_disabled_time_is_not_counted(self):
        gated = only_finding(self.log, rule(**{"while": "enabled"}))
        ungated = only_finding(self.log, rule())
        self.assertIn("for 70.0 s", gated.detail)    # 90 s hot, minus 20 disabled
        self.assertIn("for 90.0 s", ungated.detail)


class ThresholdRuleTest(unittest.TestCase):

    def test_min_duration_drops_a_blip(self):
        log = log_with([(0.0, 50.0), (10.0, 70.0), (11.0, 50.0)])
        self.assertIsNone(only_finding(log, rule(minDuration=5.0)))
        self.assertIsNotNone(only_finding(log, rule(minDuration=0.5)))

    def test_the_detail_reports_exposure_not_just_peak(self):
        log = log_with([(0.0, 50.0), (10.0, 71.0), (52.3, 50.0)])
        detail = only_finding(log, rule(unit="C")).detail
        self.assertIn("above 60 C for 42.3 s", detail)
        self.assertIn("peak 71 C at 10.0 s", detail)

    def test_several_spells_are_summarised(self):
        log = log_with([(0.0, 50.0), (10.0, 70.0), (20.0, 50.0),
                        (30.0, 65.0), (60.0, 50.0)])
        detail = only_finding(log, rule()).detail
        self.assertIn("across 2 intervals", detail)
        self.assertIn("longest 30.0 s", detail)

    def test_a_single_spell_does_not_repeat_itself(self):
        log = log_with([(0.0, 50.0), (10.0, 70.0), (20.0, 50.0)])
        detail = only_finding(log, rule()).detail
        self.assertNotIn("interval", detail)
        self.assertNotIn("longest", detail)

    def test_still_hot_at_end_of_log_is_called_out(self):
        """The spell never closed, so the duration reported is a lower bound."""
        log = log_with([(0.0, 50.0), (10.0, 70.0)],
                       enabled=((0.0, True), (60.0, False)))
        detail = only_finding(log, rule()).detail
        self.assertIn("still past the limit when the log ended", detail)
        self.assertIn("for 50.0 s", detail)

    def test_crossing_on_the_very_last_sample_is_still_reported(self):
        """Zero measurable duration, but the robot finished past the limit."""
        log = log_with([(0.0, 50.0), (10.0, 70.0)])
        finding = only_finding(log, rule())
        self.assertIsNotNone(finding, "ending hot must not be silently dropped")
        self.assertIn("still past the limit when the log ended", finding.detail)

    def test_a_spell_entirely_outside_the_gate_is_dropped(self):
        log = log_with([(0.0, 50.0), (10.0, 70.0), (30.0, 50.0), (60.0, 50.0)],
                       enabled=((0.0, False), (40.0, True), (60.0, False)))
        self.assertIsNone(only_finding(log, rule(**{"while": "enabled"})))

    def test_a_cool_motor_reports_nothing(self):
        log = log_with([(0.0, 20.0), (10.0, 35.0), (20.0, 40.0)])
        self.assertIsNone(only_finding(log, rule()))

    def test_wildcards_give_one_finding_per_motor(self):
        log = Log()
        log.put_boolean("/DriverStation/Enabled", 0.0, True)
        for corner, peak in (("FL", 70.0), ("FR", 50.0), ("BL", 65.0)):
            log.put_number(f"/Drivetrain/{corner}/DriveTemp", 0.0, 30.0)
            log.put_number(f"/Drivetrain/{corner}/DriveTemp", 10.0, peak)
            log.put_number(f"/Drivetrain/{corner}/DriveTemp", 20.0, 30.0)
        findings = compute_checks(log, "a.wpilog", [
            {"name": "Hot", "entry": "/Drivetrain/*/DriveTemp",
             "expect": {"above": 60}, "severity": "warning"}]).findings
        self.assertEqual(sorted(f.entry.split("/")[2] for f in findings), ["BL", "FL"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
