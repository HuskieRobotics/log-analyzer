#! /usr/bin/env python3
"""End-to-end tests for analysis.py, parameterized by season.

Each season under tests/fixtures/ declares a manifest naming its .wpilog files,
the top-level config shipped for that season, and any extra scenario configs.
Every scenario runs the analyzer as a subprocess and compares its full stdout
against a recorded "golden" file. This is a characterization suite: it does not
assert the numbers are *correct*, it asserts they do not change unintentionally.

Running:

    python3 -m unittest discover -s tests -v        # from the repo root
    python3 tests/test_integration.py               # equivalent

Log fixtures are large and not checked in; a season whose logs are absent skips
with an explanatory message. See tests/README.md.

After an intentional output change, re-record and review the diff:

    UPDATE_GOLDEN=1 python3 -m unittest discover -s tests
    git diff tests/fixtures/*/golden/
"""

import difflib
import json
import os
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

ANALYSIS = REPO_ROOT / "analysis.py"
FIXTURES_DIR = TESTS_DIR / "fixtures"

# Root holding the per-season log folders. Override to keep the (large) logs
# outside the working tree; season subfolder names come from each manifest.
LOG_ROOT = Path(os.environ.get("LOG_ANALYZER_TEST_LOGS", REPO_ROOT))

# Set UPDATE_GOLDEN=1 to re-record expected output instead of asserting on it.
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"

# A season's logs are read in full for every scenario, at roughly 3.5s per file.
RUN_TIMEOUT_SECONDS = 1800


class Season:
    """One season's fixture set: its logs, configs and goldens."""

    def __init__(self, manifest_path):
        manifest = json.loads(manifest_path.read_text())
        self.dir = manifest_path.parent
        self.name = manifest["season"]
        self.log_dir = LOG_ROOT / manifest["logDir"]
        self.shipped_config = REPO_ROOT / manifest["shippedConfig"]
        # The logs the goldens were recorded against. Recording against a
        # different set would silently produce a suite that tests nothing, so a
        # mismatch is reported rather than absorbed.
        self.expected_logs = list(manifest["logs"])

    @property
    def golden_dir(self):
        return self.dir / "golden"

    def scenarios(self):
        """Return [(name, config path)] — the shipped config plus extras.

        Extra scenarios are discovered from the season's configs/ directory, so
        adding a config and recording its golden is all that a new case needs.
        """
        found = [("shipped_config", self.shipped_config)]
        for config in sorted((self.dir / "configs").glob("*.json")):
            found.append((config.stem, config))
        return found

    def log_problems(self):
        """Return human-readable reasons this season cannot run, if any."""
        if not self.log_dir.is_dir():
            return [f"log folder {self.log_dir} does not exist"]
        present = sorted(p.name for p in self.log_dir.glob("*.wpilog"))
        missing = [n for n in self.expected_logs if n not in present]
        extra = [n for n in present if n not in self.expected_logs]
        problems = []
        if missing:
            problems.append(f"missing logs: {', '.join(missing)}")
        if extra:
            problems.append(
                f"unexpected logs (goldens were not recorded against these): "
                f"{', '.join(extra)}"
            )
        return problems


def discover_seasons():
    """Return every season fixture set, ordered by name."""
    if not FIXTURES_DIR.is_dir():
        return []
    return [Season(m) for m in sorted(FIXTURES_DIR.glob("*/manifest.json"))]


def run_analysis(log_dir, config_path):
    """Run analysis.py over a log folder and return its stdout."""
    completed = subprocess.run(
        [sys.executable, str(ANALYSIS), str(log_dir), str(config_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"analysis.py exited {completed.returncode} for {config_path.name}\n"
            f"--- stdout ---\n{completed.stdout}\n"
            f"--- stderr ---\n{completed.stderr}"
        )
    if completed.stderr.strip():
        raise AssertionError(
            f"analysis.py wrote to stderr for {config_path.name}:\n{completed.stderr}"
        )
    return completed.stdout


class GoldenOutputTest(unittest.TestCase):
    """Base for the per-season classes built at import time."""

    season = None  # set on each generated subclass

    @classmethod
    def setUpClass(cls):
        problems = cls.season.log_problems()
        if problems:
            raise unittest.SkipTest(
                f"season {cls.season.name}: {'; '.join(problems)}. "
                f"See tests/README.md."
            )

    def assert_matches_golden(self, scenario_name, config_path):
        """Run one scenario and compare stdout to its golden file."""
        golden_path = self.season.golden_dir / f"{scenario_name}.txt"

        # Check before running: analyzing a season takes minutes, and there is
        # nothing to compare against yet for a season still being set up.
        if not UPDATE_GOLDEN and not golden_path.is_file():
            self.skipTest(
                f"no golden recorded at {golden_path.relative_to(REPO_ROOT)}; "
                f"record it with UPDATE_GOLDEN=1"
            )

        actual = run_analysis(self.season.log_dir, config_path)

        if UPDATE_GOLDEN:
            golden_path.parent.mkdir(parents=True, exist_ok=True)
            golden_path.write_text(actual)
            self.skipTest(f"recorded {golden_path.relative_to(REPO_ROOT)}")

        expected = golden_path.read_text()
        if actual != expected:
            diff = "".join(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    actual.splitlines(keepends=True),
                    fromfile=str(golden_path.relative_to(REPO_ROOT)),
                    tofile=f"actual ({config_path.name})",
                )
            )
            self.fail(f"output changed for {config_path.name}:\n{diff}")


def _build_season_cases():
    """Create one TestCase class per season, one test method per scenario."""
    for season in discover_seasons():
        methods = {"season": season}
        for scenario_name, config_path in season.scenarios():
            def test(self, _name=scenario_name, _config=config_path):
                self.assert_matches_golden(_name, _config)

            test.__name__ = f"test_{scenario_name}"
            test.__doc__ = f"{season.name}: {scenario_name} ({config_path.name})"
            methods[test.__name__] = test

        cls_name = f"Season{season.name}GoldenTest"
        globals()[cls_name] = type(cls_name, (GoldenOutputTest,), methods)


_build_season_cases()


class ArgumentHandlingTest(unittest.TestCase):
    """Argument and config validation, which needs no .wpilog fixtures."""

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ANALYSIS), *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def any_shipped_config(self):
        seasons = discover_seasons()
        if not seasons:
            self.skipTest("no season fixtures defined")
        return seasons[0].shipped_config

    def test_no_arguments_exits_nonzero(self):
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage:", result.stderr)
        self.assertIn("log_folder", result.stderr)

    def test_missing_log_folder_exits_nonzero(self):
        result = self.run_cli("no_such_folder", str(self.any_shipped_config()))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not a directory", result.stderr)

    def test_missing_config_exits_nonzero(self):
        result = self.run_cli(str(TESTS_DIR), "no_such_config.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error loading config file", result.stderr)

    def test_folder_without_logs_exits_nonzero(self):
        result = self.run_cli(str(FIXTURES_DIR), str(self.any_shipped_config()))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No log files found", result.stderr)


class ReportArtifactTest(unittest.TestCase):
    """--html and --json must produce valid artifacts from a real run.

    The unit tests cover the emitters; this covers the wiring that fills the
    report while main() runs, which they cannot reach.
    """

    @classmethod
    def setUpClass(cls):
        available = [s for s in discover_seasons() if not s.log_problems()]
        if not available:
            raise unittest.SkipTest("no season fixtures with logs present")
        cls.season = available[0]
        cls._tmp = tempfile.TemporaryDirectory()
        folder = Path(cls._tmp.name)
        cls.html_path = folder / "report.html"
        cls.json_path = folder / "report.json"
        completed = subprocess.run(
            [sys.executable, str(ANALYSIS), str(cls.season.log_dir),
             str(cls.season.shipped_config),
             "--html", str(cls.html_path), "--json", str(cls.json_path),
             "--refresh", "15"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=RUN_TIMEOUT_SECONDS)
        if completed.returncode != 0:
            raise AssertionError(f"analysis.py failed:\n{completed.stderr}")
        cls.stdout = completed.stdout

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_tmp"):
            cls._tmp.cleanup()

    def test_both_artifacts_are_written(self):
        self.assertTrue(self.html_path.is_file())
        self.assertTrue(self.json_path.is_file())
        self.assertIn("Wrote HTML report", self.stdout)
        self.assertIn("Wrote JSON report", self.stdout)

    def test_json_describes_the_run(self):
        data = json.loads(self.json_path.read_text())
        self.assertEqual(len(data["files"]), len(self.season.expected_logs))
        self.assertIn("filters", data)
        self.assertTrue(data["generated_at"])

    def test_html_is_self_contained_and_well_formed(self):
        page = self.html_path.read_text()
        for forbidden in ("http://", "https://", "<script", " src=", "fetch("):
            self.assertNotIn(forbidden, page)
        self.assertIn('content="15"', page)
        body = page[page.index("<body>"):page.index("</body>") + len("</body>")]
        ElementTree.fromstring(body)

    def test_html_names_every_log_file(self):
        page = self.html_path.read_text()
        for name in self.season.expected_logs:
            self.assertIn(name, page)


class LatestFlagTest(unittest.TestCase):
    """--latest must narrow a whole folder to one match, end to end."""

    @classmethod
    def setUpClass(cls):
        available = [s for s in discover_seasons()
                     if not s.log_problems() and len(s.expected_logs) > 1]
        if not available:
            raise unittest.SkipTest("no multi-log season fixture present")
        cls.season = available[0]
        cls._tmp = tempfile.TemporaryDirectory()
        cls.json_path = Path(cls._tmp.name) / "report.json"
        cls.html_path = Path(cls._tmp.name) / "report.html"
        completed = subprocess.run(
            [sys.executable, str(ANALYSIS), str(cls.season.log_dir),
             str(cls.season.shipped_config), "--latest",
             "--json", str(cls.json_path), "--html", str(cls.html_path)],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=RUN_TIMEOUT_SECONDS)
        if completed.returncode != 0:
            raise AssertionError(f"analysis.py failed:\n{completed.stderr}")
        cls.stdout = completed.stdout

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_tmp"):
            cls._tmp.cleanup()

    def newest_expected(self):
        # Names embed their recording time, so lexical order is chronological.
        return sorted(self.season.expected_logs)[-1]

    def test_only_one_file_is_analyzed(self):
        data = json.loads(self.json_path.read_text())
        self.assertEqual(len(data["files"]), 1)

    def test_it_is_the_most_recent_one(self):
        data = json.loads(self.json_path.read_text())
        self.assertEqual(data["files"][0]["name"], self.newest_expected())

    def test_stdout_says_what_it_did(self):
        self.assertIn("most recent finished match", self.stdout)
        self.assertIn(self.newest_expected(), self.stdout)

    def test_the_page_names_the_match_it_is_showing(self):
        # A screen left open must make clear which match it is displaying.
        page = self.html_path.read_text()
        self.assertIn(self.newest_expected(), page)
        self.assertIn("most recent finished match", page)

    def test_older_matches_are_absent(self):
        data = json.loads(self.json_path.read_text())
        older = [n for n in self.season.expected_logs if n != self.newest_expected()]
        rendered = json.dumps(data)
        for name in older:
            self.assertNotIn(name, rendered)


class MatchesOnlyTest(unittest.TestCase):
    """--matches-only must drop pit logs, which otherwise dilute per-file
    averages by dividing match events across files that were never matches."""

    @classmethod
    def setUpClass(cls):
        available = [s for s in discover_seasons() if not s.log_problems()]
        season = next((s for s in available if s.name == "2026"), None)
        if season is None:
            raise unittest.SkipTest("2026 fixtures not present")
        cls.season = season
        cls._tmp = tempfile.TemporaryDirectory()
        folder = Path(cls._tmp.name)

        def run(*extra):
            path = folder / f"report{len(list(folder.iterdir()))}.json"
            done = subprocess.run(
                [sys.executable, str(ANALYSIS), str(season.log_dir),
                 str(season.dir / "configs" / "wildcards.json"),
                 "--json", str(path), *extra],
                cwd=str(REPO_ROOT), capture_output=True, text=True,
                timeout=RUN_TIMEOUT_SECONDS)
            if done.returncode != 0:
                raise AssertionError(done.stderr)
            return json.loads(path.read_text()), done.stdout

        cls.all_logs, cls.all_stdout = run()
        cls.matches, cls.matches_stdout = run("--matches-only")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_tmp"):
            cls._tmp.cleanup()

    def test_the_pit_log_is_dropped(self):
        self.assertEqual(len(self.all_logs["files"]) - len(self.matches["files"]), 1)
        self.assertEqual(self.matches["non_matches"],
                         ["akit_26-04-29_19-26-56.wpilog"])

    def test_it_is_named_on_stdout(self):
        self.assertIn("Not a match, skipped: akit_26-04-29_19-26-56.wpilog",
                      self.matches_stdout)

    def test_per_file_averages_are_no_longer_diluted(self):
        def average(report, camera):
            section = next(s for s in report["aggregate_sections"]
                           if f"/{camera}/" in s["title"])
            return section["counts"]["files_processed"], section["counts"]["average"]
        for camera in ("BCH", "BR"):
            files_all, avg_all = average(self.all_logs, camera)
            files_match, avg_match = average(self.matches, camera)
            self.assertEqual((files_all, files_match), (10, 9))
            self.assertGreater(avg_match, avg_all)

    def test_totals_are_unchanged(self):
        """Only the averages were wrong; the pit log contributed no match events."""
        def total(report, camera):
            section = next(s for s in report["aggregate_sections"]
                           if f"/{camera}/" in s["title"])
            return section["result"]["total_count"]
        for camera in ("BCH", "BR"):
            self.assertEqual(total(self.all_logs, camera),
                             total(self.matches, camera))

    def test_selection_says_match_logs(self):
        self.assertEqual(self.matches["selection"], "9 match logs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
