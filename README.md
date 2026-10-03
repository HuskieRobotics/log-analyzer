# log-analyzer

A Python tool for analyzing WPILib DataLog (`.wpilog`) files from an FRC robot.

It is used two ways:

- **After a practice session**, pointed at a folder of logs, to compute statistics
  across every file at once — cycle times, extremes, outliers. This is the
  original purpose and the reason the folder-wide aggregate exists.
- **In the pit between matches**, to fetch the newest log off the roboRIO, run a
  set of automated checks over it, and rewrite an HTML page on a second screen —
  with nothing typed between matches.

## The three tools

| Tool | What it does |
|------|--------------|
| `analysis.py` | Reads logs and produces analyses, checks and reports. Everything else composes this. |
| `sync_logs.py` | Copies new logs off the roboRIO over SSH. Standalone; needs no config. |
| `pit_monitor.py` | Loops: sync, pick the newest finished match, re-run the analysis, rewrite the report. |

## Quick start

```bash
pip install msgpack

# Practice review: every log in a folder
python analysis.py ./test/2025 config2025.json

# Pit checks on the newest match, as an HTML page
python analysis.py ./logs checks2026.json --matches-only --latest --html report.html

# Hands off: fetch from the robot and keep the page current
python pit_monitor.py ./logs checks2026.json --html report.html --sync-from 10.30.61.2
```

## analysis.py

```bash
python analysis.py <log_folder> <config_json_file> [options]
```

| Option | Description |
|--------|-------------|
| `--html PATH` | Also write a self-contained HTML report |
| `--json PATH` | Also write the whole run as JSON |
| `--refresh SECONDS` | HTML auto-refresh interval; `0` disables (default 30) |
| `--matches-only` | Skip logs recorded outside a match (pit sessions), which otherwise dilute every per-file average |
| `--latest` | Analyze only the most recent finished log, so a page left open always shows the newest match |
| `--settle-seconds SECONDS` | With `--latest`, the gap over which the chosen log must not grow; `0` skips the check (default 1) |
| `--verbose` | Include captured values and a record summary |

### Choosing which logs to analyze

A powered robot logs continuously, so a synced folder holds pit sessions
alongside matches, and the newest file is usually still being written.

- **`--matches-only`** keeps only logs in which the robot was ever FMS-attached.
  Nothing in the file name reliably says which is which — the event name and
  match number can be missing — and neither does whether the robot was enabled or
  for how long, since a pit session can be enabled longer than a match.
  `/DriverStation/FMSAttached` is the dependable signal.

  Classifying a *match* stops at the first attached record and is nearly free.
  Proving a log is *not* a match requires a full read, so the verdict is cached in
  a `.match-cache.json` sidecar in the log folder, keyed by name and size, and
  shared with `pit_monitor.py`. Delete the file to force reclassification.

- **`--latest`** narrows to the most recent *finished* log, ordered by the
  timestamp in the file name rather than its mtime, which `scp` resets to copy
  time. A log still growing is skipped and named in the report, so you can see
  whether the robot is logging a match or just sitting in the pit.

## Configuration

One JSON file holds filtering, analyses and checks. Every section is optional.

```json
{
    "enabled": true,
    "fmsAttached": true,
    "robotMode": "teleop",
    "timeAnalysis": [...],
    "valueAnalysis": [...],
    "checks": [...]
}
```

The repository ships three examples: `config2025.json` and `config2026.json`
(analyses, for practice review) and `checks2026.json` (checks, for the pit).

### Filtering options

These apply to `timeAnalysis` and `valueAnalysis`. Checks do their own gating with
`while` — see below.

| Property | Type | Values | Description |
|----------|------|--------|-------------|
| `enabled` | boolean | `true`/`false` | Only capture records when the robot is enabled |
| `fmsAttached` | boolean | `true`/`false` | Only capture records when FMS is attached |
| `robotMode` | string | `"auto"`, `"teleop"`, `"both"` | Filter by robot mode |

### Entry names and wildcards

Any configured entry name may be a pattern. Entry names are `/`-delimited paths
and `/` is a hard boundary:

| Pattern | Matches |
|---------|---------|
| `*` | within a single segment |
| `**` | zero or more whole segments |
| `?` | one character within a segment |
| `[abc]` | a character class within a segment |

So `/RealOutputs/Vision/*/sending frames` means "each camera" rather than
"anything below Vision". A name containing none of `* ? [` is a literal.

One rule fans out to every matching entry, each producing its own finding. Structs
are flattened into child entries, so a `Pose2d` logged at
`/RealOutputs/Drivetrain/Pose` is also addressable as
`/RealOutputs/Drivetrain/Pose/translation/x`. Array entries gain a synthetic
`/length` child.

### Time analysis

Calculates the duration between a start event and an end event, once per cycle.

```json
"timeAnalysis": [{
    "startEntry": "/RealOutputs/Manipulator/State",
    "startValue": "WAITING_FOR_CORAL",
    "endEntry": "/RealOutputs/LEDS/state",
    "endValue": "SCORING",
    "calculations": [
        {"type": "average", "name": "Average Cycle Time"},
        {"type": "max", "name": "Max Cycle Time"},
        {"type": "min", "name": "Min Cycle Time"},
        {"type": "count", "name": "Cycle Count"},
        {"type": "outlier_2std", "name": "Outliers (2 std dev)"}
    ]
}]
```

| Property | Type | Description |
|----------|------|-------------|
| `startEntry` | string | Entry that marks the start of a cycle |
| `startValue` | any | Value that triggers the start timestamp |
| `endEntry` | string | Entry that marks the end of a cycle |
| `endValue` | any | Value that triggers the end timestamp |
| `calculations` | array | Calculations to perform on the time differences |

### Value analysis

Captures values from one entry when a trigger condition is met on another.

```json
"valueAnalysis": [{
    "entry": "/RealOutputs/DriveToReef/difference (reef frame)/translation/y",
    "entryUnit": "m",
    "triggerEntry": "/Manipulator/IsIndexerIRBlocked",
    "triggerValue": false,
    "calculations": [
        {"type": "average", "name": "Average Y Diff at Reef"},
        {"type": "abs_max", "name": "Max Y Diff at Reef (abs)"},
        {"type": "abs_outlier_2std", "name": "Outliers (2 std dev; abs)"}
    ]
}]
```

| Property | Type | Description |
|----------|------|-------------|
| `entry` | string | Entry to capture values from |
| `entryUnit` | string | Unit to display for values (optional) |
| `triggerEntry` | string | Entry to monitor for the trigger condition |
| `triggerValue` | any | Value that triggers capture |
| `calculations` | array | Calculations to perform on the captured values |

### Calculation types

| Type | Description | Applies to |
|------|-------------|------------|
| `average` | Arithmetic mean | Time differences, numeric values |
| `min` | Minimum | Time differences, numeric values |
| `max` | Maximum | Time differences, numeric values |
| `count` | Count of items | Time differences, numeric values |
| `abs_average` | Mean of absolute values | Numeric values |
| `abs_min` | Minimum of absolute values | Numeric values |
| `abs_max` | Maximum of absolute values | Numeric values |
| `outlier_2std` | Values more than 2 standard deviations from the mean | Time differences, numeric values |
| `abs_outlier_2std` | As above, on absolute values | Numeric values |

## Checks

A check states what the log should show and reports where it did not. This is the
section that automates the post-match inspection that used to be done by hand in
AdvantageScope.

```json
"checks": [
    {
        "name": "Alert reported as error",
        "entry": "/RealOutputs/Alerts/errors",
        "expect": "empty",
        "severity": "error"
    },
    {
        "name": "Drive motor over temperature",
        "entry": "/Drivetrain/*/DriveTemp",
        "expect": {"above": 70},
        "clearBelow": 65,
        "minDuration": 5.0,
        "unit": "C",
        "while": "afterFirstEnable",
        "severity": "warning"
    }
]
```

### Rule properties

| Property | Description |
|----------|-------------|
| `name` | Rule name, as it appears in findings |
| `entry` | Entry name or pattern to check |
| `excludeEntry` | Pattern, or list of patterns, whose matches are removed from `entry`'s matches |
| `expect` | What the entry should show — see below |
| `expectEntries` | Names that `entry`'s wildcard **must** expand to; a missing one is a finding of its own |
| `while` | Gate: `enabled`, `disabled`, `afterFirstEnable`; omit for the whole log |
| `severity` | `error`, `warning` or `info` |
| `unit` | Unit to display in findings |
| `clearBelow` / `clearAbove` | Hysteresis: the value an excursion must return past to end |
| `minDuration` | Seconds an excursion must last before it is reported |
| `compare` | `siblings` — compare matched entries against each other |
| `statistic` | For `compare`: `peak`, `rise`, `mean` or `min` |
| `deviation` | For `compare`: `{"aboveSiblingsBy": n}` or `{"belowSiblingsBy": n}` |
| `minimumSiblings` | For `compare`: fewest matches needed before comparing (default 3) |

### `expect` forms

| Form | Meaning |
|------|---------|
| `"empty"` | The entry must be empty. For an array, **each element becomes its own finding** — this is how AdvantageKit's `Alerts` streams are read. |
| `"present"` | The entry must merely exist in the log |
| `{"always": v}` | Every sample must equal `v` |
| `{"never": v}` | No sample may equal `v` |
| `{"alwaysOneOf": [...]}` | Every sample must be one of these. Use when a status string has a legitimate "not yet reported" state as well as a good one. |
| `{"atLeastOnce": v}` | At least one sample must equal `v`; otherwise one finding |
| `{"above": n}` | Report excursions above `n`, with `clearBelow` and `minDuration` |
| `{"below": n}` | Report excursions below `n`, with `clearAbove` and `minDuration` |
| `{"minIncrease": n}` | A monotonic counter must advance by at least `n` across the gate window |

### Gates

`afterFirstEnable` is usually what a post-match report wants rather than
`enabled`. Sticky faults are published after the fact, so a fault caused during
the match is often logged once the robot is disabled and never again — gating
strictly on `enabled` discards them.

### Comparing siblings

Instead of an absolute threshold, compare entries that should behave alike: the
four drive motors, the four steer motors, a lead and its follower.

```json
{
    "name": "Drive motor hotter than its peers",
    "entry": "/Drivetrain/*/DriveTemp",
    "compare": "siblings",
    "statistic": "peak",
    "deviation": {"aboveSiblingsBy": 5},
    "unit": "C",
    "while": "afterFirstEnable",
    "severity": "warning"
}
```

Each matched entry is compared against the median of the others, so one bad
member cannot drag the baseline toward itself. This catches a motor that is
failing relative to its peers well before any fixed limit would.

For a motor with no sibling — a spindexer, a kicker — use absolute
`{"above": n}` pairs instead, one `warning` and one `error`.

### A convention: season constants

`checks2026.json` carries a `field` block recording the field dimensions and the
margin its pose-bounds thresholds derive from:

```json
"field": {
    "length": 16.54,
    "width": 8.07,
    "margin": 1.0,
    "season": 2026,
    "source": "REBUILT game manual: ... 317.7in (~8.07m) by 651.2in (~16.54m) ..."
}
```

**The tool ignores this block.** The thresholds in the rules are literal values;
`field` exists so the numbers behind them are stated once and cited, and a test in
`tests/test_checks.py` asserts the four rules stay derived from it. The field
changes between seasons by more than the margin allows, and getting it wrong does
not just loosen a bound — it rescales the healthy baseline every other threshold
was chosen against. Re-measure it each season rather than copying it forward.

## Reports

`--html` writes one self-contained page: all data inlined, nothing loaded
externally, so it works on a pit laptop with no internet. It carries a
`<meta http-equiv="refresh">`, so a browser left open on it tracks the newest
match without being touched.

The page leads with an **error band** above everything else, naming every
`error`-severity finding. Findings are grouped by rule, because they are not
evenly distributed — a folder-wide run over nine match logs raises 65 errors, 41
of them from one rule. Grouped, that is nine lines. Warnings are deliberately
excluded: a band that holds everything ranks nothing.

Below the band come notices for logs still being written or skipped as non-matches,
then the findings across all files, the aggregated analyses, and a collapsible
section per log file.

`--json` writes the same run as JSON for anything downstream.

## Pit workflow

### sync_logs.py

Copies new `.wpilog` files off the roboRIO over SSH. It needs no config file.

```bash
python sync_logs.py ./logs --host 10.30.61.2 --newest 5
```

| Option | Description |
|--------|-------------|
| `--host HOST` | roboRIO address (default `10.30.61.2`) |
| `--user USER` | SSH user (default `admin`) |
| `--identity PATH` | SSH private key to use |
| `--sshpass` | Authenticate via `sshpass`, needed for an empty password, which `ssh` cannot supply on its own. Not available on Windows — use a key. |
| `--password PASSWORD` | Password for `--sshpass` (default: empty) |
| `--remote-dir DIR` | Directory to search on the robot; repeatable (default `/media/sda1 /media/sda2 /U /home/lvuser/logs`) |
| `--from-local DIR` | Read from a local directory instead of a robot |
| `--allow-prompt` | Drop `BatchMode` so `ssh` may ask for a password. For first-time setup, not for an unattended loop. |
| `--since YYYY-MM-DD` | Only logs recorded on or after this date; a log whose name carries no date is skipped |
| `--until YYYY-MM-DD` | Only logs recorded on or before this date |
| `--newest N` | Only the N most recently recorded logs |
| `--min-size BYTES` | Skip logs smaller than this (`0` disables) |
| `--settle-seconds SECONDS` | Gap between two listings, used to detect the log the robot still has open; `0` disables (default 2) |
| `--probe` | Run increasingly demanding remote commands and report the first that fails; changes nothing |
| `--debug` | Print the exact `ssh`/`scp` commands being run |
| `--dry-run` | List what would be copied, copy nothing |

Date filters are applied **before** `--newest`, so `--since` with `--newest` can
select nothing. Use `--since` with `--until` to pick one event.

Transfers are atomic — copied to a temporary name and renamed — so a partial file
is never left where the analyzer would read it. The robot's open log is detected by
comparing two listings and left alone. An unreachable robot is reported and exits
cleanly, which is what lets the monitor loop survive a dropped network.

First-time setup needs the host key accepted once:

```bash
ssh admin@10.30.61.2        # confirm the fingerprint, then exit
python sync_logs.py ./logs --probe
```

### pit_monitor.py

Composes the other two rather than absorbing them, and loops.

```bash
python pit_monitor.py ./logs checks2026.json --html report.html \
    --sync-from 10.30.61.2
```

| Option | Description |
|--------|-------------|
| `--html PATH` | Report to rewrite in place (default `report.html`) |
| `--sync-from HOST` | Also fetch new logs from the roboRIO each cycle |
| `--sync-newest N` | Fetch only the N most recent logs (default 10); `0` fetches everything the robot has, which is rarely wanted |
| `--sync-arg ARG` | Extra argument passed through to `sync_logs.py`. Repeatable, and needs the `=` form so argparse does not read the value as an option of its own: `--sync-arg=--min-size --sync-arg=1000000` |
| `--interval SECONDS` | Seconds between cycles (default 20) |
| `--settle-seconds SECONDS` | Window a log must hold its size for (default 1) |
| `--once` | Run a single cycle and exit |

Each cycle syncs, works out which log *would* be analyzed, and re-runs the
analyzer only if that has changed. Nothing in a cycle is allowed to kill the loop:
an absent robot, an unreachable host, a bad config or a failed analysis are all
reported and the loop continues, because a pit display that dies at the wrong
moment is worse than a stale one.

Open `report.html` in a browser on the second screen and leave it. The meta
refresh does the rest.

The first run against a folder of unclassified pit sessions can take a minute or
more, because proving a log is not a match requires reading all of it. That pass
announces itself per file and is cached, so every later run is immediate.

## Data types supported

- `boolean` / `boolean[]`
- `int64` / `int64[]`
- `double` / `double[]`
- `float` / `float[]`
- `string` / `string[]`
- `json`
- `msgpack`
- structs whose schema is encoded in the log file, flattened into child entries

## Requirements

- Python 3.9+
- Standard library only, apart from one dependency
- `msgpack`

```bash
pip install msgpack
```

`sync_logs.py` additionally needs `ssh` and `scp` on the PATH. Windows 10 and 11
ship these as OpenSSH; `sshpass` is not available there, so use `--identity`.

## Tests

```bash
python3 -m unittest discover -s tests
```

The suite is a characterization suite: it pins current behavior, including the
example output below, so that any change to printed output shows up as a reviewed
diff rather than a surprise. It needs the `.wpilog` fixtures, which are too large
to commit — see [tests/README.md](tests/README.md) for how to supply them and how
to re-record goldens.

## Design notes

[claude/DESIGN.md](claude/DESIGN.md) describes the architecture, the layering and
the measured performance profile. [claude/ROADMAP.md](claude/ROADMAP.md) records
what is planned and, more usefully, the measurements behind each decision — why
`afterFirstEnable` rather than `enabled`, why `peak` rather than `rise` for
siblings, and how the pose-bounds thresholds were chosen.

## Error handling

The tool handles invalid or missing log files, malformed configuration, missing or
invalid data entries, and type conversion errors. Warnings and errors are reported
to help diagnose issues.

One gap worth knowing: **there is no config validation.** A rule naming an entry
that does not exist, or comparing against a value of the wrong type, produces a
clean run with empty results rather than an error. If a check is suspiciously
quiet, verify the entry name against the log.

## Example output

`python analysis.py ./test/2025 config2025.json`, which is what
`tests/fixtures/2025/golden/shipped_config.txt` pins verbatim:

```
=== FILTERING CRITERIA ===
Filter for enabled: True
Filter for FMS attached: True
Filter for robot mode: teleop

=== ANALYSIS ===
Found 2 log files to process:
  akit_25-04-17_14-51-30_curie_q40.wpilog
  akit_25-04-19_09-36-19_curie_e6.wpilog

Processing: akit_25-04-17_14-51-30_curie_q40.wpilog

=== TIME ANALYSIS RESULTS FOR akit_25-04-17_14-51-30_curie_q40.wpilog ===

Analyzing: /RealOutputs/Manipulator/State (SHOOT_CORAL) -> /Manipulator/IsIndexerIRBlocked (False)
  Total values captured in this file: 13
  Average Shooting Time: 0.120009 s
  Max Shooting Time: 0.140015 s
    @ 293.243832 s 
  Min Shooting Time: 0.100003 s
    @ 337.654072 s 

Analyzing: /RealOutputs/Manipulator/State (WAITING_FOR_CORAL) -> /RealOutputs/LEDS/state (SCORING)
  Total values captured in this file: 16
  Average Cycle Time: 6.804564 s
  Max Cycle Time: 9.882420 s
    @ 337.774090 s 
  Min Cycle Time: 3.967845 s
    @ 289.296021 s 
  Cycle Count: 16

=== VALUE ANALYSIS RESULTS FOR akit_25-04-17_14-51-30_curie_q40.wpilog ===

Analyzing: /RealOutputs/DriveToReef/difference (reef frame)/translation/y when /Manipulator/IsIndexerIRBlocked = False
  Total values captured in this file: 13
  Average Y Diff at Reef: -0.003209 m
  Max Y Diff at Reef: 0.012699 m
    @ 289.276043 s 
  Min Y Diff at Reef: -0.012387 m
    @ 293.383847 s 
  Average Y Diff at Reef (abs): 0.008027 m
  Max Y Diff at Reef (abs): 0.012699 m
    @ 289.276043 s 
  Min Y Diff at Reef (abs): 0.000263 m
    @ 337.754075 s 

Processing: akit_25-04-19_09-36-19_curie_e6.wpilog

=== TIME ANALYSIS RESULTS FOR akit_25-04-19_09-36-19_curie_e6.wpilog ===

Analyzing: /RealOutputs/Manipulator/State (SHOOT_CORAL) -> /Manipulator/IsIndexerIRBlocked (False)
  Total values captured in this file: 14
  Average Shooting Time: 0.112864 s
  Max Shooting Time: 0.120074 s
    @ 193.556561 s 
  Min Shooting Time: 0.099947 s
    @ 152.181621 s 

Analyzing: /RealOutputs/Manipulator/State (WAITING_FOR_CORAL) -> /RealOutputs/LEDS/state (SCORING)
  Total values captured in this file: 15
  Average Cycle Time: 7.291700 s
  Max Cycle Time: 16.860040 s
    @ 158.646889 s 
  Min Cycle Time: 4.157296 s
    @ 137.682737 s 
  Cycle Count: 15
  Outliers (2 std dev): 16.860040 s
    @ 158.646889 s 

=== VALUE ANALYSIS RESULTS FOR akit_25-04-19_09-36-19_curie_e6.wpilog ===

Analyzing: /RealOutputs/DriveToReef/difference (reef frame)/translation/y when /Manipulator/IsIndexerIRBlocked = False
  Total values captured in this file: 14
  Average Y Diff at Reef: -0.000523 m
  Max Y Diff at Reef: 0.012569 m
    @ 210.868924 s 
  Min Y Diff at Reef: -0.012030 m
    @ 175.586898 s 
  Average Y Diff at Reef (abs): 0.007575 m
  Max Y Diff at Reef (abs): 0.012569 m
    @ 210.868924 s 
  Min Y Diff at Reef (abs): 0.000153 m
    @ 187.954405 s 

=== AGGREGATED TIME ANALYSIS RESULTS ACROSS ALL FILES ===

Aggregated Analysis: /RealOutputs/Manipulator/State (SHOOT_CORAL) -> /Manipulator/IsIndexerIRBlocked (False)
  Files processed: 2
  Total values captured across all files: 27
  Average Shooting Time: 0.116304 s
  Max Shooting Time: 0.140015 s
    @ 293.243832 s in akit_25-04-17_14-51-30_curie_q40.wpilog
  Min Shooting Time: 0.099947 s
    @ 152.181621 s in akit_25-04-19_09-36-19_curie_e6.wpilog

Aggregated Analysis: /RealOutputs/Manipulator/State (WAITING_FOR_CORAL) -> /RealOutputs/LEDS/state (SCORING)
  Files processed: 2
  Average matched values per file: 15.50
  Minimum matched values in any file: 15 in akit_25-04-19_09-36-19_curie_e6.wpilog
  Maximum matched values in any file: 16 in akit_25-04-17_14-51-30_curie_q40.wpilog
  Total values captured across all files: 31
  Average Cycle Time: 7.040275 s
  Max Cycle Time: 16.860040 s
    @ 158.646889 s in akit_25-04-19_09-36-19_curie_e6.wpilog
  Min Cycle Time: 3.967845 s
    @ 289.296021 s in akit_25-04-17_14-51-30_curie_q40.wpilog
  Cycle Count: 31
  Outliers (2 std dev): 16.860040 s
    @ 158.646889 s in akit_25-04-19_09-36-19_curie_e6.wpilog

=== AGGREGATED VALUE ANALYSIS RESULTS ACROSS ALL FILES ===

Aggregated Value Analysis: /RealOutputs/DriveToReef/difference (reef frame)/translation/y when /Manipulator/IsIndexerIRBlocked = False
  Files processed: 2
  Total values captured across all files: 27
  Average Y Diff at Reef: -0.001816 m
  Max Y Diff at Reef: 0.012699 m
    @ 289.276043 s in akit_25-04-17_14-51-30_curie_q40.wpilog
  Min Y Diff at Reef: -0.012387 m
    @ 293.383847 s in akit_25-04-17_14-51-30_curie_q40.wpilog
  Average Y Diff at Reef (abs): 0.007793 m
  Max Y Diff at Reef (abs): 0.012699 m
    @ 289.276043 s in akit_25-04-17_14-51-30_curie_q40.wpilog
  Min Y Diff at Reef (abs): 0.000153 m
    @ 187.954405 s in akit_25-04-19_09-36-19_curie_e6.wpilog
```
