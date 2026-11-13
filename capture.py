"""Turning a stream of log records into a Log, under a config's capture rules.

A Log is built in one of two ways: by reading a .wpilog front to back
(`analysis.process_log_file`), or by replaying records held in the index
(`log_index.load_log`). Both feed records through `RecordCapture`, in the order
they appear in the file. That shared path is what makes a Log replayed from the
index identical to one read from the file; there is no second implementation of
the capture rules to drift.
"""

from typing import Any, Dict, Iterable, Optional, Set, Tuple

from Log import Log
from entry_patterns import EntryPattern

__all__ = ["STRUCT_PREFIX", "STRUCT_SCHEMA_PREFIX", "DS_ENABLED", "DS_AUTONOMOUS",
           "DS_FMS_ATTACHED", "MANDATORY_ENTRIES", "EntryFlags", "config_entry_names",
           "filters_from_config", "passes_filter", "put_record", "RecordCapture"]

# Constants for structured types
STRUCT_PREFIX = "struct:"

# Struct schemas are published as entries named "/.schema/struct:<Type>".
# Protobuf schemas share the namespace as "/.schema/proto:<Type>", and must not
# reach the struct decoder: the name has no struct type to extract.
STRUCT_SCHEMA_PREFIX = "/.schema/" + STRUCT_PREFIX

DS_ENABLED = "/DriverStation/Enabled"
DS_AUTONOMOUS = "/DriverStation/Autonomous"
DS_FMS_ATTACHED = "/DriverStation/FMSAttached"

# Always captured regardless of the config: the filters and the enabled gate
# read them.
MANDATORY_ENTRIES = frozenset({DS_ENABLED, DS_AUTONOMOUS, DS_FMS_ATTACHED})

# Which DriverStation input a boolean entry drives, by name. 0 is none.
_DS_SLOTS = {DS_ENABLED: 1, DS_AUTONOMOUS: 2, DS_FMS_ATTACHED: 3}

# (is_mandatory, is_target, is_schema, driver station slot), fixed per entry.
EntryFlags = Tuple[bool, bool, bool, int]


def config_entry_names(config: Dict[str, Any]) -> Set[str]:
    """Every entry name or pattern a config reads, plus the mandatory entries."""
    names = set(MANDATORY_ENTRIES)
    for analysis in config.get("timeAnalysis", []):
        for role in ("startEntry", "endEntry"):
            if analysis.get(role):
                names.add(analysis[role])
    for analysis in config.get("valueAnalysis", []):
        for role in ("entry", "triggerEntry"):
            if analysis.get(role):
                names.add(analysis[role])
    for rule in config.get("checks", []):
        if rule.get("entry"):
            names.add(rule["entry"])
    return names


def filters_from_config(config: Dict[str, Any]) -> Tuple[bool, bool, str]:
    """(filter on enabled, filter on FMS attached, robot mode) from a config."""
    return (config.get("enabled", False), config.get("fmsAttached", False),
            config.get("robotMode", "both"))


def passes_filter(enabled: Optional[bool], autonomous: Optional[bool],
                  fms_attached: Optional[bool], filter_enabled: bool,
                  filter_fms_attached: bool, robot_mode: str) -> bool:
    """Whether a record arriving in this DriverStation state is captured.

    A state not yet reported (None) never excludes anything, so records logged
    before the DriverStation entries first appear are kept.
    """
    if filter_enabled and enabled is not None and not enabled:
        return False
    if filter_fms_attached and fms_attached is not None and not fms_attached:
        return False
    if robot_mode == "auto" and autonomous is not None and not autonomous:
        return False
    if robot_mode == "teleop" and autonomous is not None and autonomous:
        return False
    return True


def put_record(log: Log, name: str, type_str: str, timestamp: float, record: Any) -> None:
    """Decode one data record by its entry's declared type and add it to a Log."""
    if type_str == "boolean":
        log.put_boolean(name, timestamp, record.getBoolean())
    elif type_str in ("int", "int64"):
        log.put_number(name, timestamp, record.getInteger())
    elif type_str == "float":
        log.put_number(name, timestamp, record.getFloat())
    elif type_str == "double":
        log.put_number(name, timestamp, record.getDouble())
    elif type_str == "string":
        log.put_string(name, timestamp, record.getString())
    elif type_str == "boolean[]":
        log.put_boolean_array(name, timestamp, record.getBooleanArray())
    elif type_str in ("int[]", "int64[]"):
        log.put_number_array(name, timestamp, record.getIntegerArray())
    elif type_str == "float[]":
        log.put_number_array(name, timestamp, record.getFloatArray())
    elif type_str == "double[]":
        log.put_number_array(name, timestamp, record.getDoubleArray())
    elif type_str == "string[]":
        log.put_string_array(name, timestamp, record.getStringArray())
    elif type_str == "json":
        log.put_json(name, timestamp, record.getString())
    elif type_str == "msgpack":
        log.put_msgpack(name, timestamp, record.data)  # getRaw() equivalent
    elif type_str.startswith(STRUCT_PREFIX):
        schema_type = type_str[len(STRUCT_PREFIX):]
        if schema_type.endswith("[]"):
            log.put_struct(name, timestamp, record.data, schema_type[:-2], True)
        else:
            log.put_struct(name, timestamp, record.data, schema_type, False)
    else:  # Default to raw
        log.put_raw(name, timestamp, record.data)
        # Note: CustomSchemas functionality not implemented in Python version


class RecordCapture:
    """Applies a config's capture rules to records, in file order, building a Log.

    Records of mandatory entries are always kept. Records of target entries are
    kept when the DriverStation state at that moment passes the config's
    filters, which is why order matters: the state is whatever the most recent
    DriverStation record set it to.
    """

    def __init__(self, mandatory_entries: Iterable[str], target_entry_names: Iterable[str],
                 filter_enabled: bool = False, filter_fms_attached: bool = False,
                 robot_mode: str = "both"):
        self.log = Log()
        self.filter_enabled = filter_enabled
        self.filter_fms_attached = filter_fms_attached
        self.robot_mode = robot_mode
        # Patterns are compiled once, then tested once per entry (not once per
        # record), which keeps matching off the hot path.
        self._mandatory = [EntryPattern(name) for name in mandatory_entries]
        self._targets = [EntryPattern(name) for name in target_entry_names]
        self._flags: Dict[Tuple[str, str], EntryFlags] = {}
        # Most recent values of the DriverStation entries, indexed by slot.
        self._state = [None, None, None, None]

    def flags_for(self, name: str, type_str: str) -> EntryFlags:
        """Whether an entry is mandatory / targeted / a struct schema / DS input.

        All four depend only on the entry's name and type, which are fixed when
        it is declared, so callers look this up once per entry rather than once
        per record.
        """
        key = (name, type_str)
        flags = self._flags.get(key)
        if flags is None:
            flags = (any(pattern.could_contain(name) for pattern in self._mandatory),
                     any(pattern.could_contain(name) for pattern in self._targets),
                     name.startswith(STRUCT_SCHEMA_PREFIX),
                     _DS_SLOTS.get(name, 0) if type_str == "boolean" else 0)
            self._flags[key] = flags
        return flags

    def passes(self, enabled: Optional[bool], autonomous: Optional[bool],
               fms_attached: Optional[bool]) -> bool:
        """Whether a target record arriving in this DriverStation state is kept."""
        return passes_filter(enabled, autonomous, fms_attached, self.filter_enabled,
                             self.filter_fms_attached, self.robot_mode)

    def add(self, name: str, type_str: str, record: Any, flags: EntryFlags) -> None:
        """Process one data record of the entry `name` (declared as `type_str`)."""
        is_mandatory, is_target, is_schema, ds_slot = flags

        if ds_slot:
            try:
                self._state[ds_slot] = record.getBoolean()
            except TypeError:
                # If we can't read the value, continue without updating state
                pass

        if is_schema:
            self.log.struct_decoder.add_schema(name[len(STRUCT_SCHEMA_PREFIX):],
                                               record.getBytes())

        if is_mandatory or (is_target and self.passes(self._state[1], self._state[2],
                                                      self._state[3])):
            put_record(self.log, name, type_str, record.timestamp / 1000000, record)
