#! /usr/bin/env python3
"""Unit tests for the hands-off pit monitor.

Sync and analysis are injected, so the loop's decisions are tested without
running either - and without needing a robot.
"""

import io
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pit_monitor import (  # noqa: E402
    DEFAULT_SYNC_NEWEST,
    Monitor,
    describe,
    find_target,
    list_logs,
    run_analysis,
)

REAL_LOGS = Path(__file__).resolve().parent.parent / "test" / "2026"
PIT_LOG = "akit_26-04-29_19-26-56.wpilog"
MATCH_LOG = "akit_26-05-01_22-45-20_johnson_q121.wpilog"


class Recorder:
    """Stands in for run_analysis, which reports success as a bool."""

    def __init__(self, ok=True, message=""):
        self.ok = ok
        self.message = message
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        return self.ok, self.message


class SyncRecorder(Recorder):
    """Stands in for run_sync, which reports a status string rather than a bool
    because an unreachable robot is neither success nor failure."""

    def __init__(self, status="ok", message=""):
        super().__init__(ok=(status == "ok"), message=message)
        self.status = status

    def __call__(self, *args):
        self.calls += 1
        return self.status, self.message


class Exploder(Recorder):
    def __call__(self, *args):
        self.calls += 1
        raise RuntimeError("boom")


class MonitorBase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.analyser = Recorder()
        self.syncer = SyncRecorder()

    def tearDown(self):
        self._tmp.cleanup()

    def monitor(self, **kwargs):
        kwargs.setdefault("settle_seconds", 0)
        return Monitor(self.folder, Path("config.json"), self.folder / "report.html",
                       analyser=self.analyser, syncer=self.syncer, **kwargs)

    def add_match(self, name):
        """A log the match detector will accept."""
        (self.folder / name).write_bytes((REAL_LOGS / MATCH_LOG).read_bytes()[:4_000_000])
        return self.folder / name


class TargetSelectionTest(MonitorBase):

    def test_no_logs_means_no_target(self):
        target, growing = find_target(self.folder, 0, {})
        self.assertIsNone(target)
        self.assertEqual(growing, [])

    def test_a_growing_log_is_reported_and_skipped(self):
        path = self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        sizes = {}
        # settle window sees the size change
        import threading
        threading.Timer(0.02, lambda: path.open("ab").write(b"x")).start()
        target, growing = find_target(self.folder, 0.15, sizes)
        self.assertIsNone(target)
        self.assertEqual(growing, [path.name])

    def test_logs_are_considered_newest_first(self):
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        self.add_match("akit_26-05-01_12-00-00_e_q2.wpilog")
        names = [Path(p).name for p in list_logs(self.folder)]
        self.assertEqual(names[0], "akit_26-05-01_12-00-00_e_q2.wpilog")

    def test_the_expensive_negative_is_cached(self):
        """Proving a pit log is not a match needs a full read; do it once."""
        (self.folder / PIT_LOG).write_bytes((REAL_LOGS / PIT_LOG).read_bytes())
        cache = {}
        reads = []
        find_target(self.folder, 0, cache, announce=reads.append)
        size = (self.folder / PIT_LOG).stat().st_size
        self.assertEqual(cache, {PIT_LOG: (size, False)})
        self.assertEqual(len(reads), 1)
        find_target(self.folder, 0, cache, announce=reads.append)
        self.assertEqual(len(reads), 1, "a cached verdict must not re-read")

    def test_a_reused_name_with_a_new_size_is_reclassified(self):
        """The size is what makes a remembered verdict safe to trust."""
        (self.folder / PIT_LOG).write_bytes((REAL_LOGS / PIT_LOG).read_bytes())
        cache = {PIT_LOG: (17, True)}  # stale: wrong size
        target, _ = find_target(self.folder, 0, cache)
        self.assertIsNone(target)
        self.assertFalse(cache[PIT_LOG][1])

    def test_the_cache_survives_a_restart(self):
        """The whole point: a laptop restarted between matches re-reads nothing."""
        (self.folder / PIT_LOG).write_bytes((REAL_LOGS / PIT_LOG).read_bytes())
        first = self.monitor()
        first.cycle()
        reads = []
        second = self.monitor(announce=reads.append)
        self.assertEqual(second.match_cache, first.match_cache)
        second.cycle()
        self.assertEqual([m for m in reads if "to see whether" in m], [])

    def test_messages_are_shown_as_they_happen(self):
        """A cold cycle runs for minutes; holding its output looks like a hang."""
        seen = []
        monitor = self.monitor(announce=seen.append)
        result = monitor.cycle()
        self.assertEqual(seen, result.messages)
        self.assertIn("no finished match log yet", seen)


class AnalysisReportingTest(unittest.TestCase):
    """What the monitor echoes from the analysis subprocess."""

    def run_with(self, stdout, stderr, code=0):
        script = (f"import sys\n"
                  f"sys.stdout.write({stdout!r})\n"
                  f"sys.stderr.write({stderr!r})\n"
                  f"sys.exit({code})\n")
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / "fake_analysis.py"
            fake.write_text(script)
            with unittest.mock.patch("pit_monitor.ANALYSIS", fake):
                return run_analysis(Path(folder), Path("c.json"),
                                    Path(folder) / "r.html", [])

    def test_success_reports_the_conclusion_not_the_last_warning(self):
        """Progress goes to stderr, so the streams must not just be concatenated."""
        ok, message = self.run_with("Wrote HTML report to r.html\n",
                                    "Scanning big.wpilog...\n")
        self.assertTrue(ok)
        self.assertEqual(message, "Wrote HTML report to r.html")

    def test_failure_reports_the_error(self):
        ok, message = self.run_with("some progress\n", "Error: bad config\n", code=1)
        self.assertFalse(ok)
        self.assertEqual(message, "Error: bad config")

    def test_a_silent_stream_falls_back_to_the_other(self):
        ok, message = self.run_with("", "only stderr said anything\n")
        self.assertTrue(ok)
        self.assertEqual(message, "only stderr said anything")


class CycleTest(MonitorBase):

    def test_nothing_to_analyse_leaves_the_report_alone(self):
        monitor = self.monitor()
        result = monitor.cycle()
        self.assertIsNone(result.target)
        self.assertFalse(result.analysed)
        self.assertEqual(self.analyser.calls, 0)
        self.assertIn("no finished match log yet", result.messages)

    def test_a_new_match_triggers_one_analysis(self):
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        monitor = self.monitor()
        self.assertTrue(monitor.cycle().analysed)
        self.assertEqual(self.analyser.calls, 1)

    def test_an_unchanged_folder_does_not_re_analyse(self):
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        monitor = self.monitor()
        monitor.cycle()
        second = monitor.cycle()
        self.assertFalse(second.analysed)
        self.assertEqual(self.analyser.calls, 1)

    def test_a_newer_match_triggers_another_analysis(self):
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        monitor = self.monitor()
        monitor.cycle()
        self.add_match("akit_26-05-01_12-00-00_e_q2.wpilog")
        result = monitor.cycle()
        self.assertTrue(result.analysed)
        self.assertEqual(self.analyser.calls, 2)
        self.assertTrue(result.target.endswith("q2.wpilog"))

    def test_a_failed_analysis_is_retried_next_cycle(self):
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        self.analyser = Recorder(ok=False, message="analysis exited 1")
        monitor = self.monitor()
        first = monitor.cycle()
        self.assertFalse(first.analysed)
        self.assertIn("analysis exited 1", first.messages)
        monitor.cycle()
        self.assertEqual(self.analyser.calls, 2)


class ResilienceTest(MonitorBase):
    """A pit display that dies at the wrong moment is worse than a stale one."""

    def test_a_sync_failure_does_not_stop_the_cycle(self):
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        self.syncer = SyncRecorder(status="failed", message="scp exited 1")
        monitor = self.monitor(sync_host="10.30.61.2")
        result = monitor.cycle()
        self.assertEqual(result.sync_status, "failed")
        self.assertTrue(result.analysed, "analysis must still run from local logs")

    def test_an_absent_robot_is_not_reported_as_a_success(self):
        """sync exits 0 when the robot is away, so a boolean would say "synced"."""
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        self.syncer = SyncRecorder(status="unreachable", message="Robot not reachable")
        result = self.monitor(sync_host="10.30.61.2").cycle()
        self.assertEqual(result.sync_status, "unreachable")
        self.assertIn("robot not reachable", describe(result))
        self.assertNotIn("synced", describe(result))

    def test_a_raising_syncer_is_survived(self):
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        self.syncer = Exploder()
        result = self.monitor(sync_host="10.30.61.2").cycle()
        self.assertEqual(result.sync_status, "failed")
        self.assertTrue(any("boom" in m for m in result.messages))
        self.assertTrue(result.analysed)

    def test_a_raising_analyser_is_survived(self):
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        self.analyser = Exploder()
        result = self.monitor().cycle()
        self.assertFalse(result.analysed)
        self.assertTrue(any("boom" in m for m in result.messages))

    def test_a_missing_folder_is_survived(self):
        monitor = Monitor(Path("/no/such/folder"), Path("c.json"), Path("r.html"),
                          settle_seconds=0, analyser=self.analyser)
        result = monitor.cycle()
        self.assertIsNone(result.target)
        self.assertEqual(self.analyser.calls, 0)

    def test_syncing_is_skipped_when_not_configured(self):
        result = self.monitor().cycle()
        self.assertIsNone(result.sync_status)
        self.assertEqual(self.syncer.calls, 0)


class SyncArgumentTest(MonitorBase):
    """The monitor analyses one log, so it must not fetch the robot's history."""

    def test_the_selection_reaches_the_syncer(self):
        recorded = {}

        def syncer(folder, host, extra):
            recorded["extra"] = list(extra)
            return "ok", ""

        monitor = Monitor(self.folder, Path("c.json"), self.folder / "r.html",
                          settle_seconds=0, sync_host="10.30.61.2",
                          sync_extra=["--newest", "10"], syncer=syncer,
                          analyser=self.analyser)
        with redirect_stdout(io.StringIO()):
            monitor.cycle()
        self.assertEqual(recorded["extra"], ["--newest", "10"])

    def test_no_selection_by_default_on_the_object(self):
        """Monitor itself stays neutral; the CLI supplies the default."""
        recorded = {}

        def syncer(folder, host, extra):
            recorded["extra"] = list(extra)
            return "ok", ""

        monitor = Monitor(self.folder, Path("c.json"), self.folder / "r.html",
                          settle_seconds=0, sync_host="h", syncer=syncer,
                          analyser=self.analyser)
        with redirect_stdout(io.StringIO()):
            monitor.cycle()
        self.assertEqual(recorded["extra"], [])

    def test_the_cli_default_bounds_the_fetch(self):
        """Without a bound the first cycle would pull the whole season - 6.55 GB
        when this was first run against a robot - and exceed sync's timeout."""
        self.assertGreater(DEFAULT_SYNC_NEWEST, 0)
        self.assertLessEqual(DEFAULT_SYNC_NEWEST, 25)


class DescribeTest(MonitorBase):

    def test_says_what_happened(self):
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        monitor = self.monitor(sync_host="10.30.61.2")
        line = describe(monitor.cycle())
        self.assertIn("synced", line)
        self.assertIn("report updated from", line)

    def test_says_when_nothing_changed(self):
        self.add_match("akit_26-05-01_10-00-00_e_q1.wpilog")
        monitor = self.monitor()
        monitor.cycle()
        self.assertIn("unchanged", describe(monitor.cycle()))

    def test_says_when_waiting(self):
        self.assertIn("waiting for a match log", describe(self.monitor().cycle()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
