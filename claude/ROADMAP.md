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
7. **Threshold and duration checks** — flag a value past a limit and report for
   how long and across how many intervals (elevated motor temperatures).
8. **Comparative checks** — flag a value that is unusual *for this robot*: a motor
   running hotter than it normally does, as an early warning of failure.

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
  indistinguishable from a broken detector. See §11.

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

## 8. Threshold and Duration Checks

Feature 7. "Was any motor too hot, and for how long?"

Every expectation in §7 is point-in-time: a sample either violates the rule or it
does not. A threshold check is about **exposure** — how long a value stayed past a
limit, and in how many separate excursions. Peak alone does not answer it. A motor
touching 71 °C for two seconds is fine; sitting at 62 °C for four minutes is not.
`max` reports the first and nothing reports the second.

### 8.1 Shape

```json
{
    "name": "Drive motor over temperature",
    "entry": "/Drivetrain/*/DriveTemp",
    "expect": {"above": 60},
    "clearBelow": 55,
    "minDuration": 2.0,
    "severity": "warning"
}
```

Per matched entry, report interval count, total time past the limit, longest
interval, and the peak with its timestamp:

```
  [WARNING] Drive motor over temperature (/Drivetrain/FR/DriveTemp)
    above 60 for 42.3 s across 3 intervals, longest 21.7 s, peak 71.0 at 233.4 s
```

`{"below": V}` is the mirror image, for things that must stay up — battery voltage,
a pressure reading.

### 8.2 Four things to get right

**1. Duration comes from the gap to the next sample, not interpolation.** These
entries log on change at 1 °C resolution, so a long gap means the value *held*.
Measured across one 2026 match:

| Entry | Samples | Median gap |
|---|---:|---:|
| `/Drivetrain/BL/SteerTemp` | 48 | 0.22 s |
| `/Drivetrain/BR/SteerTemp` | 10 | **32.57 s** |
| `/Drivetrain/FR/DriveTemp` | 125 | 1.25 s |

Same quantity, same match, two orders of magnitude apart. "Value holds until the
next sample" therefore gives an accurate duration, but the *crossing instant* is
only known to within one gap. Report durations at the resolution the data
supports rather than to six decimals, which would imply precision that is not
there.

**2. Hysteresis, not a bare threshold.** A value resting on the limit produces a
burst of one-sample intervals. `clearBelow` (enter above 60, clear below 55)
collapses those into one excursion and matches how thermal limits are actually
specified; `minDuration` additionally drops blips. Without one or the other the
report is noise exactly when the reading is most marginal.

**3. Unresolved intervals at end of log.** If the log ends while the value is
still past the limit, the interval runs to the last timestamp and must be marked
as such. "Still elevated when the log ended" is a different and more alarming
finding than a closed excursion, and in pit mode it is the one that matters —
the next match starts from there.

**4. The gate has to clip, not filter.** `"while": "enabled"` currently drops
samples. For durations it must clip intervals to the enabled windows instead, or
one excursion spanning a brief disable reads as two.

### 8.3 Built

`{"above": V}` / `{"below": V}` with `clearBelow` / `clearAbove` for hysteresis,
`minDuration`, and `unit` for the detail line. One finding per matched entry:

```
[WARNING] Drive motor over temperature (/Drivetrain/BR/DriveTemp)
  above 40 C for 162.8 s, peak 49 C at 430.4 s
```

All four cautions above are implemented, and two of them changed the code rather
than just the wording:

- **The gate clips, not filters.** `EnabledGate.windows()` returns the spans a
  gate admits; spells are found over every sample and their duration intersected
  with those spans. Filtering samples first would split one spell that straddles
  a brief disable into two. A test asserts the count stays 1 while the duration
  drops from 90 s to 70 s.
- **A spell that opens on the final sample has zero duration** and was being
  filtered out — silently losing the "robot finished past the limit" case, which
  in the pit is the one that matters, since the next match starts from there.
  Zero-duration spells are now kept; only a spell the gate admits *none* of is
  dropped, and `minDuration` filters solely when it was asked for.

Still to do: `minDuration` is compared against the gated duration, which is
right, but there is no way yet to require a *peak* margin as well as a duration —
"above 60 for 30 s" and "above 75 at all" need two rules.

### 8.4 Why it belongs with checks

Mechanically this is the time analysis's windowing — find a start condition, find
its end, measure the gap — and that interval-finding logic in
`analyze_file_records` is worth extracting rather than writing twice.

But the output is a finding, not a distribution. Nobody wants the mean of
temperature excursions; they want to know whether any happened and how bad. Using
`compute_checks`'s finding shape keeps it in the pit report beside the alerts and
dropouts, where it will actually be read.

## 9. Comparative Checks

Feature 8. "Is this motor running hotter than usual?" — the question that turns a
report from *what went wrong* into *what is about to*.

There are two ways to answer it, and measurement says the cheaper one is also the
better one.

### 9.1 Sibling comparison beats historical, and needs no history

Peak drive-motor temperature, all nine 2026 matches:

| Entry | q12 | q22 | q46 | q59 | q67 | q80 | q89 | q105 | q121 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| `/Drivetrain/FL/DriveTemp` | 46 | 42 | 46 | 45 | 48 | 44 | 47 | 44 | 44 |
| `/Drivetrain/FR/DriveTemp` | 45 | 42 | 48 | 46 | 48 | 45 | 49 | 45 | 47 |
| `/Drivetrain/BL/DriveTemp` | 45 | 42 | 46 | 45 | 47 | 42 | 47 | 44 | 44 |
| `/Drivetrain/BR/DriveTemp` | 45 | 44 | 47 | 46 | 49 | 42 | 48 | 45 | 45 |

- Spread **across the four motors within one match**: 1–3 °C.
- Spread **across matches for one motor**: 5–7 °C.

Sibling comparison is **2.3x tighter**. The reason is visible in the table: match
variation is *correlated across motors* — q22 is cool everywhere (42/42/42/44),
q67 hot everywhere (48/48/47/49). Ambient temperature, match intensity and how
long the robot was enabled move every motor together. Comparing a motor to its own
history mixes that confounder in; comparing it to its siblings cancels it.

Sibling comparison also **works from the first match of an event**, needs no
index, and A3's wildcards already produce the sibling set from
`/Drivetrain/*/DriveTemp`. It should be built first.

### 9.2 What historical comparison adds

Sibling comparison is blind to anything that degrades every sibling equally — a
battery losing capacity, a drivetrain-wide gearbox issue, a hotter venue. Only
history catches those, so it is worth having, with two cautions.

**Small n.** A qualification schedule is 8–12 matches. Two-sigma outlier detection
is unreliable there: measured while building A2's tests, five points with one
extreme inflate the standard deviation past the extreme's own deviation, so the
outlier hides itself. Prefer robust statistics (median and MAD), or better, just
**show the comparison** — "peak 71 °C, previous matches 42–49 °C" is more use to
someone in a pit than a binary verdict.

**Baseline scope.** Restrict it to the same event by default; ambient conditions
differ between venues. The event name is already in the log filename
(`akit_26-05-01_22-45-20_johnson_q121`).

### 9.3 A trend is not an outlier

"Hotter than usual" is two different findings:

- a **step change** — something broke between matches;
- a **monotonic trend** — something is wearing out.

The second is the actual pre-failure signal, and an outlier test misses it
entirely, because each match is only slightly worse than the last and never
deviates enough on its own. That needs a separate rule kind: is the slope over the
last *k* matches positive and beyond a threshold?

### 9.4 Normalise before comparing

Measured, the same motors by *rise* above their starting temperature: 19–30 °C,
against peaks of 42–49 °C. Similar relative spread, but rise removes the
starting-temperature component — which matters directly for back-to-back matches,
where the robot starts warm. Prefer rise over absolute peak, and consider
normalising by enabled time so a match cut short does not read as an improvement.

### 9.5 Shape

```json
{
    "name": "Drive motor hotter than its peers",
    "entry": "/Drivetrain/*/DriveTemp",
    "compare": "siblings",
    "statistic": "rise",
    "deviation": {"aboveSiblingsBy": 8},
    "severity": "warning"
}
```

```json
{
    "name": "Drive motor hotter than usual",
    "entry": "/Drivetrain/*/DriveTemp",
    "compare": "history",
    "scope": "event",
    "statistic": "rise",
    "deviation": {"aboveBaselineBy": 10},
    "minimumBaseline": 3,
    "severity": "warning"
}
```

`statistic` reduces a match to one number per entry — `peak`, `rise`, `mean`,
`timeAbove` (§8), or a finding count. That per-match summary is exactly what B1's
numeric manifest already proposes to store, so the two features share a data
structure.

### 9.6 Motors with no sibling

Sibling comparison covers the drive, steer and flywheel motors. The spindexer,
kicker, turret, hood, deployer and climber are singletons, and **no cheap
substitute was found**. Two candidates were measured over the nine 2026 matches,
using coefficient of variation of the per-match temperature rise:

| Metric | Spindexer | Kicker |
|---|---:|---:|
| Raw rise | 0.164 | 0.221 |
| Normalised by drivetrain mean rise | 0.161 | **0.307** |
| Normalised by work done (°C per kA·s of stator current) | **0.150** | 0.215 |
| *(drive motors, for reference)* | *0.089* | |

**Normalising against the drivetrain fails**, and makes the kicker worse. The
drivetrain is not a proxy for hopper duty: q22 had the coolest drivetrain (20.2 °C
rise) and the hottest kicker (25 °C) — heavy shooting, light driving. For a
singleton the confounder is not ambient, it is **workload**, and workload differs
per mechanism.

**Normalising by work barely tightens the distribution either**, which is the
honest result. Temperature is logged at 1 °C resolution against rises of only
12–25 °C, thermal lag means peak rise depends on *when* the work happened rather
than only how much, and nine matches is a thin basis for a variance estimate.

But it does change **which** match looks anomalous, and that is worth having:

| Match | Spindexer rise | Work (A·s) | °C per kA·s |
|---|---:|---:|---:|
| q105 | **20.0** (highest) | 3017 (highest) | 6.63 (unremarkable) |
| q67 | 15.0 | 1483 (lowest) | **10.12** (highest) |

Raw rise flags q105, which simply did the most work — a false positive.
Work-normalised flags q67, which independently contains the brownout that
disabled every motor bridge. Normalising did not reduce the noise, but it made the
metric *mean* something: heat for the same work is degradation, heat from more
work is not.

**Practical consequence — say the sensitivity out loud.** With CV around 0.16–0.22
and mean rises of 14–17 °C, one standard deviation is 2.4–3.7 °C, so a
single-match threshold has to sit near 7–11 °C to avoid false positives. Sibling
comparison resolves roughly 4 °C. **Singletons are 2–3x less sensitive**, and a
rule that pretends otherwise will cry wolf every event.

So for singletons, in order of dependability:

1. **Absolute thresholds (§8).** For a motor with no peer, "above 70 °C for more
   than 30 s" is more dependable than any comparison, needs no baseline, and the
   manufacturer publishes the limit.
2. **Trend across matches (§9.3).** A trend is detectable where a single-match
   outlier is not, because it is fitted across *k* points instead of testing one.
   This is the strongest argument for the trend rule, and it applies specifically
   to the motors sibling comparison cannot reach.
3. **Trend in work-normalised units.** Combining the two: a rising °C-per-kA·s
   means the motor is getting hotter *for the same work*, which is what a failing
   bearing or a shorted winding looks like. A rising raw temperature may only mean
   the mechanism is being used more.

### 9.7 Cold start must be visible

The first match of an event has no baseline, and neither does a newly added entry.
That must report "no baseline yet" rather than nothing — a silent pass is
indistinguishable from a clean result, the same trap as §7.1. `minimumBaseline`
guards it.

## 10. Sequence

### Milestone A — Pit mode

The post-match checklist, end to end. Nothing here needs the index.

| # | Step | Why | Depends on |
|---|---|---|---|
| A0 | ~~**Refresh fixtures to 2026 logs**~~ (§11) — **done** | Detector rules should be written against current entry names, not 2025 ones | — |
| A1 | ~~**Fix array support**~~ (defects #1–2) — **done** | Unlocks `Alerts/*` — most of feature 2's value | — |
| A2 | ~~**Split computation from formatting**~~ — **done** | Prerequisite for HTML and JSON output | — |
| A3 | ~~**Entry patterns / wildcards**~~ (§6) — **done** | Detector rules are unwritable without it; shortens every config | A1 |
| A4 | ~~**Checks as a third analysis kind**~~, incl. absence checks (§7) — **done** | Replaces the manual post-match pass | A2, A3 |
| A5 | ~~**roboRIO sync**~~ — **done** (`sync_logs.py`) | First link in the pit chain (§2.2) | — |
| A6 | ~~**HTML + JSON emitters**~~ — **done** (`report_output.py`) | The pit screen itself (§5.1) | A2, A4 |
| A7 | ~~**Watch mode**~~ — **done** (`pit_monitor.py`) | Closes the pit chain: no commands typed between matches | A5, A6 |
| A8 | ~~**Threshold / duration checks**~~ (§8) — **done** | Motor temperature exposure; the one concern class checks cannot yet express | A4 |
| A9 | **Sibling comparison** (§9.1) | "hotter than its peers" — 2.3x tighter than history and needs none | A3, A8 |
| A10 | **Absolute thresholds for singletons** (§9.6) | The dependable option for motors with no peer; §8 already provides the mechanism | A8 |
| A11 | ~~**`--matches-only`**~~ (§5) — **done** | A synced folder holds pit logs; counting them as matches skews every per-file average | — |

### Milestone B — Library / practice mode

| # | Step | Why | Depends on |
|---|---|---|---|
| B1 | **Extract / index layer** (SQLite) | Makes search viable, checks instant across a season | A2 |
| B2 | **Event search** (feature 3) | The reason the index exists | B1 |
| B3 | **Historical comparison and trends** (§9.2–9.3, §9.6) | Catches what sibling comparison cannot: wear affecting every peer equally, and slow trends | B1, A9 |

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
  `/RealOutputs//ShooterModes/DistanceToHub` match (§11.1); only the leading
  slash's empty segment is dropped.
- **Expansion order must be sorted, not first-seen.** Expanding per file means the
  first log decides the order, and the first 2026 log lacks BCL — which put the
  cameras in BCH, BL, BCL, BR order. The aggregate now sorts by key.

Not yet done from §6: the dry-run entry lister (§6.6), and `"combine": true`
pooling (§6.3) — fan-out is per-match only, which is the useful default.

**A4 — checks.** Done. A `"checks"` array alongside `timeAnalysis` /
`valueAnalysis`, producing findings rather than statistics. Rule shape:

```json
{"name": "...", "entry": "<pattern>", "expect": <expectation>,
 "while": "enabled" | "disabled" | "any", "severity": "error" | "warning" | "info"}
```

Expectations, covering both halves of §7:

| `expect` | Fires when |
|---|---|
| `"present"` | the entry never appears in the log |
| `"empty"` | an array entry is non-empty — one finding per element, which is how `Alerts/*` becomes individual concerns |
| `{"always": V}` | any sample differs from V |
| `{"never": V}` | any sample equals V |
| `{"atLeastOnce": V}` | no sample ever equals V |

Gates, via `"while"`:

| Gate | Samples considered |
|---|---|
| `"any"` (default) | all of them |
| `"enabled"` | only while the robot is enabled |
| `"disabled"` | only while it is not |
| `"afterFirstEnable"` | everything from the first enable onward |

**`afterFirstEnable` is usually the right one for a post-match report, and the
obvious choice — `enabled` — is a trap.** Measured over the nine 2026 logs, the
alert stream holds 2,835 occurrences of 46 distinct messages:

| Gate | Occurrences kept | Distinct messages kept |
|---|---:|---:|
| `any` | 2,835 | 46 |
| `afterFirstEnable` | 2,697 | **42** |
| `enabled` | 1,041 | **25** |

`afterFirstEnable` drops exactly the four boot-only messages that recur every
match and bury everything else — the JIT warning, the operator-controller
warnings, and two cameras disconnecting from NT while threads are still starved
during boot.

`enabled` drops a further **17** messages, and they are the ones that matter:
every `[STICKY] Bridge was disabled` across the drivetrain, hopper, intake and
shooter, the Pigeon's saturated accelerometer, and
`Intake: [Roller Motor]: [STICKY] Device booted while enabled`. Sticky faults are
published after the fact, so a fault caused during a match commonly appears only
once the robot has been disabled again. Twelve of those messages never appear
during an enabled window at all.

One consequence to design around: `afterFirstEnable` silences every gated rule in
a log where the robot never enabled. That must not read as a clean report, so
`checks2026.json` carries a `Robot never enabled` rule
(`{"atLeastOnce": true}` on `/DriverStation/Enabled`) that is deliberately
ungated.

Two modifiers:

- **`expectEntries`** declares the set a wildcard rule expects to find, named by
  what the wildcard should have matched (`["BCH", "BCL", "BL", "BR"]`). Without it
  a pattern can only expand over entries that *exist*, so a camera that never
  reported produces no rule and no finding — §7.3's invisible silence. With it,
  one rule covers both halves: a listed entry that never arrived is reported as
  missing, and a present one that misbehaves is reported as a deviation, at the
  same severity. This collapsed `checks2026.json` from ten rules to six.
- **`excludeEntry`** removes matches from a broad pattern. Needed because
  `/**/*Connected*` otherwise reaches `/SystemStats/NTClients/**`, whose paths
  carry a per-connection session id (`northstar_BCL@5`) and so are unbounded in
  number and useless as a per-device signal.

Findings collapse on `(rule, entry, detail)` with an occurrence count and the
first timestamp, so an alert logged every cycle reads as one line. `merge_check_findings`
does the same across files, adding a file count. Output sorts most severe first.

`checks2026.json` at the repo root is a ready-to-run pit config — no config
writing needed between matches. Checks were **not** made to run automatically on
every invocation: that would have added a section to every existing report. Once
A7 (watch mode) exists it can default to this file.

Not yet done: per-rule message templates. The detail line is generated
(`is False, expected True`), which reads well enough that templates can wait.

An unbounded-cardinality subtree is a general hazard for wildcard rules, not a
one-off: any path that embeds a session id, a connection index or a timestamp
grows without limit, and a broad pattern will happily match all of it.
`excludeEntry` is the escape hatch; §6.6's dry-run entry lister would let you see
the problem before writing the rule.

**A5 — roboRIO sync.** A **standalone program**, sharing nothing with the analyzer
but an output folder. It has different failure modes (network, not parsing), a
different lifetime (it wants to poll for a robot that is usually absent), and
different dependencies. A7 joins the two: sync drops files in a folder, watch
notices them.

That composition imposes one hard requirement: **downloads must be atomic.**
Fetch to a temporary name in the destination folder and `rename` into place only
after the size is verified. A partially-written file bearing its final name would
be analyzed mid-copy by A7 and, worse, would look complete on the next sync.

*Network.* The robot is at `10.TE.AM.2` — `10.30.61.2` for team 3061. The pit
laptop needs a **static address on the robot interface**, because the radio's DHCP
server comes and goes with robot power. Pick one outside the radio's DHCP pool and
clear of `.1` (radio) and `.2` (roboRIO) — `10.30.61.5/24` is a safe choice —
and set **no gateway on that interface**, so the wired hotspot on the other
interface keeps providing internet. Prefer the literal IP over
`roborio-3061-frc.local`: mDNS is the thing most likely to be flaky in a pit, and
the address is fixed anyway. Keep the host configurable regardless.

*Where the logs are.* Search a configurable candidate list, defaulting to
`/media/sda1`, `/media/sda2`, `/U`, `/home/lvuser/logs`. AdvantageKit commonly
writes to USB rather than internal storage, and which mount point appears depends
on the stick.

*The robot is always logging.* A powered roboRIO logs continuously, in the pit as
well as on the field. So when it comes back and is switched on it immediately
opens a **new** log, and the newest file on the robot is the one currently being
appended to — not the match that needs checking. Downloading it yields a
truncated snapshot which, worse, then matches on size and looks complete.

The fix does not assume which file that is. Sync takes **two listings a couple of
seconds apart** and skips anything whose size changed, plus anything that appeared
between them. That stays correct in the case the naive rule ("skip the newest")
gets wrong: if the robot is *not* powered in the pit, the match log is the newest
file and is finished, and must be downloaded.

The atomic size verification is a second, independent line of defence — a log
that grows between the listing and the copy fails verification and never lands.
It is not sufficient on its own, though: a log appended to just before the
listing and not during the copy window would match on size, which is why the
settle check exists.

*What is new.* Determine it from the **destination folder, not a state file**: a
remote file whose name and size already match a local file is skipped. Names are
timestamped and unique, so name+size is sufficient, and combined with atomic
download it means a partial transfer never has the final name and is simply
retried next run. No state to corrupt, survives a laptop reimage, and a restart
mid-event neither redoes the day nor skips a match — the same restartability A7
needs. Content hashing is B1's concern, where re-imports must be idempotent
across renames; here it would mean hashing every remote file over SSH for no gain.

*Transport.* Shell out to `ssh` / `scp` rather than adding `paramiko`; the repo
has one dependency and this does not need to be the second. The roboRIO image is
minimal, so do not assume `rsync` is present remotely. If `scp` proves unreliable
against the robot's SSH server, `ssh <host> 'cat <path>'` needs only `cat` and is
the bulletproof fallback.

*Testability.* There is no roboRIO in this repo and there will not be one in CI,
so **the decision logic must be separable from the transport**: a pure function
taking a remote listing and a local listing and returning what to fetch, unit
tested; a thin transport behind it. Add a `--dry-run` that prints what it would
copy, and allow a local directory as the "remote" so the whole flow can be
exercised without a robot.

*Safety.* Copy, never move — never delete from the robot. Verify size before the
rename. Degrade quietly when the robot is unreachable (that is the normal case,
not an error), so a polling loop does not fill the terminal with failures.

*Windows.* The pit laptop runs Windows, which changes three things and breaks one
outright. `UserKnownHostsFile=/dev/null` is rejected by Win32-OpenSSH — it wants
`NUL` — so the null device is selected per platform. The remote listing script is
kept to a **single line**, because Windows flattens the argument list into one
command string before `CreateProcess` sees it and embedded newlines are a good way
to have a remote script arrive mangled; a test round-trips the script through
`subprocess.list2cmdline` and a reference implementation of `CommandLineToArgvW`
to prove it survives. And `sshpass` has no Windows build, so key-based auth is the
only non-interactive option there — `ssh-copy-id` does not exist on Windows
either, so the key has to be appended manually the first time. `ssh`/`scp` come
from the OpenSSH Client optional feature; the tool checks for them up front and
says so rather than failing obscurely.

**Built as `sync_logs.py`**, all of the above implemented. One thing could not be
verified from here and needs a real robot: **non-interactive auth**. `ssh` reads a
password from the tty rather than stdin, so an empty password cannot be supplied
on its own — `BatchMode=yes` makes it fail fast instead of hanging. If plain
`scp` is refused, the fix is `ssh-copy-id admin@10.30.61.2` once, or `--sshpass`;
the tool detects the auth failure and prints both. Everything else is exercised
by `tests/test_sync_logs.py` through a local directory standing in for the robot.

**A6 — emitters.** Done, in `report_output.py`. `analysis.py --html PATH
--json PATH` writes both alongside the usual terminal output; neither changes
stdout, which the goldens enforce. `main()` now computes each result once and
renders it, rather than recomputing inside the print helpers — the payoff of A2.

The page is self-contained by necessity, not preference: browsers block
fetch/XHR against `file://`, so a page loading a sibling `report.json` would fail
silently on exactly the setup this is for. Data is inlined, there is no
JavaScript and nothing is fetched, and a `<meta http-equiv="refresh">` means
rewriting the file in place updates a screen showing it. Tests assert the absence
of `http://`, `<script`, `src=` and `fetch(` rather than trusting that to stay
true.

`analysis.py` moved to argparse in the process, which also turned the `VERBOSE`
module constant into `--verbose`. The only visible change is that bad arguments
now produce argparse's own message.

`--latest` narrows a folder to the most recent **finished** match, which is what
makes the page usable as a standing pit display: left open, the meta refresh means
it always shows the newest completed match as logs arrive.

"Finished" matters for the same reason it does in A5: the robot opens a new log
the moment it is powered in the pit, so the newest file may be growing. Files
produced by `sync_logs.py` are complete by construction (it renames into place),
so the check costs nothing there — but `--latest` may also be pointed at a USB
stick or a mount of the robot, where it would otherwise pick the open log.
`--settle-seconds` (default 1) watches the candidates for growth and falls back to
the next-newest; `--settle-seconds 0` skips it. If every log is still being
written, that is reported rather than silently analysing a partial file.

When it does fall back, the skipped log is **named, not classified** — on stdout,
in the JSON, and as a notice band on the page. The tool has no sound way to tell a
match still being logged (robot brought back from the field powered, after a
failure) from an idle pit session, and the two look identical as files. The name
settles it for a human: an AdvantageKit name carries the event and match number
when the robot was FMS-connected, so `..._johnson_q121.wpilog` reads as a match
and `..._johnson.wpilog` as a pit session.

**A log file is not a match**, and the filename cannot be trusted to say which
is which. Four cases, all real:

| Name | Meaning |
|---|---|
| `akit_26-04-29_21-55-44.wpilog` | not a match — timestamp only |
| `akit_26-04-30_16-35-17_johnson.wpilog` | match whose **match number** was not applied |
| `akit_26-05-01_20-28-19_q105.wpilog` | match whose **event name** was not applied |
| `akit_26-04-30_19-14-09_johnson_q36.wpilog` + `..._19-18-09_johnson_q36.wpilog` | **one match, two logs** — the robot rebooted during or before it |

So `(event, match number)` is neither unique nor reliably present, and must never
be used as an identity. Content hash, as B1 already plans, is the identity. The
log's *timestamp prefix* is always present, which is what `--latest` orders on and
why it survives every variant above.

The sound signals are inside the log, not in its name:

- **`/DriverStation/FMSAttached` ever true** ⇒ the log contains match activity.
  True for all nine 2026 logs; a pit session would be false.
- **Total enabled time** ⇒ whether the match is *complete*. Measured, eight of the
  nine cluster at **160.4–162.4 s** (15 s auto + 135 s teleop plus transitions),
  a ±1 s band. A reboot splits a match into two logs that each fall well short of
  it.

The exception is instructive: `q105` — the log that also lost its event name —
records **254 s** enabled, 93 s beyond a match. Anomalous in both name and
duration, so "short" is the partial-match test, not "different".

**A synced folder contains pit logs too**, and analysing them as matches corrupts
per-file statistics. Registering one real pit log
(`akit_26-04-29_19-26-56.wpilog`) alongside the nine matches moved the aggregates
measurably:

| | 9 matches | + 1 pit log |
|---|---:|---:|
| BCH camera losses, average per file | 3.00 | **2.70** |
| BR camera losses, average per file | 3.44 | **3.10** |

Totals are unaffected; it is the per-file averages that go wrong, dividing match
events across a file that was never a match. So a **`--matches-only` filter** is
needed, and `/DriverStation/FMSAttached` is what it must rest on. Measured on that
log against a full match:

| Signal | Pit log | Full match | Separates? |
|---|---|---|---|
| `FMSAttached` ever true | **false** | true | **yes** |
| `Enabled` ever true | true | true | no |
| Enable periods | 8 | 2 | indicative only |
| Total enabled | **289.3 s** | 161 s | **no** — pit exceeds a match |

The last row matters: an enabled-time threshold fails in *both* directions here,
so "was the robot enabled" and "was it enabled long enough" are both unsound.
`FMSAttached` is the only clean test, and it is cheap for a match (the flag turns
true early, so the scan can stop) while a pit log needs a full read to prove the
negative.

**Built as `--matches-only`.** Measured, the asymmetry is stark: detecting a match
costs **0.00–0.12 s** because FMS attaches about 29 s into a log, while the pit
log takes 3.43 s to prove negative — and that is a file the run would otherwise
have spent longer analysing, so the filter pays for itself. It restores the
diluted averages exactly (BCH 2.70 -> 3.00, BR 3.10 -> 3.44), leaves totals
untouched, and names what it skipped on stdout, in the JSON and as a notice band
on the page. It is opt-in, so existing behaviour and every golden are unchanged.

**This matters for §9.** A rebooted match yields two partial logs whose per-match
statistics are not comparable to a full match: half the enabled time means far
less temperature rise, and fewer of everything. Feeding them into a baseline
silently drags it down and makes a genuinely hot match look normal. Comparative
checks must either merge the pair or exclude logs whose enabled time falls short
of a full match — and say which they did.

Two discriminators were considered and rejected as the *basis* for a claim.
Asserting "a newer match is still being logged" would be wrong in the common case,
where the growing log is just the idle robot in the pit. Inferring it from the
match identifier in the filename was worse than thin: the single "negative
example" it rested on, `akit_26-04-30_16-35-17_johnson.wpilog`, is most likely a
*match* log that lost its match number, not a pit log at all — so the evidence was
not merely sparse but probably invalid. The only sound test is `/DriverStation/FMSAttached`
inside the log, which would mean reading a file that is deliberately not
downloaded — worth doing only if the classification ever has to be automatic.

"Most recent" comes from the timestamp embedded in the log name
(`akit_26-05-01_22-45-20_...`, or DataLogManager's `FRC_20260501_133809`), with
mtime only as a fallback. That ordering matters: plain `scp` resets mtime to copy
time, so a re-copied old log would otherwise masquerade as the newest and the pit
screen would quietly show the wrong match. The page header names the match it is
displaying for the same reason.

**A7 — watch mode.** Built as **`pit_monitor.py`**, a separate driver rather than
a flag on the analyser: it composes the two standalone tools instead of absorbing
them.

    ./pit_monitor.py ./logs checks2026.json --sync-from 10.30.61.2

Each cycle it optionally syncs, works out which log *would* be analysed, and
re-runs the analyser only if that changed. With the report's meta refresh, a
browser left open tracks the newest match with nothing typed between matches.

It came out smaller than planned because later work absorbed most of it. The
debounce is the size-settling check already built for A5 and `--latest`. Pit
sessions are excluded by `--matches-only`. And the content-hash bookkeeping this
note originally called for was **not** needed: with `--latest` the question is not
"which files have I processed" but "which log is currently newest", so
remembering that one path is enough, and re-running is idempotent by
construction. A restart re-renders the current match once, which is correct.

Deciding the target is deliberately cheap. Candidates go newest-first and the
search stops at the first settled match, so a normal cycle classifies one file in
about 10 ms; the costly negative — proving a pit session is not a match — happens
once per session and is cached against the file's size.

*Nothing in a cycle may kill the loop.* An absent robot, an unreachable host, a
bad config, a failed analysis: each is reported and survived. A pit display that
dies at the wrong moment is worse than a stale one.

One thing this exposed: `sync_logs.py` exits 0 when the robot is away, by design,
so a boolean "did sync succeed" reported **"synced"** when nothing had been
fetched. Sync now exports an `UNREACHABLE_NOTICE` sentinel and the monitor
reports three states — synced, robot not reachable, sync FAILED — rather than
two.

**B1 — index.** Store the state tier in full plus a numeric manifest. Key on file
identity (content hash, not name) so re-imports from A4 are idempotent. Keep the
schema versioned so a format change can rebuild rather than migrate — extraction
is cheap and the `.wpilog` files remain authoritative.

## 11. Test Fixtures and Logging Conventions

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

### 11.1 What the 2026 changeover cost

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
config linter (§12) would have caught before a 30-second run:

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

## 12. Cross-Cutting Work

Fix these along the way — each will otherwise distort a feature above:

- **Entry matching is reversed substring** (`entry.name in configured_name`, see
  [DESIGN.md §3.4](DESIGN.md#34-ingest-and-filtering-analysispy-process_log_file)).
  Superseded by A3; §6.4 covers how to keep the struct-leaf case working without it.
- **Analyses are keyed by list position** (`analysis_idx`). Reports and JSON output
  need stable named ids — and wildcard fan-out (§6.3) makes this a prerequisite of
  A3, not a nicety.
- **`.schema` handling assumes `struct:`** (defect #4) and will `IndexError` on a
  protobuf schema entry.
- **No config validation.** Every failure mode in §11.1 produced a clean run with
  empty results rather than an error, which is the worst possible feedback. A
  linter — check each referenced entry exists in the target logs, that the
  configured value's type matches the entry's, and that the entry is actually
  recorded under the configured `robotMode` — would catch all three before a
  multi-minute run. Cheap once B1's manifest exists; useful enough to do sooner.

## 13. What Must Not Regress

The folder-wide aggregate analysis is the feature that replaced hours of
one-file-at-a-time work in AdvantageScope. It is the thing to protect through
every step above, and it is the whole of practice-review mode.

[tests/](../tests/) pins its exact output byte-for-byte, including the sample
output the top-level README documents. Any step here that changes printed output
should appear as a reviewed diff in `tests/golden/`, never as a silent change —
see [tests/README.md](../tests/README.md).
