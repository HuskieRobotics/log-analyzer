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


class AfterFirstEnableGateTest(unittest.TestCase):
    """Boot noise recurs every match and hides real problems, but gating strictly
    on "enabled" discards sticky faults, which surface after the fact."""

    def rule(self, gate):
        return [{"name": "Alert", "entry": "/Alerts/errors", "expect": "empty",
                 "severity": "error", "while": gate}]

    def match_log(self):
        """Boot, then enabled, then disabled again - one match."""
        log = log_with_enabled([(0.0, False), (10.0, True), (20.0, False)])
        log.put_string_array("/Alerts/errors", 5.0, ["JITing in progress"])
        log.put_string_array("/Alerts/errors", 15.0, ["camera dropped frames"])
        log.put_string_array("/Alerts/errors", 25.0, ["[STICKY] Bridge was disabled"])
        return log

    def details(self, gate):
        findings = compute_checks(self.match_log(), "a.wpilog", self.rule(gate)).findings
        return sorted(f.detail for f in findings)

    def test_boot_noise_is_excluded(self):
        self.assertNotIn("JITing in progress", self.details("afterFirstEnable"))

    def test_faults_reported_after_the_match_are_kept(self):
        # The case a strict "enabled" gate loses.
        self.assertIn("[STICKY] Bridge was disabled", self.details("afterFirstEnable"))

    def test_enabled_gate_would_drop_that_fault(self):
        self.assertNotIn("[STICKY] Bridge was disabled", self.details("enabled"))

    def test_alerts_during_the_enabled_window_are_kept_either_way(self):
        for gate in ("enabled", "afterFirstEnable"):
            self.assertIn("camera dropped frames", self.details(gate))

    def test_ungated_keeps_everything(self):
        self.assertEqual(len(self.details("any")), 3)

    def test_a_sample_exactly_at_first_enable_is_included(self):
        log = log_with_enabled([(0.0, False), (10.0, True)])
        log.put_string_array("/Alerts/errors", 10.0, ["right at enable"])
        self.assertEqual(len(compute_checks(
            log, "a.wpilog", self.rule("afterFirstEnable")).findings), 1)

    def test_a_log_where_the_robot_never_enabled_reports_nothing(self):
        """Which is why "robot never enabled" must be a rule of its own."""
        log = log_with_enabled([(0.0, False)])
        log.put_string_array("/Alerts/errors", 5.0, ["boot noise"])
        self.assertEqual(compute_checks(
            log, "a.wpilog", self.rule("afterFirstEnable")).findings, [])

    def test_never_enabled_is_itself_detectable(self):
        log = log_with_enabled([(0.0, False)])
        rule = [{"name": "Robot never enabled", "entry": "/DriverStation/Enabled",
                 "expect": {"atLeastOnce": True}, "severity": "error"}]
        findings = compute_checks(log, "a.wpilog", rule).findings
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].detail, "never reached True")


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


class MinIncreaseTest(unittest.TestCase):
    """A monotonic counter that must advance. "Did the camera see any AprilTag
    this match" is not answerable from any single sample - the count means
    something only as a difference across the window."""

    def rule(self, minimum=1, **extra):
        base = {"name": "Counter", "entry": "/Vision/BR/UpdatePoseCount",
                "expect": {"minIncrease": minimum}, "severity": "error"}
        base.update(extra)
        return [base]

    def log_with(self, samples, enabled=((0.0, True),)):
        log = log_with_enabled(enabled)
        for timestamp, value in samples:
            log.put_number("/Vision/BR/UpdatePoseCount", timestamp, value)
        return log

    def test_an_advancing_counter_reports_nothing(self):
        log = self.log_with([(1.0, 10), (50.0, 900)])
        self.assertEqual(compute_checks(log, "a.wpilog", self.rule()).findings, [])

    def test_a_frozen_counter_is_reported(self):
        log = self.log_with([(1.0, 42), (50.0, 42)])
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        self.assertEqual(len(findings), 1)
        self.assertIn("advanced by 0", findings[0].detail)
        self.assertIn("expected at least 1", findings[0].detail)

    def test_a_larger_minimum_can_be_required(self):
        log = self.log_with([(1.0, 100), (50.0, 105)])
        self.assertEqual(compute_checks(log, "a.wpilog", self.rule(1)).findings, [])
        self.assertEqual(
            len(compute_checks(log, "a.wpilog", self.rule(50)).findings), 1)

    def test_a_single_reading_cannot_be_judged_and_says_so(self):
        """Silence would read as healthy; a counter that publishes once and stops
        is the failure this rule exists to catch."""
        log = self.log_with([(1.0, 42)])
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        self.assertEqual(len(findings), 1)
        self.assertIn("1 reading", findings[0].detail)

    def test_a_gate_that_admits_nothing_makes_the_rule_inapplicable(self):
        """A pit log where the robot never enabled: "after first enable" admits
        no time, so the rule does not apply. Synthesising a finding there made
        every camera look dead in every non-match log."""
        log = self.log_with([(2.0, 5), (3.0, 9)], enabled=((1.0, False),))
        self.assertEqual(
            compute_checks(log, "a.wpilog",
                           self.rule(**{"while": "afterFirstEnable"})).findings, [])

    def test_the_gate_restricts_which_readings_count(self):
        log = self.log_with([(1.0, 10), (5.0, 900), (50.0, 901)],
                            enabled=((0.0, False), (40.0, True)))
        # Only the 50 s reading is enabled, so it cannot be judged.
        findings = compute_checks(
            log, "a.wpilog", self.rule(**{"while": "enabled"})).findings
        self.assertIn("reading", findings[0].detail)


class AlwaysOneOfTest(unittest.TestCase):
    """For a status string with a legitimate "not yet reported" state as well as
    a good one."""

    def rule(self):
        return [{"name": "Thermal", "entry": "/Vision/BR/ThermalPressure",
                 "expect": {"alwaysOneOf": ["Nominal", ""]},
                 "severity": "warning"}]

    def log_with(self, values):
        log = log_with_enabled([(0.0, True)])
        for timestamp, value in enumerate(values, start=1):
            log.put_string("/Vision/BR/ThermalPressure", float(timestamp), value)
        return log

    def test_an_allowed_sequence_reports_nothing(self):
        log = self.log_with(["", "Nominal", "Nominal"])
        self.assertEqual(compute_checks(log, "a.wpilog", self.rule()).findings, [])

    def test_a_value_outside_the_set_is_reported(self):
        log = self.log_with(["Nominal", "Throttled"])
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        self.assertEqual(len(findings), 1)
        self.assertIn("'Throttled'", findings[0].detail)
        self.assertIn("expected one of", findings[0].detail)

    def test_the_blank_startup_value_is_not_a_finding(self):
        """A camera reads '' until its coprocessor answers; flagging that would
        fire every match."""
        log = self.log_with([""])
        self.assertEqual(compute_checks(log, "a.wpilog", self.rule()).findings, [])

    def test_distinct_offenders_are_reported_separately(self):
        log = self.log_with(["Throttled", "Critical", "Throttled"])
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        self.assertEqual(len(findings), 2)
        self.assertEqual({f.occurrences for f in findings}, {1, 2})


class ExpectEntriesTest(unittest.TestCase):
    """One rule covering both halves: the expected set, and the value expectation."""

    def rule(self):
        return [{"name": "Camera frames",
                 "entry": "/RealOutputs/Vision/*/sending frames",
                 "expectEntries": ["BCH", "BCL", "BL", "BR"],
                 "expect": {"always": True},
                 "while": "enabled", "severity": "error"}]

    def log_with(self, cameras):
        log = log_with_enabled([(0.0, True)])
        for camera, value in cameras.items():
            log.put_boolean(f"/RealOutputs/Vision/{camera}/sending frames", 1.0, value)
        return log

    def test_a_missing_camera_is_named_concretely(self):
        log = self.log_with({"BCH": True, "BL": True, "BR": True})
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        self.assertEqual([f.entry for f in findings],
                         ["/RealOutputs/Vision/BCL/sending frames"])
        self.assertEqual(findings[0].detail, "entry not present in this log")

    def test_a_present_camera_dropping_frames_is_still_caught(self):
        log = self.log_with({"BCH": True, "BCL": False, "BL": True, "BR": True})
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        self.assertEqual([f.entry for f in findings],
                         ["/RealOutputs/Vision/BCL/sending frames"])
        self.assertIn("expected True", findings[0].detail)

    def test_both_kinds_reported_together(self):
        log = self.log_with({"BCH": True, "BL": False})
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        by_entry = {f.entry: f.detail for f in findings}
        self.assertEqual(by_entry["/RealOutputs/Vision/BCL/sending frames"],
                         "entry not present in this log")
        self.assertEqual(by_entry["/RealOutputs/Vision/BR/sending frames"],
                         "entry not present in this log")
        self.assertIn("expected True", by_entry["/RealOutputs/Vision/BL/sending frames"])

    def test_all_present_and_healthy_reports_nothing(self):
        log = self.log_with({c: True for c in ("BCH", "BCL", "BL", "BR")})
        self.assertEqual(compute_checks(log, "a.wpilog", self.rule()).findings, [])

    def test_absence_and_deviation_share_the_rule_severity(self):
        log = self.log_with({"BCH": True, "BCL": False, "BL": True})
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        self.assertEqual({f.severity for f in findings}, {"error"})

    def test_no_camera_at_all_still_names_every_expected_one(self):
        log = log_with_enabled([(0.0, True)])
        findings = compute_checks(log, "a.wpilog", self.rule()).findings
        self.assertEqual(len(findings), 4)
        self.assertTrue(all(f.detail == "entry not present in this log" for f in findings))


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
