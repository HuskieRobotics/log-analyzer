#! /usr/bin/env python3
"""End-to-end tests for analysis.py.

Each test runs the analyzer as a subprocess against the .wpilog files in the
log folder and compares its full stdout against a recorded "golden" file in
tests/golden/. This is a characterization suite: it does not assert that the
numbers are *correct*, it asserts that they do not change unintentionally.

Running:

    python3 -m unittest discover -s tests -v        # from the repo root
    python3 tests/test_integration.py               # equivalent

The .wpilog files are large and are not checked in. Point the suite at them
with the log folder described in tests/README.md; every test skips with an
explanatory message when they are absent.

After an intentional output change, re-record the goldens and review the diff
before committing:

    UPDATE_GOLDEN=1 python3 -m unittest discover -s tests
    git diff tests/golden/
"""

import difflib
import os
import subprocess
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

ANALYSIS = REPO_ROOT / "analysis.py"
CONFIG_DIR = TESTS_DIR / "configs"
GOLDEN_DIR = TESTS_DIR / "golden"

# Folder holding the .wpilog fixtures. Override with LOG_ANALYZER_TEST_LOGS to
# keep the (large) logs outside the working tree.
LOG_DIR = Path(os.environ.get("LOG_ANALYZER_TEST_LOGS", REPO_ROOT / "test"))

# Set UPDATE_GOLDEN=1 to re-record expected output instead of asserting on it.
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"

# A full run reads every record of every log, which takes ~20s per scenario.
RUN_TIMEOUT_SECONDS = 600

# The fixtures the goldens were recorded against. Recording golden output from a
# different set of logs would silently produce a suite that tests nothing.
REQUIRED_LOGS = (
    "akit_25-04-17_14-51-30_curie_q40.wpilog",
    "akit_25-04-19_09-36-19_curie_e6.wpilog",
)


def missing_logs():
    """Return the names of the required .wpilog fixtures that are absent."""
    if not LOG_DIR.is_dir():
        return list(REQUIRED_LOGS)
    return [name for name in REQUIRED_LOGS if not (LOG_DIR / name).is_file()]


def run_analysis(config_path):
    """Run analysis.py against the log folder and return its stdout.

    Args:
        config_path: Path to the JSON config to run with

    Returns:
        Captured stdout as a string
    """
    completed = subprocess.run(
        [sys.executable, str(ANALYSIS), str(LOG_DIR), str(config_path)],
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
    """Compares full analyzer output against recorded expectations."""

    @classmethod
    def setUpClass(cls):
        absent = missing_logs()
        if absent:
            raise unittest.SkipTest(
                "missing .wpilog fixtures in {}: {}. See tests/README.md.".format(
                    LOG_DIR, ", ".join(absent)
                )
            )

    def assert_matches_golden(self, config_path, golden_name):
        """Run a config and compare stdout to its golden file.

        Args:
            config_path: Path to the JSON config to run
            golden_name: File name under tests/golden/ holding expected stdout
        """
        actual = run_analysis(config_path)
        golden_path = GOLDEN_DIR / golden_name

        if UPDATE_GOLDEN:
            GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
            golden_path.write_text(actual)
            self.skipTest(f"recorded {golden_path.relative_to(REPO_ROOT)}")

        if not golden_path.is_file():
            self.fail(
                f"no golden file at {golden_path.relative_to(REPO_ROOT)}; "
                f"record it with UPDATE_GOLDEN=1"
            )

        expected = golden_path.read_text()
        if actual != expected:
            diff = "".join(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    actual.splitlines(keepends=True),
                    fromfile=f"golden/{golden_name}",
                    tofile=f"actual ({config_path.name})",
                )
            )
            self.fail(f"output changed for {config_path.name}:\n{diff}")

    def test_shipped_config(self):
        """The repo's config.json output, which the README documents verbatim."""
        self.assert_matches_golden(REPO_ROOT / "config.json", "shipped_config.txt")

    def test_value_analysis_with_count(self):
        """Value analysis requesting 'count' with no timeAnalysis configured.

        Regression test for the aggregated value analysis reading the time
        analysis's locals: this raised NameError before that was fixed, and the
        shipped config does not cover it (it requests no 'count' on values).
        """
        self.assert_matches_golden(
            CONFIG_DIR / "value_count.json", "value_count.txt"
        )

    def test_unfiltered_time_analysis(self):
        """Time analysis with every robot-state filter disabled."""
        self.assert_matches_golden(
            CONFIG_DIR / "unfiltered.json", "unfiltered.txt"
        )

    def test_missing_entries_are_reported(self):
        """Entries absent from the logs are skipped without crashing."""
        self.assert_matches_golden(
            CONFIG_DIR / "missing_entries.json", "missing_entries.txt"
        )


class ArgumentHandlingTest(unittest.TestCase):
    """Argument and config validation, which needs no .wpilog fixtures."""

    def run_cli(self, *args):
        """Run analysis.py with the given arguments.

        Returns:
            The CompletedProcess, without asserting on its exit status
        """
        return subprocess.run(
            [sys.executable, str(ANALYSIS), *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_no_arguments_exits_nonzero(self):
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stderr)

    def test_missing_log_folder_exits_nonzero(self):
        result = self.run_cli("no_such_folder", str(REPO_ROOT / "config.json"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not a directory", result.stderr)

    def test_missing_config_exits_nonzero(self):
        result = self.run_cli(str(TESTS_DIR), "no_such_config.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error loading config file", result.stderr)

    def test_folder_without_logs_exits_nonzero(self):
        result = self.run_cli(str(CONFIG_DIR), str(REPO_ROOT / "config.json"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No log files found", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
