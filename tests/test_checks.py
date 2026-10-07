#! /usr/bin/env python3
"""Unit tests for the check rules, including absence checks.

No .wpilog fixtures: the Log objects are built directly, so these run in
milliseconds and cover the cases the real logs happen not to contain.
"""

import json
import struct
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from Log import Log  # noqa: E402
from analysis import (  # noqa: E402
    CheckFinding,
    CheckReport,
    compute_checks,
    format_check_findings,
    merge_check_findings,
)
from report_output import describe_durations  # noqa: E402


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


class ExcludeMessageTest(unittest.TestCase):
    """Dropping alert messages a more precise rule already covers.

    The Alerts stream is a catch-all, so a precise rule for something it also
    mentions reports one fact twice. Measured on the 2026 logs, the camera
    messages were pure duplication: 650 samples carried one and not one of them
    had "sending frames" still true, while the alerts split a single 178.68 s
    BCL outage into 34 spells as the camera flapped between two messages.
    """

    def log_with_alerts(self):
        log = log_with_enabled([(0.0, True)])
        for timestamp, alerts in [
            (1.0, []),
            (2.0, ["camera BL connected to NT but not publishing frames",
                   "CANivore error detected"]),
            (4.0, ["camera BR disconnected from NT", "CANivore error detected"]),
            (6.0, []),
        ]:
            log.put_string_array("/alerts", timestamp, alerts)
        return log

    def details(self, **extra):
        rule = {"name": "Alert", "entry": "/alerts", "expect": "empty",
                "severity": "error"}
        rule.update(extra)
        return {f.detail for f
                in compute_checks(self.log_with_alerts(), "a.wpilog", [rule]).findings}

    def test_without_it_every_message_is_reported(self):
        self.assertEqual(self.details(), {
            "CANivore error detected",
            "camera BL connected to NT but not publishing frames",
            "camera BR disconnected from NT"})

    def test_a_pattern_drops_matching_messages_and_keeps_the_rest(self):
        self.assertEqual(
            self.details(excludeMessage=[
                "camera * connected to NT but not publishing frames",
                "camera * disconnected from NT"]),
            {"CANivore error detected"})

    def test_one_pattern_covers_every_camera(self):
        """Four cameras, one line of config."""
        log = log_with_enabled([(0.0, True)])
        log.put_string_array("/alerts", 1.0, [
            f"camera {name} connected to NT but not publishing frames"
            for name in ("BCH", "BCL", "BL", "BR")])
        log.put_string_array("/alerts", 2.0, [])
        rule = {"name": "Alert", "entry": "/alerts", "expect": "empty",
                "severity": "error",
                "excludeMessage": "camera * connected to NT but not publishing frames"}
        self.assertEqual(compute_checks(log, "a.wpilog", [rule]).findings, [])

    def test_a_bare_string_is_accepted_like_excludeEntry(self):
        self.assertNotIn("CANivore error detected",
                         self.details(excludeMessage="CANivore error detected"))

    def test_a_pattern_matching_nothing_changes_nothing(self):
        self.assertEqual(self.details(excludeMessage=["no such alert"]),
                         self.details())

    def test_matching_is_case_sensitive(self):
        """fnmatch folds case by platform; a config must not mean one thing on
        Windows and another on Linux, so fnmatchcase is used."""
        self.assertIn("CANivore error detected",
                      self.details(excludeMessage="canivore error detected"))

    def test_an_excluded_message_leaves_no_trace_in_the_durations(self):
        """It must not open a spell either, or it would still shape the report."""
        rule = {"name": "Alert", "entry": "/alerts", "expect": "empty",
                "severity": "error", "excludeMessage": "camera *"}
        found = compute_checks(self.log_with_alerts(), "a.wpilog", [rule]).findings
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].detail, "CANivore error detected")
        self.assertEqual(found[0].durations, [4.0])      # 2.0 -> 6.0

    def test_the_shipped_config_suppresses_only_the_camera_messages(self):
        """Pins what checks2026.json actually excludes."""
        config = json.loads((REPO_ROOT / "checks2026.json").read_text())
        rule = next(c for c in config["checks"]
                    if c["entry"] == "/RealOutputs/Alerts/errors"
                    and c["severity"] == "error")
        self.assertEqual(rule["excludeMessage"], [
            "camera * connected to NT but not publishing frames",
            "camera * disconnected from NT"])
        # the precise rule that replaces them must still be present
        names = {c["name"] for c in config["checks"]}
        self.assertIn("Camera frames", names)


class DurationDescriptionTest(unittest.TestCase):
    """Wording of the duration summary."""

    def test_nothing_to_say_about_no_spells(self):
        self.assertEqual(describe_durations([]), "")

    def test_one_spell_still_states_its_count(self):
        """min, median and max of one number are the same number, so they are
        left out - but the count stays, so every finding reads the same shape."""
        self.assertEqual(describe_durations([0.75]), "1 spell, 0.75 s")

    def test_several_spells_get_the_spread(self):
        text = describe_durations([0.5, 0.75, 2.0, 61.4])
        self.assertIn("4 spells, 64.65 s total", text)
        self.assertIn("min 0.50 s", text)
        self.assertIn("median 1.38 s", text)
        self.assertIn("max 61.40 s", text)

    def test_an_unfinished_spell_says_so(self):
        self.assertIn("still in that state when the log ended",
                      describe_durations([12.0], True))


class FindingDurationTest(unittest.TestCase):
    """How long a finding's state actually held.

    "is False, expected True, x2" cannot distinguish a camera dark for 0.7 s
    from one dark for a minute. Both appear in the same real match - measured on
    ilnap_e12, three cameras blipped under 0.75 s while BR was dark for 9.97 s.
    """

    def rule(self, **extra):
        rule = {"name": "Camera frames", "entry": "/cam",
                "expect": {"always": True}, "severity": "error"}
        rule.update(extra)
        return [rule]

    def findings(self, log, **extra):
        return compute_checks(log, "a.wpilog", self.rule(**extra)).findings

    def test_a_spell_runs_until_the_value_comes_back(self):
        """A sample holds until the next one, as find_excursions documents."""
        log = log_with_enabled([(0.0, True)])
        for timestamp, value in [(0.0, True), (5.0, False), (6.5, True)]:
            log.put_boolean("/cam", timestamp, value)
        found = self.findings(log)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].durations, [1.5])
        self.assertFalse(found[0].unresolved)

    def test_several_spells_are_kept_separately(self):
        log = log_with_enabled([(0.0, True)])
        for timestamp, value in [(0.0, True), (1.0, False), (2.0, True),
                                 (10.0, False), (20.0, True), (30.0, False),
                                 (30.5, True)]:
            log.put_boolean("/cam", timestamp, value)
        found = self.findings(log)
        self.assertEqual(sorted(found[0].durations), [0.5, 1.0, 10.0])
        self.assertEqual(found[0].occurrences, 3)

    def test_a_spell_still_open_at_the_end_is_marked(self):
        """Its duration is a lower bound, so the reader must be told."""
        log = log_with_enabled([(0.0, True)])
        log.put_boolean("/cam", 0.0, True)
        log.put_boolean("/cam", 5.0, False)
        log.put_number("/filler", 40.0, 1.0)      # carries the log to 40 s
        found = self.findings(log)
        self.assertTrue(found[0].unresolved)
        self.assertEqual(found[0].durations, [35.0])

    def test_time_the_gate_excludes_is_not_counted(self):
        """A spell straddling a brief disable stays one spell, shorter."""
        log = log_with_enabled([(0.0, True), (10.0, False), (20.0, True),
                                (30.0, False)])
        for timestamp, value in [(0.0, True), (5.0, False), (25.0, True)]:
            log.put_boolean("/cam", timestamp, value)
        found = self.findings(log, **{"while": "enabled"})
        # raw spell is 5..25; the gate admits 5..10 and 20..25
        self.assertEqual(found[0].durations, [10.0])

    def test_a_spell_wholly_outside_the_gate_is_dropped(self):
        log = log_with_enabled([(0.0, False), (20.0, True)])
        for timestamp, value in [(0.0, True), (5.0, False), (10.0, True),
                                 (25.0, False), (30.0, True)]:
            log.put_boolean("/cam", timestamp, value)
        found = self.findings(log, **{"while": "enabled"})
        self.assertEqual(found[0].durations, [5.0])
        self.assertEqual(found[0].timestamp, 25.0)

    def test_each_alert_message_is_timed_separately(self):
        """expect "empty" makes one finding per message, so one span each."""
        log = log_with_enabled([(0.0, True)])
        for timestamp, alerts in [(0.0, []), (1.0, ["brownout"]),
                                  (2.0, ["brownout", "camera down"]),
                                  (5.0, ["camera down"]), (9.0, [])]:
            log.put_string_array("/alerts", timestamp, alerts)
        rule = [{"name": "Alert", "entry": "/alerts", "expect": "empty",
                 "severity": "error"}]
        by_detail = {f.detail: f for f in compute_checks(log, "a.wpilog", rule).findings}
        self.assertEqual(by_detail["brownout"].durations, [4.0])      # 1 -> 5
        self.assertEqual(by_detail["camera down"].durations, [7.0])   # 2 -> 9

    def test_a_threshold_rule_keeps_its_own_wording(self):
        """Excursions already report total and longest; do not double up."""
        log = log_with_enabled([(0.0, True)])
        for timestamp, value in [(0.0, 1.0), (1.0, 9.0), (3.0, 1.0)]:
            log.put_number("/temp", timestamp, value)
        rule = [{"name": "Hot", "entry": "/temp", "expect": {"above": 5},
                 "severity": "warning"}]
        found = compute_checks(log, "a.wpilog", rule).findings
        self.assertEqual(found[0].durations, [])
        self.assertIn("above 5", found[0].detail)

    def test_merging_pools_the_spells_rather_than_the_medians(self):
        """A median of medians is not a median."""
        def report(durations):
            finding = CheckFinding("R", "error", "/cam", "is False, expected True",
                                   "q1.wpilog", 1.0, durations=list(durations))
            return CheckReport(findings=[finding])
        merged = merge_check_findings([report([1.0, 1.0, 10.0]), report([2.0])])
        self.assertEqual(len(merged), 1)
        self.assertEqual(sorted(merged[0].durations), [1.0, 1.0, 2.0, 10.0])

    def test_merging_does_not_mutate_the_per_file_finding(self):
        first = CheckFinding("R", "error", "/cam", "d", "q1.wpilog", 1.0,
                             durations=[1.0])
        second = CheckFinding("R", "error", "/cam", "d", "q2.wpilog", 2.0,
                              durations=[2.0])
        merge_check_findings([CheckReport(findings=[first]),
                              CheckReport(findings=[second])])
        self.assertEqual(first.durations, [1.0])

    def test_the_duration_reaches_the_text_report(self):
        log = log_with_enabled([(0.0, True)])
        for timestamp, value in [(0.0, True), (1.0, False), (3.5, True)]:
            log.put_boolean("/cam", timestamp, value)
        lines = format_check_findings(self.findings(log), False)
        self.assertTrue(any("1 spell, 2.50 s" in line for line in lines), lines)

    def test_the_sample_count_gives_way_to_the_spell_count(self):
        """occurrences counts samples carrying the condition, so it tracks
        logging rate, not events: one real 0.72 s camera dropout was logged
        three times and read as "x3". The spell count replaces it."""
        log = log_with_enabled([(0.0, True)])
        for timestamp, value in [(0.0, True), (1.0, False), (1.2, False),
                                 (1.5, False), (1.72, True)]:
            log.put_boolean("/cam", timestamp, value)
        found = self.findings(log)
        self.assertEqual(found[0].occurrences, 3)       # three samples
        self.assertEqual(found[0].durations, [0.72])    # one spell
        lines = format_check_findings(found, False)
        joined = "\n".join(lines)
        self.assertIn("1 spell, 0.72 s", joined)
        self.assertNotIn("x3", joined)

    def test_a_sample_at_exactly_zero_is_not_seen(self):
        """Pins a pre-existing edge, so a future change to it is deliberate.

        A range read is half-open, so get_field_values(field, 0.0, last) skips a
        sample at exactly 0.0; a spell already underway there is measured from
        the next sample instead. AdvantageKit timestamps start around 1.7 s, so
        this does not arise in a real log.
        """
        log = log_with_enabled([(0.0, True)])
        for timestamp, value in [(0.0, False), (1.0, False), (2.0, True)]:
            log.put_boolean("/cam", timestamp, value)
        found = self.findings(log)
        self.assertEqual(found[0].durations, [1.0])   # 1.0 -> 2.0, not 0.0 -> 2.0

    def test_a_spell_from_the_first_visible_sample_is_measured_whole(self):
        log = log_with_enabled([(0.0, True)])
        for timestamp, value in [(0.5, False), (3.0, True)]:
            log.put_boolean("/cam", timestamp, value)
        self.assertEqual(self.findings(log)[0].durations, [2.5])


class PoseOffFieldTest(unittest.TestCase):
    """The shipped bounds on the pose estimator, against measured magnitudes.

    Rules are read from checks2026.json rather than restated, so the thresholds
    this pins are the ones actually shipped.

    The numbers come from the ten 2026 logs, measured against the real REBUILT
    field. Healthy matches put the estimated pose outside it by at most 0.54 m:
    that worst case is q89, where the pose drifts smoothly from 16.59 to 17.08
    over a quarter second while pinned in the corner at y~0 - wheel slip
    integrating into odometry, not a fault. q22's estimator left the field by
    17.91 m, discontinuously. The margin sits between them, clearing the drift
    with headroom while leaving the teleport an order of magnitude clear of it.
    """

    @classmethod
    def setUpClass(cls):
        config = json.loads((REPO_ROOT / "checks2026.json").read_text())
        cls.field = config["field"]
        cls.FIELD_X = cls.field["length"]
        cls.FIELD_Y = cls.field["width"]
        cls.rules = [rule for rule in config["checks"]
                     if rule["name"].startswith("Robot pose off the field")]

    def test_the_bounds_are_derived_from_the_declared_field_size(self):
        """The field changes between seasons - 2024 to 2025 moved the length by
        about a metre, more than the margin. A config copied forward without
        re-measuring fails open: the bound sits beyond the real edge and a real
        excursion goes unreported. Pinning the four thresholds to the one
        declared size means a rollover cannot update them by halves."""
        margin = self.field["margin"]
        expected = {
            "Robot pose off the field (-x)": ("below", -margin),
            "Robot pose off the field (+x)": ("above", self.FIELD_X + margin),
            "Robot pose off the field (-y)": ("below", -margin),
            "Robot pose off the field (+y)": ("above", self.FIELD_Y + margin),
        }
        self.assertEqual({rule["name"] for rule in self.rules}, set(expected))
        for rule in self.rules:
            key, value = expected[rule["name"]]
            with self.subTest(rule=rule["name"]):
                self.assertAlmostEqual(rule["expect"][key], value, places=6)

    def test_the_margin_clears_the_worst_healthy_excursion(self):
        """Measured against the real field: healthy matches reach 0.54 m out.

        Tightening below that starts reporting wall drift as a fault. q89 is
        exactly that case, and it is benign.
        """
        self.assertGreater(self.field["margin"], self.WORST_HEALTHY)

    def test_the_margin_still_leaves_a_real_divergence_far_clear(self):
        """q22 left the field by 17.91 m."""
        self.assertGreater(17.91 / self.field["margin"], 10)

    def setUp(self):
        self.assertEqual(len(self.rules), 4, "expected one rule per field edge")

    def log_with_poses(self, poses):
        """A Log holding Pose2d structs, flattened the way the real logs are.

        The schemas are registered by hand so this needs no .wpilog: it also
        pins that a Pose2d still flattens to translation/x and translation/y,
        which is what the config's entry paths depend on.
        """
        log = log_with_enabled([(0.0, True)])
        log.struct_decoder.add_schema("Translation2d", b"double x;double y")
        log.struct_decoder.add_schema("Rotation2d", b"double value")
        log.struct_decoder.add_schema(
            "Pose2d", b"Translation2d translation;Rotation2d rotation")
        for timestamp, x, y in poses:
            log.put_struct("/RealOutputs/Drivetrain/Pose", timestamp,
                           struct.pack("<ddd", x, y, 0.0), "Pose2d", False)
        return log

    def findings(self, poses):
        log = self.log_with_poses(poses)
        return compute_checks(log, "a.wpilog", self.rules).findings

    def test_a_pose_on_the_field_raises_nothing(self):
        self.assertEqual(self.findings([(1.0, 8.0, 4.0), (2.0, 1.2, 7.9)]), [])

    def test_the_struct_still_flattens_to_the_entries_the_config_names(self):
        """If this breaks, the rules stop matching and silently never fire."""
        log = self.log_with_poses([(1.0, 1.0, 2.0)])
        for axis in ("x", "y"):
            self.assertIn(f"/RealOutputs/Drivetrain/Pose/translation/{axis}",
                          log.fields)

    # The worst excursion any healthy 2026 match makes, measured against the
    # real field: q89 drifting into the corner at MatchTime 47.
    WORST_HEALTHY = 0.54

    def test_wall_drift_at_the_boundary_is_not_a_finding(self):
        """A robot pushing into the wall slips, and odometry integrates it.

        Every healthy 2026 match does some of this; none is a fault.
        """
        self.assertEqual(self.findings([
            (1.0, 8.0, self.FIELD_Y + self.WORST_HEALTHY),
            (2.0, -self.WORST_HEALTHY, 4.0),
            (3.0, self.FIELD_X + self.WORST_HEALTHY, 4.0),
            (4.0, -self.WORST_HEALTHY, self.FIELD_Y + self.WORST_HEALTHY),
        ]), [])

    def test_the_q89_wall_drift_specifically_stays_quiet(self):
        """Its real samples: a smooth walk to 0.543 m outside, then back.

        Against the field length this config shipped with before the game
        manual was consulted, these same samples sat 0.09 m outside and looked
        healthy for the wrong reason.
        """
        drift = [(395.606, 16.594, -0.006), (395.701, 16.875, -0.002),
                 (395.798, 17.066, -0.001), (395.839, 17.083, 0.0),
                 (395.870, 17.081, 0.0)]
        self.assertEqual(self.findings(drift), [])

    def test_the_q22_divergence_is_an_error(self):
        """The real event: 21.9 m in one cycle, to y = -17.896."""
        found = self.findings([(1.0, 0.445, 4.006), (1.05, -0.591, -17.896),
                               (1.09, -0.591, 0.0)])
        by_rule = {f.rule_name: f for f in found}
        self.assertIn("Robot pose off the field (-y)", by_rule)
        self.assertTrue(all(f.severity == "error" for f in found))
        self.assertIn("-17.8", by_rule["Robot pose off the field (-y)"].detail)
        # x reached only -0.591 here, inside the drift the margin allows. The
        # divergence is caught on the axis that actually left the field, which
        # is why each edge is bounded separately.
        self.assertNotIn("Robot pose off the field (-x)", by_rule)

    def test_each_edge_is_covered(self):
        for x, y, expected in [
            (-4.0, 4.0, "Robot pose off the field (-x)"),
            (self.FIELD_X + 4.0, 4.0, "Robot pose off the field (+x)"),
            (8.0, -4.0, "Robot pose off the field (-y)"),
            (8.0, self.FIELD_Y + 4.0, "Robot pose off the field (+y)"),
        ]:
            with self.subTest(x=x, y=y):
                names = {f.rule_name for f in self.findings([(1.0, x, y)])}
                self.assertIn(expected, names)


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
