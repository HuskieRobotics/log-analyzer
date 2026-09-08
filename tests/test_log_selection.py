#! /usr/bin/env python3
"""Unit tests for choosing the most recent log.

--latest exists so a page left open in the pit always shows the newest match, so
picking the wrong file is a silent and consequential failure.
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import log_recorded_at, select_latest_log  # noqa: E402


class RecordedAtTest(unittest.TestCase):

    def test_advantagekit_name(self):
        self.assertEqual(
            log_recorded_at("test/2026/akit_26-05-01_13-38-09_johnson_q67.wpilog"),
            datetime(2026, 5, 1, 13, 38, 9))

    def test_two_digit_year_expands_to_this_century(self):
        self.assertEqual(
            log_recorded_at("akit_25-04-17_14-51-30_curie_q40.wpilog").year, 2025)

    def test_wpilib_datalogmanager_name(self):
        self.assertEqual(log_recorded_at("FRC_20260501_133809.wpilog"),
                         datetime(2026, 5, 1, 13, 38, 9))

    def test_name_without_a_timestamp_falls_back_to_mtime(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "practice.wpilog"
            path.write_bytes(b"x")
            os.utime(path, (1_700_000_000, 1_700_000_000))
            self.assertEqual(log_recorded_at(str(path)),
                             datetime.fromtimestamp(1_700_000_000))

    def test_digits_that_are_not_a_real_date_fall_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "akit_99-99-99_99-99-99_bogus.wpilog"
            path.write_bytes(b"x")
            os.utime(path, (1_700_000_000, 1_700_000_000))
            self.assertEqual(log_recorded_at(str(path)),
                             datetime.fromtimestamp(1_700_000_000))

    def test_missing_file_without_a_timestamp_is_not_an_error(self):
        self.assertEqual(log_recorded_at("/no/such/file.wpilog"), datetime.min)

    def test_the_name_wins_over_mtime(self):
        """scp resets mtime to copy time, so a re-copied old log would otherwise
        masquerade as the newest."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "akit_26-04-30_14-46-03_johnson_q12.wpilog"
            path.write_bytes(b"x")
            os.utime(path, (time.time(), time.time()))  # copied just now
            self.assertEqual(log_recorded_at(str(path)),
                             datetime(2026, 4, 30, 14, 46, 3))


class SelectLatestTest(unittest.TestCase):

    NAMES = [
        "test/2026/akit_26-04-30_14-46-03_johnson_q12.wpilog",
        "test/2026/akit_26-05-01_22-45-20_johnson_q121.wpilog",
        "test/2026/akit_26-05-01_13-38-09_johnson_q67.wpilog",
    ]

    def test_picks_the_newest_regardless_of_input_order(self):
        for order in (self.NAMES, list(reversed(self.NAMES)), sorted(self.NAMES)):
            self.assertEqual(
                select_latest_log(order),
                ["test/2026/akit_26-05-01_22-45-20_johnson_q121.wpilog"])

    def test_returns_exactly_one_file(self):
        self.assertEqual(len(select_latest_log(self.NAMES)), 1)

    def test_empty_input_gives_empty_output(self):
        self.assertEqual(select_latest_log([]), [])

    def test_single_file_is_returned(self):
        self.assertEqual(select_latest_log([self.NAMES[0]]), [self.NAMES[0]])

    def test_ties_break_deterministically_on_name(self):
        tied = ["a/akit_26-05-01_10-00-00_b.wpilog",
                "a/akit_26-05-01_10-00-00_a.wpilog"]
        self.assertEqual(select_latest_log(tied), select_latest_log(list(reversed(tied))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
