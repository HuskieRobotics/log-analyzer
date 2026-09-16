#! /usr/bin/env python3
"""Unit tests for the computation layer, independent of any output format.

These exist to prove the split A2 made: an analysis can be computed, inspected
and serialized without producing a line of terminal output. They need no .wpilog
fixtures and run in well under a second.
"""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import (  # noqa: E402
    compute_analysis,
    compute_per_file_counts,
    format_analysis,
    format_per_file_counts,
)

# Two files' worth of results: (log file name, values, timestamps)
TWO_FILES = [
    ("alpha.wpilog", [1.0, -4.0, 2.0], [10.0, 11.0, 12.0]),
    ("beta.wpilog", [3.0, 5.0], [20.0, 21.0]),
]
ONE_FILE = [("alpha.wpilog", [1.0, -4.0, 2.0], [10.0, 11.0, 12.0])]


def calc(calc_type, name=None):
    return {"type": calc_type, "name": name or calc_type}


class ComputationIsSilentTest(unittest.TestCase):
    """Computing must not write to stdout — that is the whole point of A2."""

    def test_compute_analysis_prints_nothing(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            compute_analysis(TWO_FILES, [calc("average"), calc("max"), calc("count")], "m")
        self.assertEqual(buffer.getvalue(), "")

    def test_compute_per_file_counts_prints_nothing(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            compute_per_file_counts(TWO_FILES, [calc("count")])
        self.assertEqual(buffer.getvalue(), "")


class ComputeAnalysisTest(unittest.TestCase):

    def compute(self, calcs, results=None, unit="m"):
        return compute_analysis(results or TWO_FILES, calcs, unit)

    def by_type(self, computed, calc_type):
        return next(c for c in computed.calculations if c.calc_type == calc_type)

    def test_totals_and_aggregate_flag(self):
        computed = self.compute([calc("count")])
        self.assertTrue(computed.is_aggregate)
        self.assertEqual(computed.total_count, 5)
        self.assertTrue(computed.has_numeric)
        self.assertFalse(self.compute([calc("count")], ONE_FILE).is_aggregate)

    def test_average_min_max_count(self):
        computed = self.compute([calc("average"), calc("min"), calc("max"), calc("count")])
        self.assertAlmostEqual(self.by_type(computed, "average").value, 7.0 / 5)
        self.assertEqual(self.by_type(computed, "min").value, -4.0)
        self.assertEqual(self.by_type(computed, "max").value, 5.0)
        self.assertEqual(self.by_type(computed, "count").value, 5)

    def test_absolute_variants(self):
        computed = self.compute([calc("abs_average"), calc("abs_min"), calc("abs_max")])
        self.assertAlmostEqual(self.by_type(computed, "abs_average").value, 15.0 / 5)
        self.assertEqual(self.by_type(computed, "abs_min").value, 1.0)
        self.assertEqual(self.by_type(computed, "abs_max").value, 5.0)

    def test_locations_carry_file_and_timestamp(self):
        computed = self.compute([calc("min")])
        locations = self.by_type(computed, "min").locations
        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0].log_file_name, "alpha.wpilog")
        self.assertEqual(locations[0].timestamp, 11.0)

    def test_a_value_present_in_several_files_yields_several_locations(self):
        results = [
            ("alpha.wpilog", [9.0, 1.0], [10.0, 11.0]),
            ("beta.wpilog", [9.0], [20.0]),
        ]
        computed = compute_analysis(results, [calc("max")], "m")
        locations = self.by_type(computed, "max").locations
        self.assertEqual(
            [(loc.log_file_name, loc.timestamp) for loc in locations],
            [("alpha.wpilog", 10.0), ("beta.wpilog", 20.0)],
        )

    def test_outliers_are_detected_with_locations(self):
        # Enough baseline points that the outlier does not inflate the standard
        # deviation past its own deviation.
        values = [1.0] * 20 + [50.0]
        timestamps = [float(i) for i in range(len(values))]
        results = [("alpha.wpilog", values, timestamps)]
        computed = compute_analysis(results, [calc("outlier_2std")], "s")
        outliers = self.by_type(computed, "outlier_2std").outliers
        self.assertEqual(len(outliers), 1)
        value, locations = outliers[0]
        self.assertEqual(value, 50.0)
        self.assertEqual(locations[0].timestamp, 20.0)

    def test_outlier_needs_two_values(self):
        computed = compute_analysis(
            [("alpha.wpilog", [1.0], [1.0])], [calc("outlier_2std")], "s")
        self.assertEqual(
            self.by_type(computed, "outlier_2std").error,
            "Cannot calculate with less than 2 values",
        )

    def test_unknown_calculation_type_is_flagged_not_raised(self):
        computed = self.compute([calc("median")])
        self.assertTrue(self.by_type(computed, "median").unknown_type)

    def test_no_values_at_all(self):
        computed = compute_analysis([("alpha.wpilog", [], [])], [calc("average")], "m")
        self.assertFalse(computed.has_values)
        self.assertEqual(computed.calculations, [])

    def test_non_numeric_values_are_captured_but_not_calculated(self):
        results = [("alpha.wpilog", [["an alert"], []], [1.0, 2.0])]
        computed = compute_analysis(results, [calc("average")], "")
        self.assertTrue(computed.has_values)
        self.assertFalse(computed.has_numeric)
        self.assertEqual(computed.total_count, 2)


class ComputePerFileCountsTest(unittest.TestCase):

    def test_extremes_name_the_right_files(self):
        computed = compute_per_file_counts(TWO_FILES, [calc("count")])
        self.assertEqual(computed.files_processed, 2)
        self.assertEqual((computed.min_count, computed.min_file), (2, "beta.wpilog"))
        self.assertEqual((computed.max_count, computed.max_file), (3, "alpha.wpilog"))
        self.assertAlmostEqual(computed.average, 2.5)

    def test_detail_withheld_without_a_count_calculation(self):
        computed = compute_per_file_counts(TWO_FILES, [calc("average")])
        self.assertFalse(computed.show_detail)
        self.assertIsNone(computed.min_file)


class SerializationTest(unittest.TestCase):
    """A computed result must survive asdict/json — this is what A6's JSON
    emitter will rely on."""

    def test_analysis_is_json_serializable(self):
        computed = compute_analysis(
            TWO_FILES, [calc("max"), calc("count"), calc("outlier_2std")], "m")
        encoded = json.dumps(asdict(computed))
        decoded = json.loads(encoded)
        self.assertEqual(decoded["total_count"], 5)
        self.assertTrue(decoded["is_aggregate"])
        max_calc = next(c for c in decoded["calculations"] if c["calc_type"] == "max")
        self.assertEqual(max_calc["value"], 5.0)
        self.assertEqual(max_calc["locations"][0]["log_file_name"], "beta.wpilog")

    def test_per_file_counts_is_json_serializable(self):
        encoded = json.dumps(asdict(compute_per_file_counts(TWO_FILES, [calc("count")])))
        self.assertEqual(json.loads(encoded)["max_file"], "alpha.wpilog")


class TextRenderingTest(unittest.TestCase):
    """The text formatter is one consumer of the computation, not part of it."""

    def test_aggregate_lines(self):
        computed = compute_analysis(TWO_FILES, [calc("max", "Max Thing")], "m")
        self.assertEqual(
            format_analysis(computed),
            [
                "  Total values captured across all files: 5",
                "  Max Thing: 5.000000 m",
                "    @ 21.000000 s in beta.wpilog",
            ],
        )

    def test_single_file_omits_the_file_descriptor(self):
        computed = compute_analysis(ONE_FILE, [calc("max", "Max Thing")], "m")
        self.assertEqual(
            format_analysis(computed),
            [
                "  Total values captured in this file: 3",
                "  Max Thing: 2.000000 m",
                "    @ 12.000000 s ",
            ],
        )

    def test_per_file_counts_lines(self):
        self.assertEqual(
            format_per_file_counts(compute_per_file_counts(TWO_FILES, [calc("count")])),
            [
                "  Files processed: 2",
                "  Average matched values per file: 2.50",
                "  Minimum matched values in any file: 2 in beta.wpilog",
                "  Maximum matched values in any file: 3 in alpha.wpilog",
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
