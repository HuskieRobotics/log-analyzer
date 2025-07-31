"""
A simplified Python version of the TypeScript classes that are part of AdvantageScope:
https://github.com/Mechanical-Advantage/AdvantageScope/blob/main/src/shared/log
"""

from enum import Enum
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
import json
import msgpack
from StructDecoder import StructDecoder

__all__ = ["LoggableType", "LogValueSet", "LogValueSetRaw", "LogValueSetBoolean",
           "LogValueSetNumber", "LogValueSetString", "LogValueSetBooleanArray",
           "LogValueSetNumberArray", "LogValueSetStringArray", "LogField",
           "Log"]

# === Core Types ===

class LoggableType(Enum):
    """Types of log data that can be stored."""
    RAW = "raw"
    BOOLEAN = "boolean"
    NUMBER = "number"
    STRING = "string"
    BOOLEAN_ARRAY = "boolean_array"
    NUMBER_ARRAY = "number_array"
    STRING_ARRAY = "string_array"
    EMPTY = "empty"  # Placeholder for child fields of structured data

@dataclass
class LogValueSet:
    """Base class for log value sets."""
    timestamps: List[float] = field(default_factory=list)
    values: List[Any] = field(default_factory=list)

@dataclass
class LogValueSetRaw(LogValueSet):
    values: List[bytes] = field(default_factory=list)

@dataclass
class LogValueSetBoolean(LogValueSet):
    values: List[bool] = field(default_factory=list)

@dataclass
class LogValueSetNumber(LogValueSet):
    values: List[float] = field(default_factory=list)

@dataclass
class LogValueSetString(LogValueSet):
    values: List[str] = field(default_factory=list)

@dataclass
class LogValueSetBooleanArray(LogValueSet):
    values: List[List[bool]] = field(default_factory=list)

@dataclass
class LogValueSetNumberArray(LogValueSet):
    values: List[List[float]] = field(default_factory=list)

@dataclass
class LogValueSetStringArray(LogValueSet):
    values: List[List[str]] = field(default_factory=list)

# === Log Field ===

class LogField:
    """A full log field that contains data."""
    
    def __init__(self, log_type: LoggableType):
        self.type = log_type
        self.data = LogValueSet()
        self.structured_type: Optional[str] = None
        self.type_warning: bool = False
    
    def get_type(self) -> LoggableType:
        """Returns the constant field type."""
        return self.type
    
    def get_timestamps(self) -> List[float]:
        """Returns the full set of ordered timestamps."""
        return self.data.timestamps.copy()
    
    def clear_before_time(self, clear_timestamp: float) -> None:
        """Clears all data before the provided timestamp."""
        i = 0
        while i < len(self.data.timestamps):
            if (len(self.data.timestamps) >= 2 and 
                i + 1 < len(self.data.timestamps) and 
                self.data.timestamps[i + 1] < clear_timestamp):
                self.striping_reference = not self.striping_reference
                i += 1
                continue
            
            # Remove elements before index i
            self.data.timestamps = self.data.timestamps[i:]
            self.data.values = self.data.values[i:]
            break
        
        # Adjust first timestamp if needed
        if (self.data.timestamps and 
            self.data.timestamps[0] < clear_timestamp):
            self.data.timestamps[0] = clear_timestamp
    
    def get_range(self, start: float, end: float) -> LogValueSet:
        """Returns values in the specified timestamp range."""
        # Implement range retrieval with caching
        result_timestamps = []
        result_values = []
        
        for i, timestamp in enumerate(self.data.timestamps):
            if start < timestamp <= end:
                result_timestamps.append(timestamp)
                result_values.append(self.data.values[i])
        
        result = LogValueSet()
        result.timestamps = result_timestamps
        result.values = result_values
        return result
    
    # Specific type getters
    def get_raw(self, start: float, end: float) -> Optional[LogValueSetRaw]:
        if self.type != LoggableType.RAW:
            return None
        range_data = self.get_range(start, end)
        return LogValueSetRaw(range_data.timestamps, range_data.values)
    
    def get_boolean(self, start: float, end: float) -> Optional[LogValueSetBoolean]:
        if self.type != LoggableType.BOOLEAN:
            return None
        range_data = self.get_range(start, end)
        return LogValueSetBoolean(range_data.timestamps, range_data.values)
    
    def get_number(self, start: float, end: float) -> Optional[LogValueSetNumber]:
        if self.type != LoggableType.NUMBER:
            return None
        range_data = self.get_range(start, end)
        return LogValueSetNumber(range_data.timestamps, range_data.values)
    
    def get_string(self, start: float, end: float) -> Optional[LogValueSetString]:
        if self.type != LoggableType.STRING:
            return None
        range_data = self.get_range(start, end)
        return LogValueSetString(range_data.timestamps, range_data.values)
    
    # Putters for different types
    def put_raw(self, timestamp: float, value: bytes) -> None:
        """Writes a new Raw value to the field."""
        if self.type != LoggableType.RAW:
            self.type_warning = True
            return
        self._insert_value(timestamp, value)
    
    def put_boolean(self, timestamp: float, value: bool) -> None:
        """Writes a new Boolean value to the field."""
        if self.type != LoggableType.BOOLEAN:
            self.type_warning = True
            return
        self._insert_value(timestamp, value)
    
    def put_number(self, timestamp: float, value: float) -> None:
        """Writes a new Number value to the field."""
        if self.type != LoggableType.NUMBER:
            self.type_warning = True
            return
        self._insert_value(timestamp, value)
    
    def put_string(self, timestamp: float, value: str) -> None:
        """Writes a new String value to the field."""
        if self.type != LoggableType.STRING:
            self.type_warning = True
            return
        self._insert_value(timestamp, value)
    
    def put_boolean_array(self, timestamp: float, value: List[bool]) -> None:
        """Writes a new BooleanArray value to the field."""
        if self.type != LoggableType.BOOLEAN_ARRAY:
            self.type_warning = True
            return
        self._insert_value(timestamp, value)
    
    def put_number_array(self, timestamp: float, value: List[float]) -> None:
        """Writes a new NumberArray value to the field."""
        if self.type != LoggableType.NUMBER_ARRAY:
            self.type_warning = True
            return
        self._insert_value(timestamp, value)
    
    def put_string_array(self, timestamp: float, value: List[str]) -> None:
        """Writes a new StringArray value to the field."""
        if self.type != LoggableType.STRING_ARRAY:
            self.type_warning = True
            return
        self._insert_value(timestamp, value)
    
    def _insert_value(self, timestamp: float, value: Any) -> None:
        """Insert a value at the correct timestamp position."""
        # Find insertion point
        insert_index = len(self.data.timestamps)
        for i, ts in enumerate(self.data.timestamps):
            if ts > timestamp:
                insert_index = i
                break
        
        self.data.timestamps.insert(insert_index, timestamp)
        self.data.values.insert(insert_index, value)

# === Main Log Class ===

class Log:
    """Represents a collection of log fields."""
    
    def __init__(self):
        self.DEFAULT_TIMESTAMP_RANGE = (0.0, 10.0)
        self.msgpack_decoder = msgpack
        self.struct_decoder = StructDecoder()
        
        self.fields: Dict[str, LogField] = {}
        self.generated_parents: Set[str] = set()
        self.timestamp_range: Optional[Tuple[float, float]] = None
    
    def create_blank_field(self, key: str, log_type: LoggableType) -> None:
        """Checks if the field exists and registers it if necessary."""
        if key in self.fields:
            return
        self.fields[key] = LogField(log_type)
    
    def delete_field(self, key: str) -> None:
        """Removes all data for a field."""
        if key in self.fields:
            del self.fields[key]
            self.generated_parents.discard(key)
    
    def clear_before_time(self, timestamp: float) -> None:
        """Clears all data before the provided timestamp."""
        if self.timestamp_range is None:
            self.timestamp_range = (timestamp, timestamp)
        elif self.timestamp_range[0] < timestamp:
            new_end = max(timestamp, self.timestamp_range[1])
            self.timestamp_range = (timestamp, new_end)
        
        # Clear field data
        for field in self.fields.values():
            field.clear_before_time(timestamp)
    
    def update_range_with_timestamp(self, timestamp: float) -> None:
        """Adjusts the timestamp range based on a known timestamp."""
        if self.timestamp_range is None:
            self.timestamp_range = (timestamp, timestamp)
        else:
            start, end = self.timestamp_range
            new_start = min(start, timestamp)
            new_end = max(end, timestamp)
            self.timestamp_range = (new_start, new_end)
    
    def _process_timestamp(self, key: str, timestamp: float) -> None:
        """Updates the timestamp range and set caches if necessary."""
        self.update_range_with_timestamp(timestamp)
    
    def get_field_keys(self) -> List[str]:
        """Returns an array of registered field keys."""
        return list(self.fields.keys())
    
    def get_field_count(self) -> int:
        """Returns the count of fields (excluding array item fields)."""
        return len([field for field in self.fields.keys() if not self.is_generated(field)])
    
    def get_field(self, key: str) -> Optional[LogField]:
        """Returns the internal field object for a key."""
        return self.fields.get(key)
    
    def set_field(self, key: str, field: LogField) -> None:
        """Adds an existing log field to this log."""
        self.fields[key] = field
    
    def get_type(self, key: str) -> Optional[LoggableType]:
        """Returns the constant field type."""
        field = self.fields.get(key)
        return field.get_type() if field else None
    
    def get_structured_type(self, key: str) -> Optional[str]:
        """Returns the structured type string for a field."""
        field = self.fields.get(key)
        return field.structured_type if field else None
    
    def set_structured_type(self, key: str, type_str: Optional[str]) -> None:
        """Sets the structured type string for a field."""
        field = self.fields.get(key)
        if field:
            field.structured_type = type_str
    
    def get_type_warning(self, key: str) -> bool:
        """Returns whether there was an attempt to write a conflicting type to a field."""
        field = self.fields.get(key)
        return field.type_warning if field else False
            
    def is_generated(self, key: str) -> bool:
        """Returns whether the key is generated."""
        return self.get_generated_parent(key) is not None
    
    def get_generated_parent(self, key: str) -> Optional[str]:
        """If the key is generated, returns its parent."""
        for parent_key in self.generated_parents:
            if (len(key) > len(parent_key) + 1 and 
                key.startswith(parent_key + "/")):
                return parent_key
        return None
    
    def is_generated_parent(self, key: str) -> bool:
        """Returns whether this key causes its children to be marked generated."""
        return key in self.generated_parents
    
    def set_generated_parent(self, key: str) -> None:
        """Sets the key to cause its children to be marked generated."""
        self.generated_parents.add(key)
    
    def get_timestamps(self, keys: List[str]) -> List[float]:
        """Returns the combined timestamps from a set of fields."""
        keys = [key for key in keys if key in self.fields]
        
        if len(keys) > 1:
            # Get new data
            all_timestamps = []
            for key in keys:
                all_timestamps.extend(self.fields[key].get_timestamps())
            output = sorted(list(set(all_timestamps)))
            
        elif len(keys) == 1:
            output = self.fields[keys[0]].get_timestamps()
        else:
            output = []
        
        return output
    
    def get_timestamp_range(self) -> Tuple[float, float]:
        """Returns the range of timestamps across all fields."""
        if self.timestamp_range is None:
            return self.DEFAULT_TIMESTAMP_RANGE
        return self.timestamp_range
    
    def get_last_timestamp(self) -> float:
        """Returns the most recent timestamp across all fields."""
        timestamps = self.get_timestamps(self.get_field_keys())
        return timestamps[-1] if timestamps else 0.0
    
    # Data reading methods
    def get_range(self, key: str, start: float, end: float) -> Optional[LogValueSet]:
        """Reads a set of generic values from the field."""
        field = self.fields.get(key)
        return field.get_range(start, end) if field else None
    
    def get_raw(self, key: str, start: float, end: float) -> Optional[LogValueSetRaw]:
        """Reads a set of Raw values from the field."""
        field = self.fields.get(key)
        return field.get_raw(start, end) if field else None
    
    def get_boolean(self, key: str, start: float, end: float) -> Optional[LogValueSetBoolean]:
        """Reads a set of Boolean values from the field."""
        field = self.fields.get(key)
        return field.get_boolean(start, end) if field else None
    
    def get_number(self, key: str, start: float, end: float) -> Optional[LogValueSetNumber]:
        """Reads a set of Number values from the field."""
        field = self.fields.get(key)
        return field.get_number(start, end) if field else None
    
    def get_string(self, key: str, start: float, end: float) -> Optional[LogValueSetString]:
        """Reads a set of String values from the field."""
        field = self.fields.get(key)
        return field.get_string(start, end) if field else None

    def get_boolean_array(self, key: str, start: float, end: float) -> Optional[LogValueSetBooleanArray]:
        """Reads a set of BooleanArray values from the field."""
        field = self.fields.get(key)
        return field.get_boolean_array(start, end) if field else None

    def get_number_array(self, key: str, start: float, end: float) -> Optional[LogValueSetNumberArray]:
        """Reads a set of NumberArray values from the field."""
        field = self.fields.get(key)
        return field.get_number_array(start, end) if field else None

    def get_string_array(self, key: str, start: float, end: float) -> Optional[LogValueSetStringArray]:
        """Reads a set of StringArray values from the field."""
        field = self.fields.get(key)
        return field.get_string_array(start, end) if field else None

    # Data writing methods
    def put_raw(self, key: str, timestamp: float, value: bytes) -> None:
        """Writes a new Raw value to the field."""
        self.create_blank_field(key, LoggableType.RAW)
        self.fields[key].put_raw(timestamp, value)
        if self.fields[key].get_type() == LoggableType.RAW:
            self._process_timestamp(key, timestamp)
    
    def put_boolean(self, key: str, timestamp: float, value: bool) -> None:
        """Writes a new Boolean value to the field."""
        self.create_blank_field(key, LoggableType.BOOLEAN)
        self.fields[key].put_boolean(timestamp, value)
        if self.fields[key].get_type() == LoggableType.BOOLEAN:
            self._process_timestamp(key, timestamp)
    
    def put_number(self, key: str, timestamp: float, value: float) -> None:
        """Writes a new Number value to the field."""
        self.create_blank_field(key, LoggableType.NUMBER)
        self.fields[key].put_number(timestamp, value)
        if self.fields[key].get_type() == LoggableType.NUMBER:
            self._process_timestamp(key, timestamp)
    
    def put_string(self, key: str, timestamp: float, value: str) -> None:
        """Writes a new String value to the field."""
        self.create_blank_field(key, LoggableType.STRING)
        self.fields[key].put_string(timestamp, value)
        if self.fields[key].get_type() == LoggableType.STRING:
            self._process_timestamp(key, timestamp)
    
    def put_json(self, key: str, timestamp: float, value: str) -> None:
        """Writes a JSON-encoded string value to the field."""
        self.put_string(key, timestamp, value)
        if self.fields[key].get_type() == LoggableType.STRING:
            self.set_generated_parent(key)
            self.set_structured_type(key, "JSON")
            try:
                decoded_value = json.loads(value)
                self._put_unknown_struct(key, timestamp, decoded_value)
            except json.JSONDecodeError:
                pass
    
    def put_msgpack(self, key: str, timestamp: float, value: bytes) -> None:
        """Writes a msgpack-encoded raw value to the field."""
        self.put_raw(key, timestamp, value)
        if self.fields[key].get_type() == LoggableType.RAW:
            self.set_generated_parent(key)
            self.set_structured_type(key, "MessagePack")
            try:
                decoded_value = msgpack.unpackb(value, raw=False)
                self._put_unknown_struct(key, timestamp, decoded_value)
            except (msgpack.exceptions.ExtraData, ValueError):
                pass
    
    def put_struct(self, key: str, timestamp: float, value: bytes, schema_type: str, is_array: bool) -> None:
        """Writes a struct-encoded raw value to the field.
        
        The schema type should not include "struct:" or "[]"
        """
        self.put_raw(key, timestamp, value)
        if self.fields[key].get_type() == LoggableType.RAW:
            self.set_generated_parent(key)
            self.set_structured_type(key, schema_type + ("[]" if is_array else ""))
            decoded_data = None
            try:
                if is_array:
                    decoded_data = self.struct_decoder.decode_array(schema_type, value)
                else:
                    decoded_data = self.struct_decoder.decode(schema_type, value)
            except Exception:
                pass
            
            if decoded_data is not None:
                self._put_unknown_struct(key, timestamp, decoded_data["data"])
                for child_key, child_schema_type in decoded_data["schema_types"].items():
                    # Create the key so it can be dragged even though it doesn't have data
                    full_child_key = f"{key}/{child_key}"
                    self.create_blank_field(full_child_key, LoggableType.EMPTY)
                    self._process_timestamp(full_child_key, timestamp)
                    self.set_structured_type(full_child_key, child_schema_type)
    
    def _put_unknown_struct(self, key: str, timestamp: float, value: Any, 
                           allow_root_write: bool = False) -> None:
        """Writes an unknown array or object to the children of the field."""
        if value is None:
            return
        
        # Handle primitive types
        if isinstance(value, bool) and allow_root_write:
            self.put_boolean(key, timestamp, value)
            return
        elif isinstance(value, (int, float)) and allow_root_write:
            self.put_number(key, timestamp, float(value))
            return
        elif isinstance(value, str) and allow_root_write:
            self.put_string(key, timestamp, value)
            return
        elif isinstance(value, bytes) and allow_root_write:
            self.put_raw(key, timestamp, value)
            return
        
        # Handle arrays and objects
        if isinstance(value, list):
            # Check if all items are the same type for array handling
            if all(isinstance(item, bool) for item in value) and allow_root_write:
                self.put_boolean_array(key, timestamp, value)
            elif all(isinstance(item, (int, float)) for item in value) and allow_root_write:
                self.put_number_array(key, timestamp, [float(x) for x in value])
            elif all(isinstance(item, str) for item in value) and allow_root_write:
                self.put_string_array(key, timestamp, value)
            else:
                # Add array items as unknown structs
                self.put_number(f"{key}/length", timestamp, len(value))
                for i, item in enumerate(value):
                    self._put_unknown_struct(f"{key}/{i}", timestamp, item, True)
        elif isinstance(value, dict):
            # Add object entries
            for obj_key, obj_value in value.items():
                self._put_unknown_struct(f"{key}/{obj_key}", timestamp, obj_value, True)
    
    
