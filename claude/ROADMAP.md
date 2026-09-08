# log-analyzer — Roadmap

Companion to [DESIGN.md](DESIGN.md), which describes the tool as it stands. This
one covers where it goes next: how the tool is actually used, three requested
features, what measuring the logs revealed, and the order to build in.

Written 2026-09-07, against the post-optimization tree (analyzer runs both sample
logs in ~7 s; golden suite in [tests/](../tests/) is green).

## 1. Requested Features

1. **Auto-download logs** when the roboRIO is reachable on the local network.
2. **Automated post-match checks** producing a summary of potential concerns, or a
   dashboard that makes them visible. Named examples:
   - CANivore invalid-network errors
   - swerve motors disabled while the robot is enabled
   - other motors disabled/rebooting while enabled (e.g. the intake roller)
3. **Search for an event** and get back the file and timestamp.
4. **Refresh test fixtures to 2026-season logs**, so new work is written against
   current logging conventions.
5. **Wildcards in entry names** — `/RealOutputs/Vision/*/sending frames` rather
   than naming each of the four cameras.
6. **Absence checks** — flag what *didn't* happen: an entry that never appeared,
   or a signal that never reached its expected state.

## 2. How the Tool Is Used

Two modes, with genuinely different requirements. Most design decisions below
follow from the difference.

| | **Practice review** | **Pit / between matches** |
|---|---|---|
| Input | a folder of many logs | one log, just off the robot |
| Time budget | minutes are fine | seconds — inside a 10–20 min turnaround |
| Question | open-ended, exploratory, statistical | a fixed checklist of known concerns |
| Output | aggregate stats across files | per-match incidents, glanceable |
| Config | hand-written per question | **zero** — built-in rules |
| Reader | you, thinking | anyone in the pit, at a glance |

Practice review is what the tool does today and does well. Pit mode is new, and
is what features 1 and 2 are really for.

### 2.1 The pit environment

- A local network joins the laptop, the roboRIO, and the driver station.
- Internet is *possible* via a wired hotspot, never assumed.
- The laptop also drives a **pit display** — basic stats, an event video stream,
  the upcoming schedule — but that is a **third-party site, not ours**. This
  project is independent of it and will not integrate with it.
- This tool gets its **own screen**, alongside that one.
- **No hosted server.** Anything that runs must run locally.

Owning a screen means feature 2 needs a real display surface of its own, readable
at a glance from a few feet away, and updating without anyone typing a command
between matches. See §5.

### 2.2 The pit chain, and its latency budget

```
match ends → robot returns to pit → download log → run checks → read result
```

Sync is the first link, which moves feature 1 from "independent, whenever" onto
the critical path for pit mode.

The good news is the budget is comfortable: scanning one 55 MB match log takes
**~3.5 s** after the recent optimization work. Pit mode therefore needs no index
and no cache — a single-file scan is well inside the turnaround. That means the
checks feature can ship early and standalone, before any storage work.

Practice review is what needs the index (§4), because it asks many questions
across many files.

## 3. What Measuring the Logs Showed

Measured on `akit_25-04-17_14-51-30_curie_q40.wpilog` (55 MB, 2,206,495 data
records, 515 distinct entries).

### 3.1 The data these features need is already logged

| Need | Entries present in the log |
|---|---|
| CANivore health | `/RealOutputs/CANivoreStatus/Status` (string), `ReceiveErrorCount` / `TransmitErrorCount` / `OffCount` / `TxFullCount` (int64), `Utilization` (float); same set under `/RealOutputs/CANStatus/` and `/SystemStats/CANBus/` |
| Motor dropouts | `/<Subsystem>/Connected`, `/Elevator/ConnectedLead`, `/Elevator/ConnectedFollower`, `/Climber/Connected`, `/Drivetrain/GyroConnected` (boolean; 62 status-ish entries total) |
| Faults | `/PowerDistribution/StickyFaults` (int64 bitfield) |
| General concerns | `/RealOutputs/Alerts/errors` \| `warnings` \| `infos` (string[]) — AdvantageKit's Alerts API |

`/RealOutputs/SystemStatus/<Subsystem>/Faults` also exists, but is **not a useful
source here**: it is only populated while running system tests in the pit, and
that workflow already has its own reporting. `Alerts/*` is the stream that matters
for post-match checks — it is live during matches.

**No robot-code changes are needed to build feature 2.** The detection work is
already being done on-robot; the analyzer just cannot read the results.

### 3.2 A prototype of the three checks found real incidents

Running the checks directly against both sample logs (~3.8 s per file):

```
[errors]   failed to refresh signals on CANivore: HwTimestampOutOfSync   (q40)
[errors]   failed to refresh signals on RIO: CanMessageStale             (e6)
[errors]   camera0 / camera1 / camera2 / camera3 is disconnected         (both)
[warnings] No operator controller(s) connected.                          (q40)
[warnings] Non-competition operator controller connected.                (q40)
[warnings] Please wait to enable, JITing in progress.                    (both)
```

Two findings worth carrying forward:

- **`CANivoreStatus/Status` read `OK` for the whole match in both files.** The real
  CANivore problem surfaced only through `Alerts/errors`. A check written against
  the status string alone would have reported all-clear. Detectors should read the
  Alerts stream first and treat status/counter fields as corroboration.
- **`Connected` never went false while enabled in either file.** The motor-dropout
  detectors have no positive fixture yet — a green result would be
  indistinguishable from a broken detector. See §9.

### 3.3 The blocker

`/RealOutputs/Alerts/errors` is `string[]`. Array types crash today — `Log` never
defines `put_boolean_array` / `put_number_array` / `put_string_array`
(defects #1–2 in [DESIGN.md §8](DESIGN.md#8-known-defects-found-during-review)).
There are **73 array-typed entries** in that one log.

The richest source for feature 2 is currently unreadable. This gates everything.

### 3.4 Record mix argues for a two-tier store

| Type | Records | Share | Size | Entries |
|---|---:|---:|---:|---:|
| `double` | 1,381,252 | 62.6% | 11.1 MB | 155 |
| `struct:*` | 519,645 | 23.6% | 21.1 MB | 60 |
| `int64` | 172,545 | 7.8% | 1.4 MB | 66 |
| `double[]` | 104,161 | 4.7% | 5.1 MB | 8 |
| `boolean` | 15,991 | 0.7% | 0.03 MB | 140 |
| `string` | 510 | <0.1% | <0.01 MB | 39 |
| everything else | ~12,400 | 0.6% | 0.2 MB | 47 |

- `double` + `struct` = **86.2%** of records and 32.2 MB — high-rate numeric
  telemetry. The *context* you inspect after locating a moment, rarely the thing
  you search for.
- `boolean` + `string` = **0.7%** of records and ~30 KB. State transitions, mode
  changes, fault text — precisely what checks and searches operate on.
- Adding `int64` (counters, fault bitfields) still totals only 8.6% / 1.4 MB.

Indexing the state tier for a whole season is cheap: ~1.4 MB/file, so 60 matches
fit in well under 100 MB.

## 4. The Architectural Fork (practice mode)

For pit mode there is no fork — one file, one scan, 3.5 s (§2.2). For practice
review across a season library there is: **single-pass batch vs. extract-once,
query-many.**

Today the config decides what gets captured, so every new question re-reads every
file (see [DESIGN.md §3.4](DESIGN.md#34-ingest-and-filtering-analysispy-process_log_file)).
At ~3.5 s per file that is roughly **3.5 minutes per question** across 60 matches.
Feature 3 is unusable at that latency regardless of how the query is entered.

### Proposed layering

```
  .wpilog files
        │
        │  extract — once per file, ever
        ▼
  index (SQLite: a local file, no daemon, stdlib driver)
    ├─ all boolean / string / int64 / string[] samples   (~1.4 MB per file)
    ├─ manifest for numeric fields: name, type, count,
    │    time range, min/max/mean — not the samples
    └─ match metadata parsed from the filename (event, match, date)
        │
        │  query — instant
        ▼
  ├─ checks / detectors  (feature 2; also runs standalone on one file)
  ├─ event search        (feature 3)
  ├─ baseline comparison ("is this match worse than our season normal?")
  └─ existing time & value analyses
        │
        ▼
  ├─ terminal text  (today's output, preserved)
  ├─ static HTML    (the pit screen; also archivable per match)
  └─ JSON           (scripting, archival, a future UI)
```

Numeric detail stays in the `.wpilog`, which remains the source of truth. When an
analysis needs samples the index doesn't hold — the
`DriveToReef/.../translation/y` value analysis, for instance — re-read that one
file for that one window (~3.5 s, once), or add specific numeric entries to an
indexed allowlist.

Most existing analyses land in the fast tier already: the shipped config's time
analyses key off string state machines and booleans.

**The index is required by feature 3 regardless of the front end.** It is not a
bet on a UI. SQLite fits the local-only constraint directly — a single file, no
server process, readable by a CLI and a local Next.js app alike.

## 5. Deployment: Local Only

Settled by §2.1: nothing hosted, and this tool drives its own pit screen.

The enabling step regardless of surface is **A2 — separating computation from
formatting**. One computation then feeds three emitters:

- **terminal text** — what exists today, for practice review;
- **static HTML** — the pit screen, and a self-contained archive per match;
- **JSON** — scripting, archival, and whatever UI comes later.

### 5.1 The pit screen

Recommended: a **self-contained HTML file with its data baked in**, rewritten in
place after each match, displayed by a browser doing
`<meta http-equiv="refresh" content="30">`.

That combination is worth being deliberate about:

- **Data inlined, not fetched.** Browsers block `fetch`/XHR against `file://`
  origins, so a page that tried to load a sibling `report.json` would silently
  fail on the exact setup we want. Baking the data into the page sidesteps it
  entirely — and makes each report a single archivable artifact.
- **Rewritten in place**, so meta-refresh alone updates the screen. No server, no
  websocket, no manual reopen between matches.
- **No dependencies**, so nothing to install or maintain on a competition laptop.

Paired with **A6 (watch mode)** the pit chain becomes hands-off end to end:

```
robot returns → sync pulls the log → watcher analyzes it
             → report.html rewritten → screen refreshes itself
```

Nobody types anything between matches, which is the point.

### 5.2 If that proves limiting

Two upgrades, in order of cost:

1. **A tiny local HTTP server** (stdlib `http.server`, ~50 lines). Lifts the
   `file://` restrictions, enabling JSON polling, a match-history browser, and
   push-style updates. Still local-only, still no dependencies.
2. **A local Next.js app**, if the screen needs to become genuinely interactive.
   It would read the same JSON and the same SQLite file. Note the cost: Node and
   npm become things that must work on a competition laptop, offline.

Neither is needed to ship Milestone A, and both stay available because the
computation layer does not care which one renders.

## 6. Entry Patterns (Wildcards)

Feature 5. Today every entry must be named in full, so watching four cameras means
four near-identical config stanzas:

```
/RealOutputs/Vision/BCH/sending frames
/RealOutputs/Vision/BCL/sending frames
/RealOutputs/Vision/BL/sending frames
/RealOutputs/Vision/BR/sending frames
```

One pattern should cover them: `/RealOutputs/Vision/*/sending frames`.

This is not a niche convenience. In a single 2026 log there are **44 distinct
patterns matching three or more sibling entries** — every joystick, every swerve
corner, every camera, every subsystem's `Connected` / `Faults` pair.

### 6.1 Where patterns apply

Three places, with different consequences:

| Site | Effect |
|---|---|
| **Capture selection** (`target_entry_names`) | Widens the set of records kept. Mechanical. |
| **Analysis references** (`entry`, `triggerEntry`, `startEntry`, `endEntry`) | One config stanza **fans out** into N analyses. Semantic change — see §6.3. |
| **Detector rules** (A4) | Essential. "Any `*/Connected` false while enabled" *is* the motor-dropout check; enumerating devices by hand defeats the purpose. |

### 6.2 Syntax

Recommended: **segment-scoped glob**, where `/` is a hard boundary.

- `*` matches within one `/`-delimited segment
- `**` spans segments
- `?` and `[…]` behave as in `fnmatch`, within a segment

So `/RealOutputs/Vision/*/sending frames` matches exactly the four cameras and
cannot accidentally reach deeper, while `/RealOutputs/**/Connected` reaches any
depth when that is what you want.

Plain `fnmatch` is rejected: its `*` crosses `/`, which for hierarchical keys
produces surprising over-matching — `/RealOutputs/*/state` would match
`/RealOutputs/a/b/c/state`. Regex is rejected as unreadable inside JSON.

### 6.3 Fan-out semantics

`/RealOutputs/Vision/*/sending frames` can mean two different things:

- **Per-match** — four analyses, reported separately, one per camera.
- **Pooled** — one analysis over the union of all four cameras' values.

**Default to per-match.** The point of the vision example is finding out *which*
camera stopped sending frames; pooling discards exactly that. Pooling is still
useful ("overall vision availability"), so make it an explicit opt-in — e.g.
`"combine": true` on the analysis.

Per-match fan-out needs a label per result, and the natural label is the text the
wildcard captured (`BCH`, `BCL`, `BL`, `BR`). Which means:

> **Wildcards require the named-analyses fix (§9).** One stanza now yields N
> results, and identifying them by list position no longer works.

### 6.4 The struct-flattening ordering problem

This is the subtle part, and the reason the current matching is written backwards.

Capture selection happens *before* decoding: the tool decides whether to keep a
record from the entry name in its start record. But struct, JSON and msgpack
payloads flatten into synthetic child fields *after* decoding (see
[DESIGN.md §3.2](DESIGN.md#32-field-store-logpy)). A config naming a struct leaf
therefore refers to a field that does not exist at the moment the keep/discard
decision is made — which is why the current test asks whether the logged name is a
substring of the configured name.

With patterns, resolve it in two stages instead:

1. **At capture** — keep the record if its entry name could *produce* a field
   matching the pattern: the entry name matches the pattern, or is a prefix of the
   pattern at a segment boundary. Deliberately over-captures; that is correct and
   cheap.
2. **At analysis** — resolve the pattern against the actual field names present
   after flattening, and fan out over those.

That makes the struct-leaf case an explicit rule rather than a side effect of
substring matching, and it stops unrelated entries being captured merely because
their names happen to be substrings of a configured one.

### 6.5 Cost

Pattern matching is more expensive per test than a string compare, but it stays
off the hot path: whether an entry is captured is already decided **once per
entry** at its start record and cached in `entry_flags`
(see [DESIGN.md §7](DESIGN.md#7-performance-profile)). Matching a few dozen
patterns against ~550 distinct entry names is negligible against 4.36M records.
Any future change must preserve that memoization — testing patterns per *record*
would undo the optimization work outright.

### 6.6 Discoverability

Once a config can match things you did not enumerate, you need to see what it
matched. Add a dry-run: given a pattern and a log (or the index, once B1 exists),
print the entries it selects and their types — before committing to a run over a
folder. This also gives a fast answer to "what did we even log this year?", which
is currently a manual scan.

## 7. Absence Checks

Feature 6, and the half of the checks problem that the existing analyses cannot
express at all.

Every analysis today is **event-driven**: it scans for something happening and
reports it. But some of the most important post-match findings are things that
*did not* happen — and you cannot find a missing thing by scanning for it.

### 7.1 Absence is not self-interpreting

Two real cases from the 2026 logs, structurally identical and opposite in meaning:

| Entry | What the log shows | Interpretation |
|---|---|---|
| `/RealOutputs/Intake/RollerStalled` | Set `false` on the first periodic cycle, never changes | **Good.** It only goes `true` on a stall. Never stalling is the desired outcome. |
| `/RealOutputs/Vision/BCL/sending frames` | Entry absent from 4 of 9 logs | **Bad.** The camera never came up. |

Same shape — no transitions, or no entry — and no way to tell them apart from the
data alone. The discriminator has to be declared.

### 7.2 Declare the expected state

Each rule states what normal looks like, and the check reports the deviation:

| Expectation | Finding when violated |
|---|---|
| `present` — the entry exists in every match | entry absent → the device never reported |
| `always: false` — boolean stays false throughout | any `true` → a stall, a fault, a dropout |
| `at_least_once: true` — boolean reaches true at some point | never true → the subsystem never armed |
| `initial_only` — logged once and never changes | any change → unexpected transition |

`RollerStalled` is `always: false`; a `true` is the finding. `sending frames` is
`present` plus `always: true` while enabled; both absence and a `false` are
findings. Neither is expressible today.

### 7.3 Why this needs its own pass

Absence checks invert the scan. The existing analyzers iterate the entries a
config names and report what they contain; an absence check must know the entry
was *expected* and notice it never arrived. Two consequences:

- **The expected set has to be enumerated up front** — from the rule set, and with
  wildcards (§6) expanded against a *reference* set of entry names rather than
  against the log at hand. Otherwise a pattern matching nothing in a log where the
  camera never came up simply produces no rules, and the silence is invisible.
  The index (B1) is the natural home for that reference set: "entries we normally
  see at this event."
- **A skipped analysis must be distinguishable from a matched one.** This is
  already visible in the current output — until recently a skipped analysis printed
  a blank file name, and `RollerStalled` reports as "missing fields" in all nine
  2026 logs because its single startup record is filtered out by `enabled` /
  `fmsAttached`. Absence checks need "not captured" and "captured, no matches" to
  be different states, not both rendered as zero.

### 7.4 Sequencing

Belongs with A4 (checks), and shares its rule format. The `present` and
`always` / `at_least_once` variants work per-file and need nothing new. The
"entry we normally see is missing" variant is stronger with a season baseline, so
it can start as a static expected-entry list in the rule set and get better once
B1 exists.

## 8. Sequence

### Milestone A — Pit mode

The post-match checklist, end to end. Nothing here needs the index.

| # | Step | Why | Depends on |
|---|---|---|---|
| A0 | ~~**Refresh fixtures to 2026 logs**~~ (§9) — **done** | Detector rules should be written against current entry names, not 2025 ones | — |
| A1 | ~~**Fix array support**~~ (defects #1–2) — **done** | Unlocks `Alerts/*` — most of feature 2's value | — |
| A2 | ~~**Split computation from formatting**~~ — **done** | Prerequisite for HTML and JSON output | — |
| A3 | ~~**Entry patterns / wildcards**~~ (§6) — **done** | Detector rules are unwritable without it; shortens every config | A1 |
| A4 | **Checks as a third analysis kind**, incl. absence checks (§7) | Replaces the manual post-match pass | A2, A3 |
| A5 | **roboRIO sync** | First link in the pit chain (§2.2) | — |
| A6 | **HTML + JSON emitters** | The pit screen itself (§5.1) | A2, A4 |
| A7 | **Watch mode** | Closes the pit chain: no commands typed between matches | A5, A6 |

### Milestone B — Library / practice mode

| # | Step | Why | Depends on |
|---|---|---|---|
| B1 | **Extract / index layer** (SQLite) | Makes search viable, checks instant across a season | A2 |
| B2 | **Event search** (feature 3) | The reason the index exists | B1 |
| B3 | **Baseline comparison** | "Is this match worse than our normal?" — needs history | B1, A4 |

### Milestone C — Optional

| # | Step | Why | Depends on |
|---|---|---|---|
| C1 | **Local web UI** (Next.js) | Only if the static report and JSON feed prove insufficient | B1, A5 |

### Notes per step

**A1 — arrays.** `Log` needs `put_boolean_array` / `put_number_array` /
`put_string_array`; `LogField` needs the matching `get_*_array`. Then extend the
`LoggableType` dispatch in both analyzers, which currently skip anything that is
not STRING / BOOLEAN / NUMBER.

Done. Covered by the `arrays` scenario in the 2026 fixture set, which exercises
`string[]` in all three positions: as a time-analysis start/end value, as a value
analysis trigger (`"triggerValue": []`), and as a captured value.

Scope note: this makes array entries *ingestible and capturable* — the
prerequisite for reading `Alerts/*`. It does not make them statistically
interesting: `average` / `min` / `max` over a list is meaningless, and
`print_results_and_calculations` will report "no numeric values". Turning an
alert stream into findings is A4's job, not this step's.

**A2 — compute/format split.** Done. `compute_analysis()` /
`compute_per_file_counts()` return dataclasses carrying every figure and its
locations; `format_analysis()` / `format_per_file_counts()` render those as text
lines; the `print_*` functions are thin wrappers over the pair. A6's emitters
attach at the dataclass, which `dataclasses.asdict()` serializes straight to JSON
(covered by `tests/test_computation.py`).

**A3 — entry patterns.** Done, in `entry_patterns.py`. Replaces the
reversed-substring matching rather than layering on it. Verified equivalent: one
`/RealOutputs/Vision/*/sending frames` stanza reproduces the four explicit camera
stanzas byte for byte.

Two things learned in the build, both worth keeping in mind:

- **Interior empty segments are significant.** Normalising `//` away broke the
  `/RealOutputs//ShooterModes/DistanceToHub` match (§9.1); only the leading
  slash's empty segment is dropped.
- **Expansion order must be sorted, not first-seen.** Expanding per file means the
  first log decides the order, and the first 2026 log lacks BCL — which put the
  cameras in BCH, BL, BCL, BR order. The aggregate now sorts by key.

Not yet done from §6: the dry-run entry lister (§6.6), and `"combine": true`
pooling (§6.3) — fan-out is per-match only, which is the useful default.

**A4 — checks.** Different output shape from the existing analyses: a list of
`(file, timestamp, severity, message)` incidents rather than a numeric series for
statistics. Keep it declarative like `timeAnalysis` / `valueAnalysis`, but ship a
**default rule set** so it runs with no config — pit mode is zero-config by
definition. Rule shape: entry pattern, predicate, gating condition (e.g. "while
enabled"), severity, message template.

**A5 — roboRIO sync.** A separate program sharing only an output folder. Reach the
robot at `roborio-<team>-frc.local` or `10.TE.AM.2`; AdvantageKit commonly logs to
USB (`/media/sda1` / `/media/sdb1`) rather than `/home/lvuser/logs`, so check both.
Copy-then-verify, never move; dedupe on content hash. Must degrade quietly when
the robot is absent — it will be, most of the time.

**A7 — watch mode.** A long-running `--watch <folder>` that notices new `.wpilog`
files (dropped there by A5), analyzes each once, and rewrites the report. Debounce
on file size settling — a log still being copied must not be analyzed early. Keep
it restartable and idempotent: track which files have been processed by content
hash, the same identity B1 uses, so a restart mid-event does not redo the day or
skip a match.

**B1 — index.** Store the state tier in full plus a numeric manifest. Key on file
identity (content hash, not name) so re-imports from A4 are idempotent. Keep the
schema versioned so a format change can rebuild rather than migrate — extraction
is cheap and the `.wpilog` files remain authoritative.

## 9. Test Fixtures and Logging Conventions

**Done.** The suite is season-parameterized; see
[tests/README.md](../tests/README.md) for the mechanics.

```
tests/fixtures/<season>/manifest.json    logs, log folder, shipped config
tests/fixtures/<season>/configs/*.json   extra scenarios beyond the shipped config
tests/fixtures/<season>/golden/*.txt     recorded stdout, one per scenario
```

Scenarios are discovered rather than registered — a season's `shippedConfig` plus
every `configs/*.json` — so a new case is a config plus a recorded golden. A
season whose logs are absent skips; the manifest lists exact filenames and the
suite flags *unexpected* logs too, since an extra file changes output and would
invalidate the goldens.

Two seasons are set up:

| Season | Logs | Config | Role |
|---|---:|---|---|
| 2025 | 2 (~108 MB) | `config2025.json` | Frozen regression baseline |
| 2026 | 9 (~387 MB) | `config2026.json` | Development baseline |

The 2025 set is kept deliberately: it lets a refactor prove it changed nothing on
old data while new work is written against current conventions.

### 9.1 What the 2026 changeover cost

Worth recording, because the next changeover will look the same. Entry names are
**game-specific and do not survive a season**. Of the four entries
`config2025.json` referenced, three vanished in 2026:

| 2025 | 2026 |
|---|---|
| `/RealOutputs/Manipulator/State` (`SHOOT_CORAL`, `WAITING_FOR_CORAL`) | `/RealOutputs/ShooterModes/CurrentMode` (`SHOOT_OTM`, `COLLECT_AND_HOLD`, …) |
| `/Manipulator/IsIndexerIRBlocked` | `/Shooter/FuelDetectorHasFuel` |
| `/RealOutputs/DriveToReef/...` | gone |
| `/RealOutputs/LEDS/state` | same entry, entirely new value set |

So a season changeover is "write a new config and record new goldens", not
"re-record". Budget for it.

Three failure modes showed up while validating `config2026.json`, all of which a
config linter (§10) would have caught before a 30-second run:

- **A type mismatch that silently matches nothing** — `"12"` as a string against a
  `double` entry. `12.0 == "12"` is `False`, so the analysis simply never fired.
- **A filter/entry conflict** — an auto-only entry analyzed under
  `robotMode: "teleop"`, so its records were filtered out at capture.
- **A typo in the *robot code*, not the config** —
  `/RealOutputs//ShooterModes/DistanceToHub` is logged with a double slash in all
  nine files. `config2026.json` matches the log deliberately. Do not "correct" it:
  the single-slash form captures nothing and reports no values rather than
  erroring.

**Also worth building here:** a small `.wpilog` synthesizer. A few-KB synthetic log
committed to the repo would let the suite run anywhere without 100+ MB of
fixtures, and — more importantly — would give the motor-dropout detectors the
positive fixture they currently lack (§3.2). Without it, those detectors cannot be
distinguished from ones that never fire.

## 10. Cross-Cutting Work

Fix these along the way — each will otherwise distort a feature above:

- **Entry matching is reversed substring** (`entry.name in configured_name`, see
  [DESIGN.md §3.4](DESIGN.md#34-ingest-and-filtering-analysispy-process_log_file)).
  Superseded by A3; §6.4 covers how to keep the struct-leaf case working without it.
- **Analyses are keyed by list position** (`analysis_idx`). Reports and JSON output
  need stable named ids — and wildcard fan-out (§6.3) makes this a prerequisite of
  A3, not a nicety.
- **`.schema` handling assumes `struct:`** (defect #4) and will `IndexError` on a
  protobuf schema entry.
- **No config validation.** Every failure mode in §9.1 produced a clean run with
  empty results rather than an error, which is the worst possible feedback. A
  linter — check each referenced entry exists in the target logs, that the
  configured value's type matches the entry's, and that the entry is actually
  recorded under the configured `robotMode` — would catch all three before a
  multi-minute run. Cheap once B1's manifest exists; useful enough to do sooner.

## 11. What Must Not Regress

The folder-wide aggregate analysis is the feature that replaced hours of
one-file-at-a-time work in AdvantageScope. It is the thing to protect through
every step above, and it is the whole of practice-review mode.

[tests/](../tests/) pins its exact output byte-for-byte, including the sample
output the top-level README documents. Any step here that changes printed output
should appear as a reviewed diff in `tests/golden/`, never as a silent change —
see [tests/README.md](../tests/README.md).
