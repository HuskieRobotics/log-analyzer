#! /usr/bin/env python3
"""The log index: each .wpilog extracted once into SQLite, then queried many times.

    ./log_index.py build ./logs                           # state tier + manifest
    ./log_index.py build ./logs --config checks2026.json  # + what that config reads
    ./log_index.py build ~/practice ./events --index ~/season2026.sqlite
    ./log_index.py list ./logs
    ./analysis.py ./logs checks2026.json --index          # analyse from the index

What is stored (see claude/ROADMAP.md §3.4 and §4):

- **The state tier, in full.** Every boolean, integer, string and array sample,
  json and msgpack, and the struct schemas: about 6% of a match log's records
  and well under a megabyte of it. State transitions, fault text and counters
  are what checks and searches ask about.
- **A manifest of every entry**, stored or not: type, record count, time range,
  and the field names the entry produces once decoded (a struct produces one
  per leaf).
- **Statistics of every numeric value, per robot mode** (disabled, auto,
  teleop): time held, min, max, and time-weighted mean and RMS. These are what
  trends over a season compare, so they exist for every double, stored or not.
- **The numeric tier only where a config needs it.** Doubles and structs are
  most of a log and are rarely searched for, so they stay in the .wpilog unless
  a config passed with --config reads them. The manifest is what makes "reads
  them" precise: `/**/*Connected*` could in principle be a leaf inside any
  struct, but the manifest knows which structs actually produce such a leaf.

A file is identified by the sha256 of its contents, never by its name, so a
renamed or re-copied log is recognised and re-indexing is idempotent. The
.wpilog stays the source of truth: the schema is versioned, and a version change
rebuilds the index rather than migrating it, since extraction is cheap.

**Replay.** `load_log` rebuilds the Log that `analysis.process_log_file` would
have produced, by feeding the stored records through the same `RecordCapture`
in file order. It loads only the entries whose fields the config can match, so
it needs one more thing a full read gets for free: where the read would have
ended. Every entry's manifest therefore records, for each DriverStation state
it was seen in, the last timestamp it reached, which lets the end be computed
for any config's filters without the samples themselves.
"""

import argparse
import hashlib
import json
import math
import mmap
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from capture import (MANDATORY_ENTRIES, STRUCT_PREFIX, STRUCT_SCHEMA_PREFIX,
                     RecordCapture, config_entry_names, filters_from_config, put_record)
from datalog import DataLogReader, DataLogRecord
from entry_patterns import EntryPattern
from Log import Log
from StructDecoder import StructDecoder

__all__ = ["SCHEMA_VERSION", "DEFAULT_INDEX_NAME", "STATE_TYPES", "LogIndex",
           "IndexVersionError", "NotAnIndexError", "LoadResult", "index_version",
           "default_index_path", "parse_log_name", "main"]

# Bump on any change to the tables below or to what extraction records. An
# index of another version is rebuilt, never migrated.
SCHEMA_VERSION = 1

# Kept beside the logs, like analysis.py's .match-cache.json.
DEFAULT_INDEX_NAME = ".log-index.sqlite"

# Entry types stored in full regardless of config: the state tier.
STATE_TYPES = frozenset({"boolean", "int", "int64", "string", "boolean[]", "int[]",
                         "int64[]", "string[]", "json", "msgpack", "structschema"})

NUMERIC_SCALAR_TYPES = frozenset({"int", "int64", "float", "double"})

_DOUBLE = struct.Struct("<d")
_FLOAT = struct.Struct("<f")
_INT64 = struct.Struct("<q")

# Which DriverStation input a boolean entry drives, and the encoding of the
# three inputs (each unknown / false / true) as one small integer.
_DS_SLOT = {"/DriverStation/Enabled": 0, "/DriverStation/Autonomous": 1,
            "/DriverStation/FMSAttached": 2}
_DS_WEIGHTS = (9, 3, 1)

# Robot modes statistics are split by. Before the robot first reports, it
# counts as disabled.
MODES = ("disabled", "auto", "teleop")


def _mode_of(state: List[int]) -> int:
    """Index into MODES for a DriverStation state (digits as in _DS_WEIGHTS)."""
    if state[0] != 2:
        return 0
    return 1 if state[1] == 2 else 2


def _decode_state(state: int) -> Tuple[Optional[bool], Optional[bool], Optional[bool]]:
    """Invert the DriverStation state encoding to (enabled, autonomous, fms)."""
    values = []
    for weight in _DS_WEIGHTS:
        digit = (state // weight) % 3
        values.append(None if digit == 0 else digit == 2)
    return tuple(values)  # type: ignore[return-value]


_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE files (
    id              INTEGER PRIMARY KEY,
    sha256          TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,     -- as first indexed; paths holds the rest
    size            INTEGER NOT NULL,
    recorded_at     TEXT,              -- from the name, when it carries one
    event           TEXT,              -- from the name; often absent
    match           TEXT,              -- from the name; often absent
    fms_attached    INTEGER NOT NULL,  -- ever attached: a match, not a pit session
    enabled_seconds REAL NOT NULL,     -- auto + teleop
    auto_seconds    REAL NOT NULL,
    teleop_seconds  REAL NOT NULL,
    disabled_seconds REAL NOT NULL,    -- including before the robot first reports
    first_us        INTEGER,
    last_us         INTEGER,
    records         INTEGER NOT NULL,
    replay_safe     INTEGER NOT NULL,  -- 0 if a replay could differ from a read
    notes           TEXT NOT NULL,     -- JSON: what a read prints while ingesting
    indexed_at      TEXT NOT NULL
);
CREATE TABLE paths (
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE
);
CREATE TABLE entries (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_seq INTEGER NOT NULL,  -- position of its start record in the file
    name      TEXT NOT NULL,
    type      TEXT NOT NULL,
    metadata  TEXT NOT NULL,
    records   INTEGER NOT NULL,
    first_us  INTEGER,
    last_us   INTEGER,
    stored    INTEGER NOT NULL,  -- 1 if every sample is in samples
    fields    TEXT,              -- JSON field names produced, if not just [name]
    spans     TEXT NOT NULL      -- JSON {DriverStation state: last timestamp}
);
CREATE INDEX entries_by_file ON entries(file_id);
CREATE INDEX entries_by_name ON entries(name);
-- Numeric scalars only, one row per robot mode the entry held a value in.
-- AdvantageKit writes a value only when it changes, so every statistic is
-- weighted by how long each value held: a mean over samples would count a
-- current that sat at 0 A for three minutes once.
CREATE TABLE entry_stats (
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    mode     TEXT NOT NULL,      -- 'disabled', 'auto' or 'teleop'
    samples  INTEGER NOT NULL,   -- records written in this mode
    seconds  REAL NOT NULL,      -- time a value was held in this mode
    min      REAL,
    max      REAL,
    mean     REAL,               -- time-weighted
    rms      REAL,               -- time-weighted; heating goes as current squared
    PRIMARY KEY (entry_id, mode)
) WITHOUT ROWID;
-- value is untyped on purpose: no column affinity, so a string that looks
-- like a number stays a string.
CREATE TABLE samples (
    entry_id INTEGER NOT NULL,
    seq      INTEGER NOT NULL,   -- position of the record in the file
    ts_us    INTEGER NOT NULL,
    raw      BLOB,               -- the record's bytes, unless value rebuilds them
    value,                       -- decoded, for searching; NULL for numeric tier
    PRIMARY KEY (entry_id, seq)
) WITHOUT ROWID;
"""


class IndexVersionError(Exception):
    """The index was written by a different schema version."""


class NotAnIndexError(Exception):
    """The file is not a log index at all."""


def index_version(path: str) -> int:
    """The schema version of the index at `path`.

    Raises NotAnIndexError for anything else - another SQLite database, a log,
    an empty file - so nothing that is not an index is ever overwritten.
    """
    try:
        uri = "file:" + urllib.parse.quote(Path(path).resolve().as_posix()) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        row = None
    if row is None:
        raise NotAnIndexError(f"{path} is not a log index")
    try:
        return int(row[0])
    except ValueError:
        raise NotAnIndexError(f"{path} is not a log index") from None


# === Names ====================================================================

# AdvantageKit: akit_26-05-01_22-45-20_johnson_q121. DataLogManager:
# FRC_20260501_133809_<event>_<match>. Either can lack the event, the match, or
# both, so neither is ever used as an identity.
_LOG_NAME = re.compile(
    r"^(?:akit_\d{2}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}|FRC_\d{8}_\d{6})(?P<rest>(?:_[^_]+)*)$",
    re.IGNORECASE)
_MATCH_TOKEN = re.compile(r"^(?:p|q|e|sf|f|qf)\d+$", re.IGNORECASE)


def parse_log_name(name: str) -> Tuple[Optional[str], Optional[str]]:
    """(event, match) from a log's file name, either None when absent."""
    stem = name[:-len(".wpilog")] if name.endswith(".wpilog") else name
    # sync_logs.py keeps the readable part of a log it gave up on this way.
    if stem.endswith(".truncated"):
        stem = stem[:-len(".truncated")]
    found = _LOG_NAME.match(stem)
    if not found:
        return None, None
    tokens = [token for token in found.group("rest").split("_") if token]
    match = tokens.pop() if tokens and _MATCH_TOKEN.match(tokens[-1]) else None
    event = "_".join(tokens) if tokens else None
    return event, match


def _recorded_at(name: str) -> Optional[str]:
    """The timestamp in a log's name as ISO text, or None if it has none."""
    for pattern in (r"(\d{2})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})",
                    r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"):
        found = re.search(pattern, name)
        if found:
            parts = [int(part) for part in found.groups()]
            if parts[0] < 100:
                parts[0] += 2000
            try:
                return datetime(*parts).isoformat(sep=" ")
            except ValueError:
                continue
    return None


def _log_type_class(type_str: str) -> str:
    """The Log field type an entry type lands in; two entries sharing a name
    but not this class would conflict in a full read."""
    if type_str == "boolean":
        return "boolean"
    if type_str in NUMERIC_SCALAR_TYPES:
        return "number"
    if type_str in ("string", "json"):
        return "string"
    if type_str == "boolean[]":
        return "boolean[]"
    if type_str in ("int[]", "int64[]", "float[]", "double[]"):
        return "number[]"
    if type_str == "string[]":
        return "string[]"
    return "raw"


def _searchable_value(type_str: str, record: DataLogRecord) -> Any:
    """A state-tier record's value in a form SQL can compare, or None.

    Numeric-tier samples (doubles, floats, numeric arrays, structs) get None:
    entry_stats already summarises them, and a second copy of every sample
    would double what they cost to store.
    """
    try:
        if type_str == "boolean":
            return int(record.getBoolean())
        if type_str in ("int", "int64"):
            return record.getInteger()
        if type_str in ("string", "json"):
            return record.getString()
        if type_str == "boolean[]":
            return json.dumps(record.getBooleanArray())
        if type_str in ("int[]", "int64[]"):
            return json.dumps(list(record.getIntegerArray()))
        if type_str == "string[]":
            return json.dumps(record.getStringArray())
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return None


# Types whose raw bytes the searchable value reproduces exactly, as far as any
# decoder can tell: a boolean is re-encoded as 0 or 1 (getBoolean only tests
# for non-zero), an integer as its eight little-endian bytes, a string as its
# UTF-8. These are most of the state tier's rows, so not storing the bytes twice
# matters.
def _rebuild_raw(type_str: str, value: Any) -> Optional[bytes]:
    """The raw bytes a stored value stands for, or None if it cannot say."""
    if value is None:
        return None
    if type_str == "boolean":
        return b"\x01" if value else b"\x00"
    if type_str in ("int", "int64"):
        return _INT64.pack(value)
    if type_str in ("string", "json"):
        return value.encode("utf-8")
    return None


# === Extraction ===============================================================

@dataclass
class _Entry:
    """One declared entry in one file, as the scan accumulates it."""
    start_seq: int
    name: str
    type: str
    metadata: str
    stored: bool
    records: int = 0
    first_us: Optional[int] = None
    last_us: Optional[int] = None
    spans: Dict[int, int] = field(default_factory=dict)
    # Numeric scalars: statistics per mode, and the value currently held.
    modes: Optional[List["_ModeStats"]] = None
    held: Optional[float] = None
    held_since: int = 0
    # Struct entries: one sample per distinct record length. An array of
    # structs produces one set of leaves per element, so its field names
    # depend on the longest array seen; a plain struct has a single length.
    shapes: Dict[int, bytes] = field(default_factory=dict)
    fields: Optional[List[str]] = None


class _ModeStats:
    """Time-weighted statistics of one numeric entry in one robot mode."""
    __slots__ = ("samples", "held_us", "integral", "integral_sq", "minimum", "maximum")

    def __init__(self):
        self.samples = 0
        self.held_us = 0
        self.integral = 0.0
        self.integral_sq = 0.0
        self.minimum: Optional[float] = None
        self.maximum: Optional[float] = None

    def see(self, value: float) -> None:
        if self.minimum is None or value < self.minimum:
            self.minimum = value
        if self.maximum is None or value > self.maximum:
            self.maximum = value

    def hold(self, value: float, duration_us: int) -> None:
        self.held_us += duration_us
        self.integral += value * duration_us
        self.integral_sq += value * value * duration_us
        self.see(value)

    def row(self) -> Tuple[int, float, Optional[float], Optional[float],
                           Optional[float], Optional[float]]:
        """(samples, seconds, min, max, mean, rms)."""
        if not self.held_us:
            return self.samples, 0.0, self.minimum, self.maximum, None, None
        return (self.samples, self.held_us / 1e6, self.minimum, self.maximum,
                self.integral / self.held_us,
                math.sqrt(max(self.integral_sq / self.held_us, 0.0)))


def _release(entry: _Entry, mode: int, ts: int) -> None:
    """Credit the value an entry holds, up to `ts`, to `mode`."""
    if ts > entry.held_since:
        entry.modes[mode].hold(entry.held, ts - entry.held_since)  # type: ignore[index]
        entry.held_since = ts


@dataclass
class _Scan:
    """Everything one pass over a file learned."""
    entries: List[_Entry]
    samples: List[Tuple[int, int, int, bytes]]  # (entry index, seq, ts, raw)
    records: int
    first_us: Optional[int]
    last_us: Optional[int]
    fms_attached: bool
    mode_seconds: List[float]  # indexed like MODES
    notes: List[str]


def _scan(buffer: Any, keep: Optional[Set[int]] = None) -> Optional[_Scan]:
    """Read every record of a log once.

    With `keep` None this is the full extraction pass: manifest, spans, and the
    samples of every state-tier entry. With `keep` a set of start positions it
    only collects those entries' samples, which is how numeric entries a config
    needs are added to a file already indexed.
    """
    reader = DataLogReader(buffer)
    if not reader:
        return None

    full = keep is None
    active: Dict[int, int] = {}        # wpilog entry id -> index into entries
    entries: List[_Entry] = []
    samples: List[Tuple[int, int, int, bytes]] = []
    notes: List[str] = []
    state = [0, 0, 0]                  # DriverStation digits, see _DS_WEIGHTS
    ds_slots: Dict[int, int] = {}      # entry index -> DriverStation slot
    stored_flags: List[bool] = []

    records = 0
    first_us = last_us = None
    fms_attached = False
    mode = 0                           # index into MODES
    mode_since: Optional[int] = None
    mode_us = [0, 0, 0]
    holding: Dict[int, _Entry] = {}    # entries holding a numeric value

    for seq, record in enumerate(reader):
        if record.entry == 0:
            # Mirror analysis.process_log_file exactly, including what it
            # prints, since a replay has to print the same.
            if record.isStart():
                try:
                    data = record.getStartData()
                except TypeError:
                    notes.append("Start(INVALID)")
                    continue
                if data.entry in active:
                    notes.append("...DUPLICATE entry ID, overriding")
                index = len(entries)
                stored = (data.type in STATE_TYPES or data.name.startswith("/.schema/")
                          if full else seq in keep)  # type: ignore[operator]
                entries.append(_Entry(seq, data.name, data.type, data.metadata, stored))
                stored_flags.append(stored)
                active[data.entry] = index
                if data.type == "boolean" and data.name in _DS_SLOT:
                    ds_slots[index] = _DS_SLOT[data.name]
            elif record.isFinish():
                try:
                    entry_id = record.getFinishEntry()
                except TypeError:
                    notes.append("Finish(INVALID)")
                    continue
                if entry_id not in active:
                    notes.append("...ID not found")
                else:
                    del active[entry_id]
            elif record.isSetMetadata():
                try:
                    data = record.getSetMetadataData()
                except TypeError:
                    notes.append("SetMetadata(INVALID)")
                    continue
                if data.entry not in active:
                    notes.append("...ID not found")
                else:
                    entries[active[data.entry]].metadata = data.metadata
            else:
                notes.append("Unrecognized control record")
            continue

        index = active.get(record.entry)
        if index is None:
            continue
        ts = record.timestamp

        if not full:
            if stored_flags[index]:
                samples.append((index, seq, ts, bytes(record.data)))
            continue

        entry = entries[index]
        records += 1
        if first_us is None or ts < first_us:
            first_us = ts
        if last_us is None or ts > last_us:
            last_us = ts
        if mode_since is None:
            mode_since = ts

        slot = ds_slots.get(index)
        if slot is not None:
            try:
                value = record.getBoolean()
            except TypeError:
                value = None
            if value is not None:
                state[slot] = 2 if value else 1
                if slot == 2 and value:
                    fms_attached = True
                new_mode = _mode_of(state)
                if new_mode != mode:
                    # Every value held so far was held in the old mode.
                    for holder in holding.values():
                        _release(holder, mode, ts)
                    if ts > mode_since:
                        mode_us[mode] += ts - mode_since
                        mode_since = ts
                    mode = new_mode

        encoded = state[0] * 9 + state[1] * 3 + state[2]
        spans = entry.spans
        if spans.get(encoded, -1) < ts:
            spans[encoded] = ts

        entry.records += 1
        if entry.first_us is None or ts < entry.first_us:
            entry.first_us = ts
        if entry.last_us is None or ts > entry.last_us:
            entry.last_us = ts

        type_str = entry.type
        if type_str in NUMERIC_SCALAR_TYPES:
            data = record.data
            try:
                if type_str == "double":
                    number = _DOUBLE.unpack(data)[0]
                elif type_str == "float":
                    number = _FLOAT.unpack(data)[0]
                else:
                    number = _INT64.unpack(data)[0]
            except struct.error:
                number = None
            if entry.modes is None:
                entry.modes = [_ModeStats(), _ModeStats(), _ModeStats()]
            if entry.held is not None:
                _release(entry, mode, ts)
            if number is not None and number == number:  # NaN holds nothing
                entry.modes[mode].samples += 1
                entry.modes[mode].see(number)
                entry.held = number
                entry.held_since = ts
                holding[index] = entry
            else:
                entry.held = None
                holding.pop(index, None)
        elif type_str.startswith(STRUCT_PREFIX):
            size = len(record.data)
            if size not in entry.shapes:
                entry.shapes[size] = bytes(record.data)

        if entry.stored:
            samples.append((index, seq, ts, bytes(record.data)))

    if last_us is not None and full:
        # A value holds until the log ends.
        for holder in holding.values():
            _release(holder, mode, last_us)
        if mode_since is not None and last_us > mode_since:
            mode_us[mode] += last_us - mode_since

    return _Scan(entries=entries, samples=samples, records=records, first_us=first_us,
                 last_us=last_us, fms_attached=fms_attached,
                 mode_seconds=[us / 1e6 for us in mode_us], notes=notes)


def _produced_fields(scan: _Scan) -> None:
    """Fill in the field names each structured entry produces once decoded.

    A plain entry produces exactly its own name. A struct produces its leaves
    (and their intermediate nodes), decoded here from one sample of each record
    length against every schema the file carries. Json and msgpack entries are
    stored in full, so their keys come from decoding every sample.
    """
    decoder = StructDecoder()
    by_entry: Dict[int, List[Tuple[int, bytes]]] = {}
    for index, _seq, ts, raw in scan.samples:
        by_entry.setdefault(index, []).append((ts, raw))
    for index, entry in enumerate(scan.entries):
        if entry.name.startswith(STRUCT_SCHEMA_PREFIX):
            for _ts, raw in by_entry.get(index, []):
                try:
                    decoder.add_schema(entry.name[len(STRUCT_SCHEMA_PREFIX):], raw)
                except Exception:
                    pass

    for index, entry in enumerate(scan.entries):
        if entry.type.startswith(STRUCT_PREFIX) and entry.shapes:
            scratch = Log()
            scratch.struct_decoder = decoder
            for raw in entry.shapes.values():
                put_record(scratch, entry.name, entry.type, 0.0,
                           DataLogRecord(0, 0, raw))
            entry.fields = sorted(scratch.get_field_keys())
        elif entry.type in ("json", "msgpack") and index in by_entry:
            scratch = Log()
            for ts, raw in by_entry[index]:
                try:
                    put_record(scratch, entry.name, entry.type, ts / 1000000,
                               DataLogRecord(0, ts, raw))
                except Exception:
                    pass
            entry.fields = sorted(scratch.get_field_keys())
        if entry.fields == [entry.name]:
            entry.fields = None


def _replay_safe(entries: Sequence[_Entry]) -> bool:
    """Whether a partial replay is guaranteed to match a full read.

    It is not when one name is declared with two types that land in different
    Log field types: a full read drops whichever arrives second, and that drop
    depends on records a partial replay may not load.
    """
    classes: Dict[str, str] = {}
    for entry in entries:
        kind = _log_type_class(entry.type)
        if classes.setdefault(entry.name, kind) != kind:
            return False
    return True


def file_digest(path: str) -> str:
    """sha256 of a file's contents: its identity in the index."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# === The index ================================================================

@dataclass
class LoadResult:
    """What `LogIndex.load_log` produced: a Log, or why it could not."""
    log: Optional[Log]
    reason: Optional[str] = None   # why the .wpilog must be read instead
    notes: List[str] = field(default_factory=list)  # printed by a full read


@dataclass
class BuildResult:
    """What indexing one file did."""
    name: str
    action: str            # "indexed", "extended", "unchanged", "skipped"
    detail: str = ""
    seconds: float = 0.0


class LogIndex:
    """An open index file."""

    def __init__(self, path: str, create: bool = False):
        self.path = path
        exists = os.path.exists(path)
        if not exists and not create:
            raise FileNotFoundError(path)
        if exists:
            version = index_version(path)  # raises if this is not an index
            if version != SCHEMA_VERSION:
                raise IndexVersionError(
                    f"{path} has index format {version}, this version reads "
                    f"{SCHEMA_VERSION}")
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        if not exists:
            self._create()

    @classmethod
    def open_for_build(cls, path: str) -> Tuple["LogIndex", Optional[str]]:
        """Open an index to add to, rebuilding it if its format is out of date.

        Returns the index and, if it was rebuilt, a sentence saying why. A file
        at `path` that is not an index at all raises NotAnIndexError rather than
        being replaced: a mistyped --index must never delete a log.
        """
        try:
            return cls(path, create=True), None
        except IndexVersionError as error:
            os.remove(path)
            return cls(path, create=True), f"{error}; rebuilt it from the logs"

    def _create(self) -> None:
        with self.conn:
            self.conn.executescript(_SCHEMA)
            self.conn.execute("INSERT INTO meta VALUES ('schema_version', ?)",
                              (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self.conn.close()

    # --- identity -------------------------------------------------------------

    def file_id_for(self, path: str) -> Optional[int]:
        """The indexed file a path holds, or None."""
        return self._lookup(path)[0]

    def _lookup(self, path: str) -> Tuple[Optional[int], Optional[str]]:
        """(file id or None, sha256 if it had to be computed).

        A path seen before with the same size and mtime is trusted without
        re-hashing, so looking up a season's logs costs a stat each. Anything
        else is hashed, which recognises a renamed or re-copied log.
        """
        key = os.path.abspath(path)
        stat = os.stat(path)
        row = self.conn.execute(
            "SELECT file_id FROM paths WHERE path = ? AND size = ? AND mtime_ns = ?",
            (key, stat.st_size, stat.st_mtime_ns)).fetchone()
        if row:
            return row[0], None
        digest = file_digest(path)
        found = self.conn.execute("SELECT id FROM files WHERE sha256 = ?",
                                  (digest,)).fetchone()
        if found:
            self._remember_path(path, found[0])
            return found[0], digest
        return None, digest

    def _remember_path(self, path: str, file_id: int) -> None:
        stat = os.stat(path)
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO paths VALUES (?, ?, ?, ?)",
                (os.path.abspath(path), stat.st_size, stat.st_mtime_ns, file_id))

    # --- building -------------------------------------------------------------

    def add(self, path: str, patterns: Sequence[str] = ()) -> BuildResult:
        """Index one log, and store every entry the given patterns read.

        Idempotent: a file already indexed is only extended, and only if the
        patterns read an entry not yet stored.
        """
        name = os.path.basename(path)
        started = time.monotonic()
        before = os.stat(path)
        file_id, digest = self._lookup(path)
        if file_id is not None:
            added = self._extend(file_id, path, patterns)
            if not added:
                return BuildResult(name, "unchanged")
            return BuildResult(name, "extended",
                               f"stored {added} more entr{'y' if added == 1 else 'ies'}",
                               time.monotonic() - started)

        with open(path, "rb") as handle:
            buffer = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                scan = _scan(buffer)
            finally:
                buffer.close()
        if scan is None:
            return BuildResult(name, "skipped", "not a valid log file")

        after = os.stat(path)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            return BuildResult(name, "skipped", "still being written")

        _produced_fields(scan)
        event, match = parse_log_name(name)
        with self.conn:
            file_id = self.conn.execute(
                "INSERT INTO files (sha256, name, size, recorded_at, event, match, "
                "fms_attached, enabled_seconds, auto_seconds, teleop_seconds, "
                "disabled_seconds, first_us, last_us, records, "
                "replay_safe, notes, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (digest, name, after.st_size, _recorded_at(name), event, match,
                 int(scan.fms_attached), scan.mode_seconds[1] + scan.mode_seconds[2],
                 scan.mode_seconds[1], scan.mode_seconds[2], scan.mode_seconds[0],
                 scan.first_us,
                 scan.last_us, scan.records, int(_replay_safe(scan.entries)),
                 json.dumps(scan.notes),
                 datetime.now(timezone.utc).isoformat(timespec="seconds"))).lastrowid
            ids = self._insert_entries(file_id, scan.entries)
            self._insert_samples(ids, scan.entries, scan.samples)
            self.conn.execute("INSERT OR REPLACE INTO paths VALUES (?, ?, ?, ?)",
                              (os.path.abspath(path), after.st_size,
                               after.st_mtime_ns, file_id))
        added = self._extend(file_id, path, patterns)
        detail = f"{scan.records:,} records, {len(scan.entries)} entries"
        if added:
            detail += f", {added} numeric entr{'y' if added == 1 else 'ies'} stored"
        return BuildResult(name, "indexed", detail, time.monotonic() - started)

    def _insert_entries(self, file_id: int, entries: Sequence[_Entry]) -> List[int]:
        ids = []
        stats = []
        for entry in entries:
            entry_id = self.conn.execute(
                "INSERT INTO entries (file_id, start_seq, name, type, metadata, records, "
                "first_us, last_us, stored, fields, spans) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, entry.start_seq, entry.name, entry.type, entry.metadata,
                 entry.records, entry.first_us, entry.last_us, int(entry.stored),
                 json.dumps(entry.fields) if entry.fields is not None else None,
                 json.dumps({str(k): v for k, v in sorted(entry.spans.items())}))
            ).lastrowid
            ids.append(entry_id)
            for mode, mode_stats in zip(MODES, entry.modes or ()):
                if mode_stats.samples or mode_stats.held_us:
                    stats.append((entry_id, mode) + mode_stats.row())
        self.conn.executemany("INSERT INTO entry_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              stats)
        return ids

    def _insert_samples(self, ids: Sequence[int], entries: Sequence[_Entry],
                        samples: Iterable[Tuple[int, int, int, bytes]]) -> None:
        def rows():
            for index, seq, ts, raw in samples:
                type_str = entries[index].type
                value = _searchable_value(type_str, DataLogRecord(0, ts, raw))
                if _rebuild_raw(type_str, value) == raw:
                    raw = None  # the value says it all
                yield ids[index], seq, ts, raw, value

        self.conn.executemany("INSERT OR IGNORE INTO samples VALUES (?, ?, ?, ?, ?)",
                              rows())

    def _extend(self, file_id: int, path: str, patterns: Sequence[str]) -> int:
        """Store the not-yet-stored entries the patterns read; return how many."""
        if not patterns:
            return 0
        compiled = _compile(patterns)
        wanted = [row for row in self._entry_rows(file_id)
                  if not row.stored and row.reads(compiled)]
        if not wanted:
            return 0
        by_seq = {row.start_seq: row for row in wanted}
        with open(path, "rb") as handle:
            buffer = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                scan = _scan(buffer, keep=set(by_seq))
            finally:
                buffer.close()
        if scan is None:
            return 0
        ids = [by_seq[entry.start_seq].id if entry.start_seq in by_seq else -1
               for entry in scan.entries]
        with self.conn:
            self._insert_samples(ids, scan.entries, scan.samples)
            self.conn.executemany("UPDATE entries SET stored = 1 WHERE id = ?",
                                  [(row.id,) for row in wanted])
        return len(wanted)

    # --- reading --------------------------------------------------------------

    def _entry_rows(self, file_id: int) -> List["_EntryRow"]:
        return [_EntryRow(*row) for row in self.conn.execute(
            "SELECT id, start_seq, name, type, stored, fields, spans FROM entries "
            "WHERE file_id = ? ORDER BY start_seq", (file_id,))]

    def load_log(self, path: str, config: Dict[str, Any]) -> LoadResult:
        """The Log `analysis.process_log_file` would build for this config.

        Returns a LoadResult with no Log, and the reason, when the index cannot
        reproduce it exactly: the file is not indexed, or the config reads an
        entry whose samples were not stored.
        """
        try:
            file_id = self.file_id_for(path)
        except OSError as error:
            return LoadResult(None, f"cannot read it: {error.strerror}")
        if file_id is None:
            return LoadResult(None, "not indexed")
        safe, notes_json = self.conn.execute(
            "SELECT replay_safe, notes FROM files WHERE id = ?", (file_id,)).fetchone()
        if not safe:
            return LoadResult(None, "an entry name is declared with two types")

        mandatory = set(MANDATORY_ENTRIES)
        targets = config_entry_names(config)
        capture = RecordCapture(mandatory, targets, *filters_from_config(config))
        patterns = _compile(sorted(targets | mandatory))

        needed: Dict[int, Tuple[str, str, Any]] = {}
        missing: List[str] = []
        until: Optional[int] = None
        for row in self._entry_rows(file_id):
            flags = capture.flags_for(row.name, row.type)
            is_mandatory, is_target, is_schema, ds_slot = flags
            captured = is_mandatory or is_target
            if captured:
                # Where a full read would have ended: the last record of any
                # captured entry, counting a target's records only in the
                # DriverStation states the filters let through.
                for state, last in row.span_items():
                    if is_mandatory or capture.passes(*_decode_state(state)):
                        if until is None or last > until:
                            until = last
            if is_schema or ds_slot or (captured and row.reads(patterns)):
                if not row.stored:
                    missing.append(row.name)
                else:
                    needed[row.id] = (row.name, row.type, flags)

        if missing:
            shown = ", ".join(sorted(set(missing))[:3])
            more = len(set(missing)) - 3
            return LoadResult(None, f"needs entries not stored: {shown}"
                                    + (f" and {more} more" if more > 0 else ""))

        if needed:
            marks = ",".join("?" * len(needed))
            rows = self.conn.execute(
                f"SELECT entry_id, ts_us, raw, value FROM samples "
                f"WHERE entry_id IN ({marks}) ORDER BY seq", list(needed))
            add = capture.add
            for entry_id, ts, raw, value in rows:
                name, type_str, flags = needed[entry_id]
                if raw is None:
                    raw = _rebuild_raw(type_str, value)
                add(name, type_str, DataLogRecord(0, ts, raw), flags)

        if until is not None:
            capture.log.captured_until = until / 1000000
        return LoadResult(capture.log, notes=json.loads(notes_json))

    def files(self) -> List[Dict[str, Any]]:
        """Every indexed file, oldest recording first."""
        columns = ("id", "name", "size", "recorded_at", "event", "match",
                   "fms_attached", "enabled_seconds", "records", "replay_safe")
        rows = self.conn.execute(
            f"SELECT {', '.join(columns)}, "
            "(SELECT COUNT(*) FROM entries WHERE file_id = files.id), "
            "(SELECT COUNT(*) FROM entries WHERE file_id = files.id AND stored) "
            "FROM files ORDER BY recorded_at, name")
        return [dict(zip(columns + ("entries", "stored"), row)) for row in rows]


@dataclass
class _EntryRow:
    """An entry as the index holds it."""
    id: int
    start_seq: int
    name: str
    type: str
    stored: int
    fields_json: Optional[str]
    spans_json: str

    def fields(self) -> List[str]:
        return json.loads(self.fields_json) if self.fields_json else [self.name]

    def span_items(self) -> List[Tuple[int, int]]:
        return [(int(state), last) for state, last in json.loads(self.spans_json).items()]

    def reads(self, patterns: Sequence[EntryPattern]) -> bool:
        """Whether any field this entry produces is one the patterns match."""
        return any(pattern.matches(name) for name in self.fields() for pattern in patterns)


def _compile(names: Iterable[str]) -> List[EntryPattern]:
    return [EntryPattern(name) for name in names]


def default_index_path(log_folder: str) -> str:
    return os.path.join(log_folder, DEFAULT_INDEX_NAME)


def config_patterns(config_paths: Sequence[str]) -> List[str]:
    """Every entry name or pattern the given config files read."""
    names: Set[str] = set()
    for config_path in config_paths:
        with open(config_path, "r") as handle:
            names |= config_entry_names(json.load(handle))
    return sorted(names)


# === Command line =============================================================

def _size_text(size: int) -> str:
    return f"{size / 1e6:.1f} MB"


def find_logs(sources: Sequence[str]) -> List[str]:
    """Every .wpilog named, or found anywhere under a named folder.

    A season's logs end up in many places - one folder per event, practice
    sessions synced from different laptops - so folders are walked whole.
    Hidden folders are skipped, which keeps out caches and editor state.
    """
    found: Set[str] = set()
    for source in sources:
        if os.path.isfile(source):
            found.add(source)
            continue
        for folder, subfolders, names in os.walk(source):
            subfolders[:] = sorted(name for name in subfolders if not name.startswith("."))
            found.update(os.path.join(folder, name) for name in names
                         if name.endswith(".wpilog"))
    return sorted(found)


def _build(args: argparse.Namespace) -> int:
    for source in args.sources:
        if not os.path.exists(source):
            print(f"Error: {source} does not exist", file=sys.stderr)
            return 1
    if args.index is None and (len(args.sources) > 1 or not os.path.isdir(args.sources[0])):
        print("Error: name the index with --index when indexing more than one folder, "
              "so that every build adds to the same one", file=sys.stderr)
        return 1
    try:
        patterns = config_patterns(args.config)
    except (OSError, json.JSONDecodeError) as error:
        print(f"Error loading config file: {error}", file=sys.stderr)
        return 1

    logs = find_logs(args.sources)
    if not logs:
        print(f"No log files found in {', '.join(args.sources)}", file=sys.stderr)
        return 1

    path = args.index or default_index_path(args.sources[0])
    try:
        if args.rebuild and os.path.exists(path):
            index_version(path)  # refuses anything that is not an index
            os.remove(path)
        index, rebuilt = LogIndex.open_for_build(path)
    except NotAnIndexError as error:
        print(f"Error: {error}; not replacing it", file=sys.stderr)
        return 1
    if rebuilt:
        print(rebuilt)


    counts: Dict[str, int] = {}
    started = time.monotonic()
    try:
        for number, log_path in enumerate(logs, 1):
            print(f"[{number}/{len(logs)}] {os.path.basename(log_path)} "
                  f"({_size_text(os.path.getsize(log_path))}) ...", end="", flush=True)
            result = index.add(log_path, patterns)
            counts[result.action] = counts.get(result.action, 0) + 1
            text = result.action
            if result.detail:
                text += f": {result.detail}"
            if result.seconds >= 0.05:
                text += f" ({result.seconds:.1f} s)"
            print(f" {text}")
    finally:
        index.close()

    summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
    print(f"\n{summary} in {time.monotonic() - started:.0f} s; "
          f"index is {_size_text(os.path.getsize(path))} at {path}")
    return 0


def _list(args: argparse.Namespace) -> int:
    if not (args.index or args.log_folder):
        print("Error: give a log folder or --index", file=sys.stderr)
        return 1
    path = args.index or default_index_path(args.log_folder)
    try:
        index = LogIndex(path)
    except FileNotFoundError:
        print(f"No index at {path}; build one with: log_index.py build {args.log_folder}",
              file=sys.stderr)
        return 1
    except IndexVersionError as error:
        print(f"{error}; rebuild it with: log_index.py build {args.log_folder}",
              file=sys.stderr)
        return 1
    except NotAnIndexError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    try:
        files = index.files()
    finally:
        index.close()
    print(f"{len(files)} logs in {path} ({_size_text(os.path.getsize(path))})\n")
    print(f"{'recorded':19}  {'event':10} {'match':6} {'FMS':3} {'enabled':>8} "
          f"{'size':>8} {'stored':>9}  name")
    for row in files:
        print(f"{row['recorded_at'] or '-':19}  {row['event'] or '-':10} "
              f"{row['match'] or '-':6} {'yes' if row['fms_attached'] else 'no':3} "
              f"{row['enabled_seconds']:7.1f}s {_size_text(row['size']):>8} "
              f"{row['stored']:>4}/{row['entries']:<4}  {row['name']}"
              + ("" if row["replay_safe"] else "  (read from .wpilog: conflicting types)"))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract .wpilog files once into a SQLite index, so analyses and "
                    "searches across a season do not re-read every log.")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser(
        "build", help="index every .wpilog under the given folders (only new ones "
                      "are read)")
    build.add_argument("sources", nargs="+", metavar="LOG_FOLDER",
                       help="folders to search, subfolders included, or single "
                            ".wpilog files; more than one needs --index")
    build.add_argument("--config", action="append", default=[], metavar="JSON",
                       help="also store every numeric entry this analysis config "
                            "reads, so analysis.py --index can run it without the "
                            ".wpilog; repeatable")
    build.add_argument("--index", metavar="PATH",
                       help=f"index file; a season library spanning several folders "
                            f"names one here (default with one folder: "
                            f"{DEFAULT_INDEX_NAME} in it)")
    build.add_argument("--rebuild", action="store_true",
                       help="discard the index and extract every log again")
    build.set_defaults(handler=_build)

    listing = commands.add_parser("list", help="show what the index holds")
    listing.add_argument("log_folder", nargs="?",
                         help="directory the index was built for (or give --index)")
    listing.add_argument("--index", metavar="PATH",
                         help=f"index file (default: {DEFAULT_INDEX_NAME} in the folder)")
    listing.set_defaults(handler=_list)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
