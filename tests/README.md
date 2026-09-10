# Integration Tests

End-to-end characterization tests for `analysis.py`, **parameterized by season**.
Each scenario runs the analyzer as a subprocess against real `.wpilog` files and
compares its **full stdout** against a recorded expectation under
[`fixtures/<season>/golden/`](fixtures/).

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

Every scenario reads every record of every log in its season, at roughly **3.5 s
per file**. The 2025 season (2 logs, 4 scenarios) takes about 30 s.

To run one season, or only the fast tests that need no log files (~0.2 s):

```bash
python3 -m unittest tests.test_integration.Season2025GoldenTest -v
python3 -m unittest tests.test_integration.ArgumentHandlingTest -v
```

## Fixture layout

Each season is a directory under `fixtures/` with a manifest, optional extra
configs, and its recorded goldens:

```
fixtures/2025/manifest.json      logs, log folder, shipped config
fixtures/2025/configs/*.json     extra scenarios beyond the shipped config
fixtures/2025/golden/*.txt       recorded stdout, one per scenario
```

The manifest points at a log folder relative to the repo root:

```json
{
    "season": "2025",
    "logDir": "test/2025",
    "shippedConfig": "config2025.json",
    "logs": ["akit_25-04-17_...wpilog", "akit_25-04-19_...wpilog"]
}
```

Scenarios are **discovered**, not registered: the season's `shippedConfig` plus
every `configs/*.json`, each compared against `golden/<name>.txt`. Adding a case
means dropping in a config and recording its golden.

### Adding a season

1. `mkdir -p fixtures/<year>/{configs,golden}`
2. Write `fixtures/<year>/manifest.json` naming the logs and shipped config.
3. Record goldens (below). Until then, that season's scenarios skip.

## Log fixtures

The `.wpilog` files are large and **not checked in** (`*.wpilog` is gitignored) —
~108 MB for 2025, ~433 MB for 2026. Place them in the `logDir` each manifest
names, or relocate all seasons at once:

```bash
LOG_ANALYZER_TEST_LOGS=/path/to/logs python3 -m unittest discover -s tests
```

`LOG_ANALYZER_TEST_LOGS` replaces the repo root, so the per-season subfolders
(`test/2025`, `test/2026`) are still appended beneath it.

A season whose logs are missing skips with an explanatory message;
`ArgumentHandlingTest` still runs. Each manifest lists the exact filenames its
goldens were recorded against, and the suite reports **unexpected** logs as well
as missing ones — an extra file in the folder changes the output, so goldens
recorded against a different set would test nothing.

## Scenarios

| Scenario | Config | Covers |
|---|---|---|
| `shipped_config` | the season's `shippedConfig` | The example config, whose output the top-level README documents verbatim |
| `value_count` | [`fixtures/2025/configs/value_count.json`](fixtures/2025/configs/value_count.json) | Value analysis requesting `count` with **no** `timeAnalysis` — regression test, see below |
| `unfiltered` | [`fixtures/2025/configs/unfiltered.json`](fixtures/2025/configs/unfiltered.json) | All robot-state filters off (`robotMode: "both"`), plus `outlier_2std` |
| `missing_entries` | [`fixtures/2025/configs/missing_entries.json`](fixtures/2025/configs/missing_entries.json) | Entries absent from the logs are skipped without crashing |
| `ArgumentHandlingTest` | — | Bad argv, missing folder, missing config, folder with no logs |

The `value_count` scenario exists because the aggregated value analysis
used to read the *time* analysis's local variables, which raised
`UnboundLocalError` whenever a value analysis requested `count` without a prior
time analysis. The shipped config never requests `count` on a value analysis, so
a golden test of the shipped config alone would not have caught it.

## A deliberate absence in the 2026 fixture

`fixtures/2026/configs/checks.json` lists `FRONT` in the camera rule's
`expectEntries` alongside the four real cameras. **The robot has no FRONT
camera** — it is there so the golden exercises the absence path end to end, since
all four real cameras are present in all nine logs. The resulting
`entry not present in this log` finding is expected, not a defect.

## The pit log in the 2026 set

`akit_26-04-29_19-26-56.wpilog` is a **pit session, not a match** — the robot
logs whenever it is powered, so a real synced folder contains logs like it. It is
kept in the fixture set deliberately, because that is realistic.

Two consequences to be aware of when reading the 2026 goldens:

- `Files processed` is **10**, and per-file averages are divided by 10 even
  though only nine files are matches. Those averages are therefore diluted; the
  totals are not. That is what the goldens record, because they do not pass
  `--matches-only`; running with that flag restores the averages (BCH 2.70 ->
  3.00) and names the skipped log.
- `tests/test_match_detection.py` uses this log as the only real negative example
  of `/DriverStation/FMSAttached`, which is what tells a match from a pit session.
  Enabled time does not: this log has 289 s of enabled time against a match's
  161 s.

## Updating goldens

When you change the output deliberately, re-record and **review the diff** before
committing — that diff is the point of the suite:

```bash
UPDATE_GOLDEN=1 python3 -m unittest discover -s tests
git diff tests/fixtures/*/golden/
```

Tests report as *skipped* while recording, since nothing is being asserted.

## Determinism

Output contains no wall-clock times, absolute paths, or hash-ordered iteration,
so runs are byte-identical on the same machine. Floating-point results are
printed to six decimals; a different CPU or Python build could in principle
differ in the last digit, in which case re-record the goldens on that machine.
