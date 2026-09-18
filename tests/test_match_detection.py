#! /usr/bin/env python3
"""Characterizes what separates a match log from a pit log, on real logs.

A synced folder contains both: the robot logs whenever it is powered, so pit
testing produces logs alongside matches. Nothing in the file name reliably says
which is which - the event name and match number can be missing, and a non-match
log carries only a timestamp. These tests pin the signals that *are* dependable,
which is what a future --matches-only filter has to rest on.
"""

import mmap
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import (  # noqa: E402
    MATCH_CACHE_NAME,
    classify_match_log,
    load_match_cache,
    log_contains_match,
    partition_match_logs,
    save_match_cache,
)
from datalog import DataLogReader  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent.parent / "test" / "2026"
PIT_LOG = "akit_26-04-29_19-26-56.wpilog"
MATCH_LOG = "akit_26-05-01_22-45-20_johnson_q121.wpilog"

# Measured across the nine 2026 match logs: 15 s auto + 135 s teleop plus
# transitions, in a +/- 1 s band.
FULL_MATCH_ENABLED_RANGE = (160.0, 163.0)


def robot_state(path):
    """Return (fms_ever, enable_periods, enabled_seconds) for one log."""
    with open(path, "rb") as handle:
        buffer = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        entries = {}
        fms = enabled = False
        periods, total, started, last = 0, 0.0, None, None
        for record in DataLogReader(buffer):
            if record.entry == 0:
                if record.isStart():
                    data = record.getStartData()
                    entries[data.entry] = data
                elif record.isFinish():
                    entries.pop(record.getFinishEntry(), None)
                continue
            entry = entries.get(record.entry)
            if entry is None or entry.type != "boolean":
                continue
            timestamp = record.timestamp / 1e6
            last = timestamp
            if entry.name == "/DriverStation/FMSAttached":
                fms = fms or record.getBoolean()
            elif entry.name == "/DriverStation/Enabled":
                value = record.getBoolean()
                if value and not enabled:
                    periods += 1
                    started = timestamp
                elif enabled and not value and started is not None:
                    total += timestamp - started
                enabled = value
        if enabled and started is not None and last is not None:
            total += last - started
    return fms, periods, total


class MatchDiscriminatorTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        missing = [n for n in (PIT_LOG, MATCH_LOG) if not (LOG_DIR / n).is_file()]
        if missing:
            raise unittest.SkipTest(f"missing fixtures: {', '.join(missing)}")
        cls.pit = robot_state(LOG_DIR / PIT_LOG)
        cls.match = robot_state(LOG_DIR / MATCH_LOG)

    def test_fms_attached_separates_them(self):
        """The only signal that cleanly distinguishes a match from pit testing."""
        self.assertFalse(self.pit[0], "pit log must not report FMS attached")
        self.assertTrue(self.match[0], "match log must report FMS attached")

    def test_being_enabled_does_not_separate_them(self):
        """The robot is enabled plenty in the pit, so "was enabled" is useless."""
        self.assertGreater(self.pit[1], 0)
        self.assertGreater(self.match[1], 0)

    def test_enabled_duration_does_not_separate_them_either(self):
        """289 s of pit testing exceeds a match, so a duration threshold in
        either direction misclassifies one of them."""
        pit_seconds, match_seconds = self.pit[2], self.match[2]
        self.assertGreater(pit_seconds, match_seconds)
        low, high = FULL_MATCH_ENABLED_RANGE
        self.assertTrue(low <= match_seconds <= high, match_seconds)
        self.assertFalse(low <= pit_seconds <= high, pit_seconds)

    def test_the_pit_log_has_many_short_enable_periods(self):
        """Indicative, not dependable: a match is one auto plus one teleop."""
        self.assertGreater(self.pit[1], self.match[1])
        self.assertLessEqual(self.match[1], 3)

    def test_a_full_match_falls_in_the_measured_band(self):
        low, high = FULL_MATCH_ENABLED_RANGE
        self.assertTrue(low <= self.match[2] <= high, self.match[2])


class LogContainsMatchTest(unittest.TestCase):
    """The production function behind --matches-only, on the real logs."""

    @classmethod
    def setUpClass(cls):
        missing = [n for n in (PIT_LOG, MATCH_LOG) if not (LOG_DIR / n).is_file()]
        if missing:
            raise unittest.SkipTest(f"missing fixtures: {', '.join(missing)}")

    def test_a_match_log_is_recognised(self):
        self.assertTrue(log_contains_match(str(LOG_DIR / MATCH_LOG)))

    def test_a_pit_log_is_not(self):
        self.assertFalse(log_contains_match(str(LOG_DIR / PIT_LOG)))

    def test_detecting_a_match_is_cheap(self):
        """Reading stops at the first FMS-attached record, so a match must not
        cost anything like a full scan - otherwise the filter is not worth it."""
        start = time.perf_counter()
        log_contains_match(str(LOG_DIR / MATCH_LOG))
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_a_missing_or_invalid_file_is_not_a_match(self):
        self.assertFalse(log_contains_match("/no/such/log.wpilog"))
        with tempfile.TemporaryDirectory() as folder:
            junk = Path(folder) / "junk.wpilog"
            junk.write_bytes(b"not a wpilog at all")
            self.assertFalse(log_contains_match(str(junk)))

    def test_partition_keeps_order_and_separates_both_sides(self):
        paths = [str(LOG_DIR / PIT_LOG), str(LOG_DIR / MATCH_LOG)]
        matches, others = partition_match_logs(paths)
        self.assertEqual([Path(p).name for p in matches], [MATCH_LOG])
        self.assertEqual([Path(p).name for p in others], [PIT_LOG])

    def test_partition_of_nothing(self):
        self.assertEqual(partition_match_logs([]), ([], []))


class MatchCacheTest(unittest.TestCase):
    """The verdict for a file never changes, so it is worth keeping.

    Proving a pit log is *not* a match costs a full read - measured at 3.5 s for
    56 MB, and a driver station laptop can hold a gigabyte of them. Without a
    cache on disk that price is paid again on every process start.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_round_trip_returns_what_was_stored(self):
        cache = {"a.wpilog": (10, True), "b.wpilog": (20, False)}
        save_match_cache(self.folder, cache)
        self.assertEqual(load_match_cache(self.folder), cache)

    def test_an_absent_cache_is_empty_not_an_error(self):
        self.assertEqual(load_match_cache(self.folder), {})
        self.assertEqual(load_match_cache("/no/such/folder"), {})

    def test_a_corrupt_cache_is_discarded_rather_than_raised(self):
        Path(self.folder, MATCH_CACHE_NAME).write_text("{ this is not json")
        self.assertEqual(load_match_cache(self.folder), {})

    def test_one_bad_entry_does_not_lose_the_others(self):
        Path(self.folder, MATCH_CACHE_NAME).write_text(
            '{"version": 1, "logs": {"good.wpilog": {"size": 5, "match": true},'
            ' "bad.wpilog": {"size": "huh"}}}')
        self.assertEqual(load_match_cache(self.folder), {"good.wpilog": (5, True)})

    def test_an_unwritable_folder_is_survived(self):
        save_match_cache("/no/such/folder", {"a.wpilog": (1, True)})  # must not raise

    def test_a_cached_verdict_is_returned_without_reading(self):
        pit = str(LOG_DIR / PIT_LOG)
        size = Path(pit).stat().st_size
        cache = {PIT_LOG: (size, False)}
        start = time.perf_counter()
        self.assertFalse(classify_match_log(pit, cache))
        self.assertLess(time.perf_counter() - start, 0.1,
                        "a cache hit must not re-read the log")

    def test_partition_fills_the_cache_it_is_given(self):
        paths = [str(LOG_DIR / MATCH_LOG)]
        cache = {}
        partition_match_logs(paths, cache)
        self.assertEqual(cache, {MATCH_LOG: (Path(paths[0]).stat().st_size, True)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
