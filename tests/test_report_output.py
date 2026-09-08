#! /usr/bin/env python3
"""Unit tests for the JSON and HTML emitters."""

import html as html_module
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import compute_analysis, compute_checks, compute_per_file_counts  # noqa: E402
from Log import Log  # noqa: E402
from report_output import (  # noqa: E402
    FileReport,
    Report,
    ReportSection,
    render_html,
    render_json,
)


def sample_log():
    log = Log()
    log.put_boolean("/DriverStation/Enabled", 0.0, True)
    log.put_string_array("/Alerts/errors", 1.0, ["brownout <b>now</b>"])
    log.put_boolean("/Vision/BR/Connected", 2.0, False)
    return log


def sample_report(**overrides):
    log = sample_log()
    rules = [
        {"name": "Alert", "entry": "/Alerts/errors", "expect": "empty",
         "severity": "error"},
        {"name": "Disconnected", "entry": "/Vision/*/Connected",
         "expect": {"always": True}, "severity": "warning"},
    ]
    findings = compute_checks(log, "q1.wpilog", rules).findings
    results = [("q1.wpilog", [1.0, 5.0, 3.0], [10.0, 11.0, 12.0]),
               ("q2.wpilog", [2.0], [20.0])]
    calcs = [{"type": "max", "name": "Max Thing"}, {"type": "count", "name": "N"}]
    section = ReportSection(
        kind="time", title="/A/start (True) -> /A/end (False)", unit="s",
        result=compute_analysis(results, calcs, "s"),
        counts=compute_per_file_counts(results, calcs))
    report = Report(
        log_folder="test/2026", config_path="checks2026.json",
        filters={"enabled": True, "robotMode": "both"},
        files=[FileReport("q1.wpilog", findings, [section])],
        aggregate_findings=findings, aggregate_sections=[section],
        generated_at="2026-09-08 12:00:00")
    for key, value in overrides.items():
        setattr(report, key, value)
    return report


class RenderJsonTest(unittest.TestCase):

    def test_round_trips_and_keeps_structure(self):
        data = json.loads(render_json(sample_report()))
        self.assertEqual(data["log_folder"], "test/2026")
        self.assertEqual(data["files"][0]["name"], "q1.wpilog")
        self.assertEqual(data["generated_at"], "2026-09-08 12:00:00")

    def test_findings_survive_with_their_fields(self):
        data = json.loads(render_json(sample_report()))
        finding = data["aggregate_findings"][0]
        for key in ("rule_name", "severity", "entry", "detail",
                    "log_file_name", "timestamp", "occurrences", "file_count"):
            self.assertIn(key, finding)

    def test_calculations_and_locations_survive(self):
        data = json.loads(render_json(sample_report()))
        calcs = data["aggregate_sections"][0]["result"]["calculations"]
        top = next(c for c in calcs if c["calc_type"] == "max")
        self.assertEqual(top["value"], 5.0)
        self.assertEqual(top["locations"][0]["log_file_name"], "q1.wpilog")

    def test_empty_report_is_still_valid_json(self):
        data = json.loads(render_json(Report(log_folder="x", config_path="y")))
        self.assertEqual(data["files"], [])


class RenderHtmlTest(unittest.TestCase):

    def test_is_self_contained(self):
        page = render_html(sample_report())
        # Nothing may be loaded from elsewhere: file:// blocks fetch, and a pit
        # has no internet.
        for forbidden in ("http://", "https://", "<script", "src=", "fetch("):
            self.assertNotIn(forbidden, page, f"page must not contain {forbidden}")

    def test_carries_a_meta_refresh_so_the_screen_updates_itself(self):
        self.assertIn('http-equiv="refresh"', render_html(sample_report()))
        self.assertIn('content="15"', render_html(sample_report(), refresh_seconds=15))

    def test_refresh_can_be_disabled(self):
        self.assertNotIn("refresh", render_html(sample_report(), refresh_seconds=0))

    def test_untrusted_text_is_escaped(self):
        # Alert text comes from robot code and can contain anything.
        page = render_html(sample_report())
        self.assertNotIn("<b>now</b>", page)
        self.assertIn(html_module.escape("brownout <b>now</b>"), page)

    def test_counts_appear_in_the_verdict_band(self):
        page = render_html(sample_report())
        self.assertIn("errors", page)
        self.assertIn("warnings", page)

    def test_a_clean_run_says_so(self):
        page = render_html(sample_report(aggregate_findings=[]))
        self.assertIn("No concerns found.", page)
        self.assertIn("tile ok", page)

    def test_per_file_details_are_present(self):
        page = render_html(sample_report())
        self.assertIn("<details>", page)
        self.assertIn("q1.wpilog", page)

    def test_well_formed_enough_to_parse(self):
        from xml.etree import ElementTree
        page = render_html(sample_report())
        body = page[page.index("<body>"):page.index("</body>") + len("</body>")]
        ElementTree.fromstring(body)  # raises if the markup is malformed

    def test_outlier_calculation_with_no_outliers_renders(self):
        """value is None for an outlier calc that found nothing; formatting it
        as a number crashed until this was handled."""
        results = [("q1.wpilog", [1.0, 1.1, 0.9], [1.0, 2.0, 3.0])]
        calcs = [{"type": "outlier_2std", "name": "Outliers"}]
        section = ReportSection(kind="time", title="t", unit="s",
                                result=compute_analysis(results, calcs, "s"))
        page = render_html(Report(log_folder="x", config_path="y",
                                  aggregate_sections=[section]))
        self.assertIn("Outliers", page)
        self.assertIn("none", page)

    def test_outlier_calculation_with_too_few_values_renders(self):
        results = [("q1.wpilog", [1.0], [1.0])]
        calcs = [{"type": "outlier_2std", "name": "Outliers"}]
        section = ReportSection(kind="time", title="t", unit="s",
                                result=compute_analysis(results, calcs, "s"))
        page = render_html(Report(log_folder="x", config_path="y",
                                  aggregate_sections=[section]))
        self.assertIn("Cannot calculate", page)

    def test_empty_report_renders(self):
        page = render_html(Report(log_folder="x", config_path="y"))
        self.assertIn("No concerns found.", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
