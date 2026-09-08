# log-analyzer — Design Notes

A working understanding of the repository, first written against commit `230d144`
before exploring new features. Describes what the code *does*, not what it should
do. Defects listed in §8 are marked as they are fixed. For where the tool goes
next, see [ROADMAP.md](ROADMAP.md).

## 1. Purpose

A batch offline analyzer for WPILib **DataLog** (`.wpilog`) files produced by FRC
robots (the sample config targets AdvantageKit-style `/RealOutputs/...` keys). It
answers two questions across a folder of match logs:

- **Time analysis** — how long does it take to get from event A to event B? (cycle times)
- **Value analysis** — what was the value of field X at the moment condition Y became true?

Results are printed per-file and then aggregated across all files. Everything is
driven by a JSON config; there is no CLI beyond `analysis.py <log_folder> <config_json_file>`.

## 2. Module Map

| File | Role | Provenance |
|---|---|---|
| [datalog.py](../datalog.py) | Byte-level `.wpilog` record reader | Vendored from WPILib (`allwpilib`), BSD |
| [StructDecoder.py](../StructDecoder.py) | WPILib struct schema parser + decoder | Port of AdvantageScope's `StructDecoder.ts`, Littleton Robotics BSD |
| [Log.py](../Log.py) | In-memory field store (timestamps → values) | Simplified port of AdvantageScope's `shared/log` |
| [analysis.py](../analysis.py) | Entry point: config, filtering, analysis, reporting | Original to this repo |
| [config2025.json](../config2025.json), [config2026.json](../config2026.json) | Per-season example/default analysis configs | — |
| [tests/](../tests/) | Season-parameterized golden-output suite (stdlib `unittest`) | — |
| `test/<season>/` | `.wpilog` fixtures per season; gitignored (~108 MB for 2025, ~433 MB for 2026) | — |
| [test.log](../test.log) | Stray plain-text sample; **not** a `.wpilog` and unused by any code | — |

The dependency direction is strictly `analysis.py → Log.py → StructDecoder.py`
and `analysis.py → datalog.py`. There are no cycles and no package structure —
flat top-level modules imported by name.

Only external dependency: `msgpack`. Everything else is stdlib. The checked-in
`.venv/` is Python 3.9, but `datalog.py` uses `tuple[str, int]` in an annotation
(PEP 585), which needs 3.9+ at runtime only because it is inside a function
signature evaluated at def-time — it works on 3.9, but the README's claim of
"Python 3.6+" is wrong.

## 3. Layered Architecture

```
.wpilog bytes
    │
    │  mmap (read-only, whole file)
    ▼
DataLogReader ──► DataLogIterator ──► DataLogRecord      [datalog.py]
    │                                   (entry id, timestamp µs, payload)
    │
    │  process_log_file(): control-record bookkeeping,
    │  DriverStation state tracking, filtering, type dispatch
    ▼
Log ──► LogField ──► LogValueSet(timestamps[], values[])  [Log.py]
    │       └─ struct/JSON/msgpack payloads are *flattened* into
    │          synthetic child fields ("parent/child/leaf")
    ▼
analyze_file_records() / analyze_value_records()          [analysis.py]
    │  produce {analysis_idx: (file_name, data[], timestamps[])}
    ▼
print_results_and_calculations()  ── stdout report
```

### 3.1 Record layer (`datalog.py`)

Unmodified upstream WPILib reader. Records are either **control records**
(`entry == 0`: start / finish / setMetadata) or **data records**. Start records
carry the `(entry id, name, type string, metadata)` binding; data records carry
only the entry id, so the reader must maintain the id→name map itself.

`DataLogRecord` exposes typed accessors (`getBoolean`, `getDouble`,
`getStringArray`, `getMsgPack`, …) that validate payload length and raise
`TypeError` on mismatch. `getBytes()` was added locally (commit `fdbdd8d`) to
support struct schema extraction.

The iterator does variable-width header decoding and stops silently on a
truncated tail — a partially-written log degrades to "fewer records", never an
error.

### 3.2 Field store (`Log.py`)

`Log` is a `Dict[str, LogField]`. A `LogField` has one `LoggableType`
(RAW / BOOLEAN / NUMBER / STRING / *_ARRAY / EMPTY) fixed at creation. Writing a
mismatched type does **not** raise — it sets `type_warning = True` and drops the
value. Nothing in `analysis.py` ever reads `type_warning`, so type conflicts are
silently lossy.

Key design property: **structured payloads are flattened, not nested.** When
`put_struct` / `put_json` / `put_msgpack` decodes a payload, `_put_unknown_struct`
recursively writes each leaf to its own field keyed by path
(`/RealOutputs/DriveToReef/difference (reef frame)/translation/y`). The parent key
is registered in `generated_parents`, and `is_generated()` marks descendants so
they can be excluded from field counts. This is what lets a config address a
sub-field of a struct by a slash path as if it were a top-level entry.

`Log` owns the single `StructDecoder` instance, so schemas learned from the log
are available to every subsequent struct record in that same file. Decoders are
per-`Log`, i.e. **per file** — schemas do not carry across files.

### 3.3 Struct decoding (`StructDecoder.py`)

Two-phase: `add_schema(name, bytes)` stores the schema text, then repeatedly
attempts to compile every uncompiled schema until a full pass compiles nothing
new. This resolves inter-schema dependencies in any arrival order (a struct
referencing another struct fails to compile until its dependency lands).

`_compile_schema` parses the WPILib struct IDL — declarations separated by `;`,
each `[enum {A=0,B=1}] type name[:bits | [len]]` — and assigns each field a
`bit_range`, handling bitfield packing rules (bools pack at 1 bit; same-type
integers share a bitfield word; a type change or overflow starts a new word).
Decoding slices bits, pads to the type width, and unpacks little-endian.

`decode()` returns `{"data": ..., "schema_types": {...}}` — the second map lets
`Log.put_struct` pre-create empty child fields for nested struct types even when
they carry no scalar data.

### 3.4 Ingest and filtering (`analysis.py: process_log_file`)

The single pass over records does five things in order:

1. **Control records** maintain the `entries` id→`StartRecordData` map.
2. **DriverStation tracking** — `/DriverStation/Enabled`, `/Autonomous`,
   `/FMSAttached` update three "most recent value" variables. These are *always*
   updated, even for filtered-out records.
3. **Schema capture** — any entry whose name contains `.schema` is fed to the
   struct decoder as `name.split("struct:")[1]`.
4. **Selection** — a record is kept if its name matches a *mandatory* entry, or
   matches a *target* entry **and** `should_capture_record()` passes.
5. **Type dispatch** — the entry's type string routes to the matching `Log.put_*`.

**Filtering is a capture-time gate, not a post-hoc query.** Records outside the
requested robot state never enter the `Log` at all, so downstream analysis sees a
log with holes in it rather than a filtered view. The filters are also
*permissive on unknown state*: each check is `if filter_on and state is not None
and state_is_wrong: reject`. Before the first DriverStation record appears, the
state is `None` and everything is captured.

`mandatory_entries` (the three DriverStation keys) is a subset of
`target_entry_names`, and is checked first — so DS state is captured unfiltered
while everything else is gated.

**Entry-name matching is substring, and reversed from the obvious direction:**
`any(entry.name in name for name in target_entry_names)`. The *logged* name must
be a substring of a *configured* name. This is deliberate: the config names a
struct leaf like `.../translation/y`, but the record actually present in the log
is the parent struct `.../difference (reef frame)`. The substring test captures
the parent so struct flattening can synthesize the requested leaf. The cost is
that any logged entry whose name happens to be a prefix/substring of a configured
name is also captured.

Target entries are derived automatically from the analysis configs (commit
`66db43b` removed an explicit `entryNames` key), so the config never has to list
what to capture.

There is **no pattern matching**: every entry must be named in full, so watching
four cameras takes four near-identical config stanzas. Wildcard support
(`/RealOutputs/Vision/*/sending frames`) is designed in
[ROADMAP.md §6](ROADMAP.md#6-entry-patterns-wildcards), which replaces this
substring test rather than layering on it — §6.4 there covers how the struct-leaf
case survives the change.

### 3.5 Analysis semantics

**Time analysis** (`analyze_file_records`) — for each occurrence of `startValue`
on `startEntry`, find the window extending to the *next* occurrence of
`startValue`, then take the **first** `endValue` on `endEntry` inside that window.
The difference is one cycle. A start with no matching end inside its window
contributes nothing. Reported timestamps are **start** timestamps.

**Value analysis** (`analyze_value_records`) — walk `triggerEntry`; each time it
equals `triggerValue`, take the **last** value of `entry` in the window since the
previous trigger. This is "the value as of the trigger", accounting for the two
fields being logged at different rates. Reported timestamps are **trigger**
timestamps.

Both dispatch on `LoggableType` and support only STRING / BOOLEAN / NUMBER; array
and raw fields are skipped with a message. Comparison against the config's value
is plain Python `==`, so JSON `false` matches a logged boolean and JSON strings
match logged enum names directly.

`get_range(start, end)` is half-open at the low end: `start < ts <= end`.

### 3.6 Reporting

Computation and rendering are separate layers:

```
compute_analysis(results, calculations, unit)  -> AnalysisResult
compute_per_file_counts(results, calculations) -> PerFileCounts
        │  dataclasses: every figure plus the (file, timestamp) locations it
        │  came from; no formatting decisions, no output
        ▼
format_analysis(...) / format_per_file_counts(...) -> List[str]
        ▼
print_results_and_calculations(...) / print_per_file_counts(...)
```

`AnalysisResult` and `PerFileCounts` are plain dataclasses, so
`dataclasses.asdict()` serializes them directly — that is the attachment point for
the HTML and JSON emitters in [ROADMAP.md §8](ROADMAP.md#8-sequence).

One computation serves both the per-file case (a one-element `results` list) and
the aggregate case (one element per file); `is_aggregate` drives the wording and
whether `in <file>` is appended to locations. Calculations are a flat if/elif
chain over `calc_type` in `compute_calculation`: `average`, `min`, `max`, `count`,
`abs_average`, `abs_min`, `abs_max`, `outlier_2std`, `abs_outlier_2std`.

Locating *where* an extreme value occurred is done by **value equality search** —
`if result in file_data: idx = file_data.index(result)`. It reports the first
positional match, so duplicate values are attributed to the earliest occurrence,
and the loop does not break, so a value present in several files prints several
locations.

Non-numeric captured values are silently dropped from `numeric_values` before
calculations, which means string-valued analyses print "No numeric values found".

## 4. Config Model

```
{
  "enabled": bool,            # gate on DriverStation enabled
  "fmsAttached": bool,        # gate on FMS attached
  "robotMode": "auto"|"teleop"|"both",
  "timeAnalysis":  [ {startEntry, startValue, endEntry, endValue, calculations[]} ],
  "valueAnalysis": [ {entry, entryUnit, triggerEntry, triggerValue, calculations[]} ]
}
```

Each `calculations` entry is `{"type": <calc>, "name": <display label>}`. Analyses
are identified positionally by list index (`analysis_idx`) throughout the pipeline
— the index is the join key between per-file results and the aggregate maps.

Validation is by truthiness: `if not all([entry, trigger_entry, calculations])`.
Consequence — a legitimate `startValue` of `0`, `false`, or `""` is fine (it isn't
checked), but `valueAnalysis` additionally requires `trigger_value is not None`,
and a `triggerValue` of `false` passes while `null` would skip the analysis.

## 5. Control Flow of `main()`

```
parse argv → load config → derive target_entry_names from analyses
  → list *.wpilog in folder (non-recursive), sorted
  → for each file:
        process_log_file()             # ingest + filter → Log
        analyze_file_records()         # → aggregated_time_analysis_results[idx]
        analyze_value_records()        # → aggregated_value_analysis_results[idx]
        print per-file time section
        print per-file value section
  → print aggregated time section
  → print aggregated value section
  → VERBOSE: captured-records summary
```

Every `Log` is retained in `all_logs` for the whole run — memory is O(all files),
not O(one file), though only the VERBOSE summary uses the retained list.

## 6. Cross-Cutting Characteristics

- **Report-only.** Output is unstructured text on stdout. No JSON/CSV export, no
  exit code signalling analysis outcomes, no plotting.
- **Config-driven, not scriptable.** All extension happens through JSON; there is
  no plugin point for a new calculation short of adding a branch to the if/elif
  chain in `print_results_and_calculations`.
- **`VERBOSE` is a module-level constant** in `analysis.py`, not a flag.
- **Golden-output integration tests, no CI, no packaging.** [tests/](../tests/)
  runs `analysis.py` end-to-end over real logs and diffs full stdout against
  recorded expectations; see [tests/README.md](../tests/README.md). There are no
  unit tests, no `requirements.txt`, `pyproject.toml`, or `.github/`, and
  `msgpack` must still be installed manually.
- **Error handling is print-and-continue.** Invalid log files, missing fields, and
  unsupported types produce a message and skip; only bad argv and an unreadable
  config exit non-zero.
- **Naming is inconsistent** across the boundary between vendored and original
  code: `datalog.py`/`StructDecoder` use `camelCase`/`PascalCase` (upstream
  convention), `Log.py` and `analysis.py` use `snake_case`. Module filenames are
  likewise mixed (`Log.py`, `StructDecoder.py` vs `analysis.py`, `datalog.py`).

## 7. Performance Profile

**Run time is dominated by the ingest scan, not by the analysis.** Both sample
logs together hold ~4.36M records, of which the shipped config captures ~15K
values. Everything downstream of capture is therefore negligible; cost is
proportional to *records read*, not records kept.

Measured on the two 2025 sample logs with `config2025.json` (`cProfile`, same machine):

| | before | after |
|---|---|---|
| Wall clock | 21.2 s | **7.0 s** |
| Function calls | 118.3 M | **26.7 M** |
| Test suite | 84.2 s | **28.5 s** |

Four fixes, all verified output-identical by [tests/](../tests/):

1. **Per-record entry selection was re-derived 4.36M times.** The capture test
   `any(entry.name in name for name in target_entry_names)` ran for every data
   record — 52.3M generator steps, ~6.9 s. It depends only on `entry.name`, which
   is fixed when the entry is declared, so it is now computed once per entry at
   its start record and looked up from `entry_flags`. The `.schema` substring test
   moved with it.
2. **Four control-record predicates per data record.** `isStart` / `isFinish` /
   `isSetMetadata` / `isControl` each re-tested `entry == 0`. The loop now
   branches once on `record.entry == 0`, so the overwhelmingly common data path
   costs one comparison.
3. **`DataLogIterator.__next__` decoded headers a byte at a time.** `_readVarInt`
   was a Python shift-and-or loop called 13.1M times; header fields are now read
   with `int.from_bytes`, and the buffer length is cached instead of re-measured
   three times per record. This is a local deviation from the upstream WPILib
   example, noted in that file's header.
4. **The timestamp division ran for every record**, though only captured records
   use it; it moved inside the capture branch.

`LogField._insert_value` and `get_range` were also genuinely quadratic —
`_insert_value` scanned the timestamp list from the front, which for in-order
records runs its full length every time. Both now use `bisect` (with an append
fast path for the in-order case). This is a **latent** fix, not a current one:
profiling showed `_insert_value` at 0.197 s and `get_range` at 0.002 s, because
filtering keeps per-field lists small. It matters as soon as a config captures
broadly — measured in isolation, in-order ingest of 32K records into one field
went from 14.9 s to 0.016 s. `Log.get_last_timestamp` likewise no longer merges
and sorts every timestamp in the log on each call.

What remains is close to the floor for pure Python: ~5.0 s in `__next__` and
~2.3 s in the `process_log_file` body, both simply the cost of walking 4.36M
records. Further gains would need to avoid decoding records that cannot match —
the entry id is known before the payload is touched — or move the scan out of
Python.

Records are mmapped whole-file, and every captured value is retained in Python
lists, so peak memory scales with total captured records across all files.

## 8. Known Defects Found During Review

These are live bugs in the current code, listed because they shape what any new
feature will run into.

1. ~~**`Log.put_boolean_array` / `put_number_array` / `put_string_array` do not
   exist.**~~ **Fixed.** `LogField` defined them; `Log` did not, so every
   `boolean[]` / `int64[]` / `float[]` / `double[]` / `string[]` entry raised
   `AttributeError` on capture, as did `_put_unknown_struct` on homogeneous
   JSON/msgpack arrays. All three now exist on `Log`; `put_number_array` coerces
   `array.array` to a plain list so values compare equal to a JSON list in a config.
2. ~~**`LogField.get_boolean_array` / `get_number_array` / `get_string_array` do
   not exist either.**~~ **Fixed.** All three now mirror the scalar getters, so
   `Log.get_*_array` works. Both analyzers route type dispatch through one
   `get_field_values()` helper ([analysis.py:144](../analysis.py#L144)) that covers
   the three scalar and three array types; adding a type is a one-line change in
   one place rather than four if/elif chains.
3. ~~**Copy-paste leak in the aggregated value analysis.**~~ **Fixed.** The
   aggregated value block used `all_results_by_file`, `cycle_counts`,
   `min_cycles_per_file` and `max_cycles_per_file` — variables belonging to the
   *time* analysis loop — reporting the wrong filenames, and raising
   `UnboundLocalError` when a `count` calculation was requested on a value
   analysis with no prior time analysis. Both aggregate blocks now delegate to `print_per_file_counts()`
   ([analysis.py:144](../analysis.py#L144)), which derives counts and filenames
   from the results list it is handed.
4. **`.schema` handling assumes `struct:`.** `entry.name.split("struct:")[1]`
   ([analysis.py:407](../analysis.py#L407)) raises `IndexError` on any schema entry
   that isn't a struct schema (e.g. a protobuf descriptor under `/.schema/`).
5. **`LogField.clear_before_time` references `self.striping_reference`**, which is
   never initialized — `AttributeError` if called. Unused today.
6. **The `continue` inside the type dispatch of `analyze_file_records` /
   `analyze_value_records`** (unsupported end/value field type) continues the
   *inner* per-timestamp loop, not the analysis loop, after already overwriting the
   result slot with an empty tuple — so it re-prints the skip message once per
   matching start event.
7. ~~**Blank file name when an analysis was skipped.**~~ **Fixed.** A skipped
   analysis (missing fields, unsupported type) stored `("", [], [])`, so
   `print_per_file_counts` reported `Minimum matched values in any file: 0 in `
   with an empty name. All ten placeholders now carry the real log file name,
   which is known at every one of those points.
8. ~~`README.md` documents "Python 3.6+" and lists `datetime` among used
   modules.~~ **Fixed** — README now requires Python 3.9+, and the example output
   in it was realigned with what the code actually prints.

## 9. Extension Points

Where new features naturally attach:

| Want to add | Touch |
|---|---|
| A new calculation type | if/elif chain in `compute_calculation`, a branch in `format_calculation`, plus README table |
| A new analysis kind | new `analyze_*_records()` + config key + per-file and aggregate print blocks in `main()` |
| Wildcards in entry names | Selection test in `process_log_file` and entry lookup in both analyzers; see [ROADMAP.md §6](ROADMAP.md#6-entry-patterns-wildcards). Keep the per-entry `entry_flags` memoization or §7's gains are lost |
| Machine-readable output | Consume `AnalysisResult` / `PerFileCounts` from `compute_*`; `asdict()` gives JSON directly |
| Anything that changes printed output | Re-record goldens with `UPDATE_GOLDEN=1` and review `git diff tests/golden/` — that diff is the review artifact |
| Support for array/raw fields | fix defects 1–2 first, then extend the `LoggableType` dispatch in both analyzers |
| Faster ingest on large logs | Already optimized; see §7. The remaining cost is `DataLogIterator.__next__` walking every record |
| CLI flags (verbose, output format) | replace `VERBOSE` constant and manual `sys.argv` handling with `argparse` |
