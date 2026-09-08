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
import subprocess
import sys
import unittest
from pathlib import Path

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
        self.assertIn("Usage:", result.stderr)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
