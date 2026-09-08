#! /usr/bin/env python3
"""Unit tests for the check rules, including absence checks.

No .wpilog fixtures: the Log objects are built directly, so these run in
milliseconds and cover the cases the real logs happen not to contain.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Log import Log  # noqa: E402
from analysis import (  # noqa: E402
    compute_checks,
    format_check_findings,
    merge_check_findings,
)


def log_with_enabled(enabled_at):
    """A Log carrying DriverStation/Enabled transitions."""
    log = Log()
    for timestamp, value in enabled_at:
        log.put_boolean("/DriverStation/Enabled", timestamp, value)
    return log


class ExpectEmptyTest(unittest.TestCase):
    """Array entries such as AdvantageKit's Alerts."""

    def setUp(self):
        self.log = log_with_enabled([(0.0, True)])
        for timestamp, alerts in [
            (1.0, []),
            (2.0, ["camera BR disconnected"]),
            (3.0, ["camera BR disconnected", "brownout"]),
            (4.0, ["camera BR disconnected"]),
        ]:
            self.log.put_string_array("/Alerts/errors", timestamp, alerts)
        self.rule = [{"name": "Alert", "entry": "/Alerts/errors",
                      "expect": "empty", "severity": "error"}]

    def test_each_message_is_its_own_finding(self):
        findings = compute_checks(self.log, "a.wpilog", self.rule).findings
        self.assertEqual({f.detail for f in findings},
                         {"camera BR disconnected", "brownout"})

    def test_repeats_collapse_with_a_count_and_first_timestamp(self):
        findings = {f.detail: f for f in compute_checks(self.log, "a.wpilog", self.rule).findings}
        self.assertEqual(findings["camera BR disconnected"].occurrences, 3)
        self.assertEqual(findings["camera BR disconnected"].timestamp, 2.0)
        self.assertEqual(findings["brownout"].occurrences, 1)

    def test_empty_arrays_produce_nothing(self):
        log = log_with_enabled([(0.0, True)])
        log.put_string_array("/Alerts/errors", 1.0, [])
        self.assertEqual(compute_checks(log, "a.wpilog", self.rule).findings, [])


class ExpectAlwaysTest(unittest.TestCase):

    def rule(self, **extra):
        base = {"name": "Connected", "entry": "/Intake/RollerConnectedLead",
                "expect": {"always": True}, "severity": "error"}
        base.update(extra)
        return [base]

    def test_a_deviation_is_reported(self):
        log = log_with_enabled([(0.0, True)])
        log.put_boolean("/Intake/RollerConnectedLead", 1.0, True)
        log.put_boolean("/Intake/RollerConnectedLead", 2.0, False)
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].timestamp, 2.0)
        self.assertIn("expected True", findings[0].detail)

    def test_holding_the_expected_value_reports_nothing(self):
        log = log_with_enabled([(0.0, True)])
        log.put_boolean("/Intake/RollerConnectedLead", 1.0, True)
        self.assertEqual(compute_checks(log, "a.wpilog", self.rule()).findings, [])

    def test_while_enabled_ignores_samples_taken_while_disabled(self):
        log = log_with_enabled([(0.0, False), (10.0, True)])
        log.put_boolean("/Intake/RollerConnectedLead", 1.0, False)   # disabled
        log.put_boolean("/Intake/RollerConnectedLead", 11.0, False)  # enabled
        findings = compute_checks(log, "a.wpilog", self.rule(**{"while": "enabled"})).findings
        self.assertEqual([f.timestamp for f in findings], [11.0])

    def test_without_a_gate_both_samples_count(self):
        log = log_with_enabled([(0.0, False), (10.0, True)])
        log.put_boolean("/Intake/RollerConnectedLead", 1.0, False)
        log.put_boolean("/Intake/RollerConnectedLead", 11.0, False)
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        self.assertEqual(findings[0].occurrences, 2)


class AbsenceTest(unittest.TestCase):
    """The half that scanning cannot express: what did not happen."""

    def test_expected_entry_missing_is_a_finding(self):
        log = log_with_enabled([(0.0, True)])
        rule = [{"name": "Camera never reported",
                 "entry": "/Vision/BCL/sending frames",
                 "expect": "present", "severity": "warning"}]
        findings = compute_checks(log, "a.wpilog", rule).findings
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].detail, "entry not present in this log")
        self.assertIsNone(findings[0].timestamp)

    def test_present_entry_reports_nothing(self):
        log = log_with_enabled([(0.0, True)])
        log.put_boolean("/Vision/BCL/sending frames", 1.0, True)
        rule = [{"name": "Camera never reported",
                 "entry": "/Vision/BCL/sending frames", "expect": "present"}]
        self.assertEqual(compute_checks(log, "a.wpilog", rule).findings, [])

    def test_never_reaching_a_state_is_a_finding(self):
        log = log_with_enabled([(0.0, True)])
        for timestamp in (1.0, 2.0):
            log.put_boolean("/Shooter/Armed", timestamp, False)
        rule = [{"name": "Shooter never armed", "entry": "/Shooter/Armed",
                 "expect": {"atLeastOnce": True}, "severity": "warning"}]
        findings = compute_checks(log, "a.wpilog", rule).findings
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].detail, "never reached True")

    def test_reaching_the_state_reports_nothing(self):
        log = log_with_enabled([(0.0, True)])
        log.put_boolean("/Shooter/Armed", 1.0, False)
        log.put_boolean("/Shooter/Armed", 2.0, True)
        rule = [{"name": "Shooter never armed", "entry": "/Shooter/Armed",
                 "expect": {"atLeastOnce": True}}]
        self.assertEqual(compute_checks(log, "a.wpilog", rule).findings, [])

    def test_a_never_firing_flag_is_not_a_finding(self):
        """RollerStalled staying false is the healthy case, not a concern."""
        log = log_with_enabled([(0.0, True)])
        log.put_boolean("/Intake/RollerStalled", 1.0, False)
        rule = [{"name": "Roller stalled", "entry": "/Intake/RollerStalled",
                 "expect": {"always": False}}]
        self.assertEqual(compute_checks(log, "a.wpilog", rule).findings, [])


class WildcardRuleTest(unittest.TestCase):

    def test_one_rule_covers_every_matching_entry(self):
        log = log_with_enabled([(0.0, True)])
        for camera, value in (("BCH", True), ("BCL", False), ("BR", False)):
            log.put_boolean(f"/RealOutputs/Vision/{camera}/sending frames", 1.0, value)
        rule = [{"name": "Camera stopped", "expect": {"always": True},
                 "entry": "/RealOutputs/Vision/*/sending frames", "severity": "error"}]
        findings = compute_checks(log, "a.wpilog", rule).findings
        self.assertEqual(sorted(f.entry for f in findings), [
            "/RealOutputs/Vision/BCL/sending frames",
            "/RealOutputs/Vision/BR/sending frames",
        ])


class MergeAndFormatTest(unittest.TestCase):

    def reports(self):
        reports = []
        for name in ("a.wpilog", "b.wpilog"):
            log = log_with_enabled([(0.0, True)])
            log.put_string_array("/Alerts/errors", 1.0, ["brownout"])
            reports.append(compute_checks(
                log, name,
                [{"name": "Alert", "entry": "/Alerts/errors",
                  "expect": "empty", "severity": "error"}]))
        return reports

    def test_the_same_concern_in_two_files_merges(self):
        merged = merge_check_findings(self.reports())
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].occurrences, 2)
        self.assertEqual(merged[0].file_count, 2)

    def test_findings_sort_most_severe_first(self):
        log = log_with_enabled([(0.0, True)])
        log.put_boolean("/A/Flag", 1.0, False)
        log.put_boolean("/B/Flag", 1.0, False)
        rules = [
            {"name": "Low", "entry": "/A/Flag", "expect": {"always": True}, "severity": "info"},
            {"name": "High", "entry": "/B/Flag", "expect": {"always": True}, "severity": "error"},
        ]
        findings = compute_checks(log, "a.wpilog", rules).findings
        self.assertEqual([f.severity for f in
                          sorted(findings, key=lambda f: f.severity != "error")][0], "error")
        lines = format_check_findings(findings, is_aggregate=False)
        self.assertTrue(lines[0].startswith("  [ERROR]"))

    def test_no_findings_says_so(self):
        self.assertEqual(format_check_findings([], is_aggregate=False),
                         ["  No concerns found."])

    def test_per_file_rendering_omits_the_file_name(self):
        findings = self.reports()[0].findings
        lines = format_check_findings(findings, is_aggregate=False)
        self.assertNotIn("a.wpilog", "\n".join(lines))
        self.assertIn("a.wpilog", "\n".join(format_check_findings(findings, is_aggregate=True)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
