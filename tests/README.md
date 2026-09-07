# Integration Tests

End-to-end characterization tests for `analysis.py`. Each test runs the analyzer
as a subprocess against real `.wpilog` files and compares its **full stdout**
against a recorded expectation in [`golden/`](golden/).

These tests assert that output does not change *unintentionally*. They do not
assert the numbers are correct — the goldens were recorded from the current
implementation, so they lock in present behavior (bugs included) and make any
change to it visible in a diff.

## Running

From the repository root:

```bash
python3 -m unittest discover -s tests -v
```

Or directly:

```bash
python3 tests/test_integration.py
```

A full run takes roughly **90 seconds** — each scenario reads every record of
both logs, and ingest is currently O(n²) in records per field.

To run only the fast tests that need no log files (~0.2s):

```bash
python3 -m unittest tests.test_integration.ArgumentHandlingTest -v
```

## Log fixtures

The tests need these two files, which are the ones the README's sample output was
produced from:

```
akit_25-04-17_14-51-30_curie_q40.wpilog
akit_25-04-19_09-36-19_curie_e6.wpilog
```

They are ~108 MB together and are **not checked in** (`*.wpilog` is gitignored).
Place them in `test/` at the repository root, or point the suite elsewhere:

```bash
LOG_ANALYZER_TEST_LOGS=/path/to/logs python3 -m unittest discover -s tests
```

Without them, `GoldenOutputTest` skips with an explanatory message and
`ArgumentHandlingTest` still runs. The suite checks for these exact filenames —
recording goldens against a different set of logs would produce a suite that
silently tests nothing.

## Scenarios

| Test | Config | Covers |
|---|---|---|
| `test_shipped_config` | [`../config.json`](../config.json) | The example config, whose output the top-level README documents verbatim |
| `test_value_analysis_with_count` | [`configs/value_count.json`](configs/value_count.json) | Value analysis requesting `count` with **no** `timeAnalysis` — regression test, see below |
| `test_unfiltered_time_analysis` | [`configs/unfiltered.json`](configs/unfiltered.json) | All robot-state filters off (`robotMode: "both"`), plus `outlier_2std` |
| `test_missing_entries_are_reported` | [`configs/missing_entries.json`](configs/missing_entries.json) | Entries absent from the logs are skipped without crashing |
| `ArgumentHandlingTest` | — | Bad argv, missing folder, missing config, folder with no logs |

`test_value_analysis_with_count` exists because the aggregated value analysis
used to read the *time* analysis's local variables, which raised
`UnboundLocalError` whenever a value analysis requested `count` without a prior
time analysis. The shipped config never requests `count` on a value analysis, so
a golden test of `config.json` alone would not have caught it.

## Updating goldens

When you change the output deliberately, re-record and **review the diff** before
committing — that diff is the point of the suite:

```bash
UPDATE_GOLDEN=1 python3 -m unittest discover -s tests
git diff tests/golden/
```

Tests report as *skipped* while recording, since nothing is being asserted.

## Determinism

Output contains no wall-clock times, absolute paths, or hash-ordered iteration,
so runs are byte-identical on the same machine. Floating-point results are
printed to six decimals; a different CPU or Python build could in principle
differ in the last digit, in which case re-record the goldens on that machine.
