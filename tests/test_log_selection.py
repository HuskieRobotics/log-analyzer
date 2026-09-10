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


class NamingVariantTest(unittest.TestCase):
    """Log names are not reliable identifiers, but their timestamp prefix is.

    A non-match log is timestamp-only ("akit_26-04-29_21-55-44"). A match log can
    lose its event name or match number to a rename failure. And a robot reboot
    during or before a match produces two logs carrying the *same* event and
    match number with different timestamps. Ordering must survive all of it.
    """

    VARIANTS = [
        "akit_26-04-29_21-55-44.wpilog",             # no event, no match
        "akit_26-04-30_16-35-17_johnson.wpilog",     # event, match number missing
        "akit_26-05-01_20-28-19_q105.wpilog",        # match, event name missing
        "akit_26-04-30_19-14-09_johnson_q36.wpilog", # reboot, first log
        "akit_26-04-30_19-18-09_johnson_q36.wpilog", # reboot, second log
    ]

    def test_every_variant_yields_a_timestamp(self):
        for name in self.VARIANTS:
            self.assertNotEqual(log_recorded_at(name), datetime.min, name)

    def test_ordering_ignores_the_missing_event_and_match_fields(self):
        self.assertEqual(
            os.path.basename(select_latest_log(self.VARIANTS).path),
            "akit_26-05-01_20-28-19_q105.wpilog")

    def test_a_reboot_pair_orders_by_timestamp_not_name_length(self):
        pair = [n for n in self.VARIANTS if "q36" in n]
        self.assertEqual(os.path.basename(select_latest_log(pair).path),
                         "akit_26-04-30_19-18-09_johnson_q36.wpilog")

    def test_a_timestamp_only_log_still_sorts_correctly(self):
        mixed = ["akit_26-04-29_21-55-44.wpilog",
                 "akit_26-04-30_19-18-09_johnson_q36.wpilog"]
        self.assertEqual(os.path.basename(select_latest_log(mixed).path),
                         "akit_26-04-30_19-18-09_johnson_q36.wpilog")


class SelectLatestTest(unittest.TestCase):

    NAMES = [
        "test/2026/akit_26-04-30_14-46-03_johnson_q12.wpilog",
        "test/2026/akit_26-05-01_22-45-20_johnson_q121.wpilog",
        "test/2026/akit_26-05-01_13-38-09_johnson_q67.wpilog",
    ]

    def test_picks_the_newest_regardless_of_input_order(self):
        for order in (self.NAMES, list(reversed(self.NAMES)), sorted(self.NAMES)):
            self.assertEqual(
                select_latest_log(order).path,
                "test/2026/akit_26-05-01_22-45-20_johnson_q121.wpilog")

    def test_empty_input_gives_no_path(self):
        chosen = select_latest_log([])
        self.assertIsNone(chosen.path)
        self.assertEqual(chosen.still_writing, [])

    def test_single_file_is_returned(self):
        self.assertEqual(select_latest_log([self.NAMES[0]]).path, self.NAMES[0])

    def test_ties_break_deterministically_on_name(self):
        tied = ["a/akit_26-05-01_10-00-00_b.wpilog",
                "a/akit_26-05-01_10-00-00_a.wpilog"]
        self.assertEqual(select_latest_log(tied).path,
                         select_latest_log(list(reversed(tied))).path)


class GrowingLogTest(unittest.TestCase):
    """Back in the pit the robot has already opened a new log, so the newest file
    is the one being written - the match to check is the newest *finished* one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.finished = root / "akit_26-05-01_10-00-00_e_q1.wpilog"
        self.live = root / "akit_26-05-01_10-30-00_e_q2.wpilog"
        self.finished.write_bytes(b"a" * 100)
        self.live.write_bytes(b"b" * 40)
        self.files = [str(self.finished), str(self.live)]

    def tearDown(self):
        self._tmp.cleanup()

    def grow(self):
        with open(self.live, "ab") as handle:
            handle.write(b"b" * 10)

    def test_without_the_check_the_open_log_is_chosen(self):
        self.assertEqual(select_latest_log(self.files, settle_seconds=0).path, str(self.live))

    def test_a_growing_log_is_skipped_for_the_previous_one(self):
        import threading
        timer = threading.Timer(0.02, self.grow)
        timer.start()
        try:
            self.assertEqual(select_latest_log(self.files, settle_seconds=0.15).path, str(self.finished))
        finally:
            timer.cancel()

    def test_a_static_newest_log_is_still_chosen_with_the_check_on(self):
        self.assertEqual(select_latest_log(self.files, settle_seconds=0.05).path, str(self.live))

    def test_everything_growing_yields_nothing(self):
        import threading
        timers = []
        for path in self.files:
            def bump(p=path):
                with open(p, "ab") as handle:
                    handle.write(b"x")
            timer = threading.Timer(0.02, bump)
            timer.start()
            timers.append(timer)
        try:
            chosen = select_latest_log(self.files, settle_seconds=0.15)
            self.assertIsNone(chosen.path)
            self.assertEqual(sorted(chosen.still_writing), sorted(self.files))
        finally:
            for timer in timers:
                timer.cancel()


if __name__ == "__main__":
    unittest.main(verbosity=2)
