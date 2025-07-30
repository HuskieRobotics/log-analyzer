from enum import Enum
from typing import Dict, List, Optional, Union, Any, Set, Tuple, TypeVar, Generic
from dataclasses import dataclass, field
import json
import msgpack
import struct
from abc import ABC, abstractmethod
from StructDecoder import StructDecoder

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

# === Geometry Types ===

Translation2d = Tuple[float, float]  # meters (x, y)
Rotation2d = float  # radians

@dataclass
class Pose2d:
    translation: Translation2d
    rotation: Rotation2d

Translation3d = Tuple[float, float, float]  # meters (x, y, z)
Rotation3d = Tuple[float, float, float, float]  # quaternion (w, x, y, z)

@dataclass
class Pose3d:
    translation: Translation3d
    rotation: Rotation3d

# === Tree Structure ===

@dataclass
class LogFieldTree:
    """A layer of a recursive log tree."""
    full_key: Optional[str] = None
    children: Dict[str, 'LogFieldTree'] = field(default_factory=dict)

# === Decoders ===

class ProtoDecoder:
    """Manages decoding protobuf data."""
    
    def __init__(self):
        self.descriptors: List[Any] = []
    
    def add_descriptor(self, descriptor: bytes) -> None:
        """Add a protobuf descriptor."""
        # Implement protobuf descriptor parsing
        self.descriptors.append(descriptor)
    
    def decode(self, schema_type: str, data: bytes) -> Dict[str, Any]:
        """Decode protobuf data."""
        # Implement protobuf decoding logic
        return {"data": None, "schema_types": {}}
    
    @staticmethod
    def get_friendly_schema_type(schema_type: str) -> str:
        """Convert schema type to friendly name."""
        return schema_type.replace("wpi.proto.", "")
    
    def to_serialized(self) -> List[Any]:
        """Serialize decoder state."""
        return self.descriptors
    
    @classmethod
    def from_serialized(cls, descriptors: List[Any]) -> 'ProtoDecoder':
        """Create decoder from serialized state."""
        decoder = cls()
        decoder.descriptors = descriptors
        return decoder

class PhotonStructDecoder:
    """Manages decoding PhotonVision struct data."""
    
    def __init__(self):
        self.schemas: Dict[str, Any] = {}
    
    def add_schema(self, name: str, schema: bytes) -> None:
        """Add a PhotonStruct schema."""
        # Implement PhotonStruct schema parsing
        pass
    
    def decode(self, schema_type: str, data: bytes) -> Dict[str, Any]:
        """Decode PhotonStruct data."""
        # Implement PhotonStruct decoding logic
        return {"data": None, "schema_types": {}}

# === Log Field ===

class LogField:
    """A full log field that contains data."""
    
    def __init__(self, log_type: LoggableType):
        self.type = log_type
        self.data = LogValueSet()
        self.structured_type: Optional[str] = None
        self.wpilib_type: Optional[str] = None
        self.metadata_string: str = ""
        self.type_warning: bool = False
        self.striping_reference: bool = False
        self.get_range_cache: Dict[str, int] = {}
    
    def get_type(self) -> LoggableType:
        """Returns the constant field type."""
        return self.type
    
    def get_striping_reference(self) -> bool:
        """Returns the value of the striping reference."""
        return self.striping_reference
    
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
    
    def get_range(self, start: float, end: float, uuid: Optional[str] = None, 
                  start_offset: Optional[int] = None) -> LogValueSet:
        """Returns values in the specified timestamp range."""
        # Implement range retrieval with caching
        # This is a simplified version
        result_timestamps = []
        result_values = []
        
        for i, timestamp in enumerate(self.data.timestamps):
            if start <= timestamp <= end:
                result_timestamps.append(timestamp)
                result_values.append(self.data.values[i])
        
        result = LogValueSet()
        result.timestamps = result_timestamps
        result.values = result_values
        return result
    
    # Specific type getters
    def get_raw(self, start: float, end: float, uuid: Optional[str] = None, 
                start_offset: Optional[int] = None) -> Optional[LogValueSetRaw]:
        if self.type != LoggableType.RAW:
            return None
        range_data = self.get_range(start, end, uuid, start_offset)
        return LogValueSetRaw(range_data.timestamps, range_data.values)
    
    def get_boolean(self, start: float, end: float, uuid: Optional[str] = None, 
                    start_offset: Optional[int] = None) -> Optional[LogValueSetBoolean]:
        if self.type != LoggableType.BOOLEAN:
            return None
        range_data = self.get_range(start, end, uuid, start_offset)
        return LogValueSetBoolean(range_data.timestamps, range_data.values)
    
    def get_number(self, start: float, end: float, uuid: Optional[str] = None, 
                   start_offset: Optional[int] = None) -> Optional[LogValueSetNumber]:
        if self.type != LoggableType.NUMBER:
            return None
        range_data = self.get_range(start, end, uuid, start_offset)
        return LogValueSetNumber(range_data.timestamps, range_data.values)
    
    def get_string(self, start: float, end: float, uuid: Optional[str] = None, 
                   start_offset: Optional[int] = None) -> Optional[LogValueSetString]:
        if self.type != LoggableType.STRING:
            return None
        range_data = self.get_range(start, end, uuid, start_offset)
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
    
    def to_serialized(self) -> Dict[str, Any]:
        """Serialize field data."""
        return {
            "type": self.type.value,
            "timestamps": self.data.timestamps,
            "values": self.data.values,
            "structured_type": self.structured_type,
            "wpilib_type": self.wpilib_type,
            "metadata_string": self.metadata_string,
            "type_warning": self.type_warning
        }
    
    @classmethod
    def from_serialized(cls, data: Dict[str, Any]) -> 'LogField':
        """Create field from serialized data."""
        field = cls(LoggableType(data["type"]))
        field.data.timestamps = data["timestamps"]
        field.data.values = data["values"]
        field.structured_type = data.get("structured_type")
        field.wpilib_type = data.get("wpilib_type")
        field.metadata_string = data.get("metadata_string", "")
        field.type_warning = data.get("type_warning", False)
        return field

# === Queued Structure ===

@dataclass
class QueuedStructure:
    key: str
    timestamp: float
    value: bytes
    schema_type: str

# === Main Log Class ===

class Log:
    """Represents a collection of log fields."""
    
    def __init__(self, enable_timestamp_set_cache: bool = True):
        self.DEFAULT_TIMESTAMP_RANGE = (0.0, 10.0)
        self.msgpack_decoder = msgpack
        self.struct_decoder = StructDecoder()
        self.proto_decoder = ProtoDecoder()
        self.photon_decoder = PhotonStructDecoder()
        
        self.fields: Dict[str, LogField] = {}
        self.generated_parents: Set[str] = set()
        self.timestamp_range: Optional[Tuple[float, float]] = None
        self.enable_timestamp_set_cache = enable_timestamp_set_cache
        self.timestamp_set_cache: Dict[str, Dict[str, Any]] = {}
        self.changed_fields: Set[str] = set()
        
        self.queued_structs: List[QueuedStructure] = []
        self.queued_struct_arrays: List[QueuedStructure] = []
        self.queued_protos: List[QueuedStructure] = []
    
    def create_blank_field(self, key: str, log_type: LoggableType) -> None:
        """Checks if the field exists and registers it if necessary."""
        if key in self.fields:
            return
        self.fields[key] = LogField(log_type)
        self.changed_fields.add(key)
    
    def delete_field(self, key: str) -> None:
        """Removes all data for a field."""
        if key in self.fields:
            del self.fields[key]
            self.generated_parents.discard(key)
            self.changed_fields.discard(key)
            
            # Update timestamp cache
            for uuid, cache_values in self.timestamp_set_cache.items():
                if key in cache_values["keys"]:
                    cache_values["keys"] = [k for k in cache_values["keys"] if k != key]
                    # Recalculate timestamps
                    all_timestamps = []
                    for cache_key in cache_values["keys"]:
                        if cache_key in self.fields:
                            all_timestamps.extend(self.fields[cache_key].get_timestamps())
                    new_timestamps = sorted(list(set(all_timestamps)))
                    self.timestamp_set_cache[uuid]["timestamps"] = new_timestamps
                    self.timestamp_set_cache[uuid]["source_counts"] = [
                        len(self.fields[k].get_timestamps()) if k in self.fields else 0
                        for k in cache_values["keys"]
                    ]
    
    def clear_before_time(self, timestamp: float) -> None:
        """Clears all data before the provided timestamp."""
        if self.timestamp_range is None:
            self.timestamp_range = (timestamp, timestamp)
        elif self.timestamp_range[0] < timestamp:
            new_end = max(timestamp, self.timestamp_range[1])
            self.timestamp_range = (timestamp, new_end)
        
        # Clear timestamp caches
        for cache in self.timestamp_set_cache.values():
            timestamps = cache["timestamps"]
            while len(timestamps) >= 2 and timestamps[1] <= timestamp:
                timestamps.pop(0)
            if timestamps and timestamps[0] < timestamp:
                timestamps[0] = timestamp
        
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
        
        if self.enable_timestamp_set_cache:
            for cache in self.timestamp_set_cache.values():
                if key in cache["keys"] and timestamp not in cache["timestamps"]:
                    # Insert timestamp in sorted order
                    timestamps = cache["timestamps"]
                    insert_index = len(timestamps)
                    for i, ts in enumerate(timestamps):
                        if ts > timestamp:
                            insert_index = i
                            break
                    timestamps.insert(insert_index, timestamp)
    
    def get_changed_fields(self) -> Set[str]:
        """Returns the set of fields that have changed since the last call."""
        output = self.changed_fields
        self.changed_fields = set()
        return output
    
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
        self.changed_fields.add(key)
    
    def get_type(self, key: str) -> Optional[LoggableType]:
        """Returns the constant field type."""
        field = self.fields.get(key)
        return field.get_type() if field else None
    
    def get_striping_reference(self, key: str) -> bool:
        """Returns a boolean that toggles when a value is removed from the field."""
        field = self.fields.get(key)
        return field.get_striping_reference() if field else False
    
    def get_structured_type(self, key: str) -> Optional[str]:
        """Returns the structured type string for a field."""
        field = self.fields.get(key)
        return field.structured_type if field else None
    
    def set_structured_type(self, key: str, type_str: Optional[str]) -> None:
        """Sets the structured type string for a field."""
        field = self.fields.get(key)
        if field:
            field.structured_type = type_str
            self.changed_fields.add(key)
    
    def get_wpilib_type(self, key: str) -> Optional[str]:
        """Returns the WPILib type string for a field."""
        field = self.fields.get(key)
        return field.wpilib_type if field else None
    
    def set_wpilib_type(self, key: str, type_str: str) -> None:
        """Sets the WPILib type string for a field."""
        field = self.fields.get(key)
        if field:
            field.wpilib_type = type_str
            self.changed_fields.add(key)
    
    def get_metadata_string(self, key: str) -> str:
        """Returns the metadata string for a field."""
        field = self.fields.get(key)
        return field.metadata_string if field else ""
    
    def set_metadata_string(self, key: str, metadata: str) -> None:
        """Sets the WPILib metadata string for a field."""
        field = self.fields.get(key)
        if field:
            field.metadata_string = metadata
            self.changed_fields.add(key)
    
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
    
    def get_timestamps(self, keys: List[str], uuid: Optional[str] = None) -> List[float]:
        """Returns the combined timestamps from a set of fields."""
        keys = [key for key in keys if key in self.fields]
        
        if len(keys) > 1:
            # Check cache if available
            if (uuid and self.enable_timestamp_set_cache and 
                uuid in self.timestamp_set_cache):
                cache = self.timestamp_set_cache[uuid]
                if (cache["keys"] == keys and 
                    cache["source_counts"] == [len(self.fields[k].get_timestamps()) for k in keys]):
                    return cache["timestamps"].copy()
            
            # Get new data
            all_timestamps = []
            for key in keys:
                all_timestamps.extend(self.fields[key].get_timestamps())
            output = sorted(list(set(all_timestamps)))
            
            # Save to cache
            if uuid and self.enable_timestamp_set_cache:
                self.timestamp_set_cache[uuid] = {
                    "keys": keys,
                    "timestamps": output,
                    "source_counts": [len(self.fields[k].get_timestamps()) for k in keys]
                }
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
    
    def get_field_tree(self, include_generated: bool = True, 
                       prefix: str = "") -> Dict[str, LogFieldTree]:
        """Organizes the fields into a tree structure."""
        root: Dict[str, LogFieldTree] = {}
        
        for key in self.fields.keys():
            if not key.startswith(prefix):
                continue
            if not include_generated and self.is_generated(key):
                continue
            
            position = LogFieldTree(children=root)
            relative_key = key[len(prefix):]
            if relative_key.startswith("/"):
                relative_key = relative_key[1:]
            
            path_parts = relative_key.split("/")
            for part in path_parts:
                if not part:
                    continue
                if part not in position.children:
                    position.children[part] = LogFieldTree()
                position = position.children[part]
            
            position.full_key = relative_key
        
        return root
    
    # Data reading methods
    def get_range(self, key: str, start: float, end: float, 
                  uuid: Optional[str] = None, start_offset: Optional[int] = None) -> Optional[LogValueSet]:
        """Reads a set of generic values from the field."""
        field = self.fields.get(key)
        return field.get_range(start, end, uuid, start_offset) if field else None
    
    def get_raw(self, key: str, start: float, end: float, 
                uuid: Optional[str] = None, start_offset: Optional[int] = None) -> Optional[LogValueSetRaw]:
        """Reads a set of Raw values from the field."""
        field = self.fields.get(key)
        return field.get_raw(start, end, uuid, start_offset) if field else None
    
    def get_boolean(self, key: str, start: float, end: float, 
                    uuid: Optional[str] = None, start_offset: Optional[int] = None) -> Optional[LogValueSetBoolean]:
        """Reads a set of Boolean values from the field."""
        field = self.fields.get(key)
        return field.get_boolean(start, end, uuid, start_offset) if field else None
    
    def get_number(self, key: str, start: float, end: float, 
                   uuid: Optional[str] = None, start_offset: Optional[int] = None) -> Optional[LogValueSetNumber]:
        """Reads a set of Number values from the field."""
        field = self.fields.get(key)
        return field.get_number(start, end, uuid, start_offset) if field else None
    
    def get_string(self, key: str, start: float, end: float, 
                   uuid: Optional[str] = None, start_offset: Optional[int] = None) -> Optional[LogValueSetString]:
        """Reads a set of String values from the field."""
        field = self.fields.get(key)
        return field.get_string(start, end, uuid, start_offset) if field else None
    
    def get_boolean_array(self, key: str, start: float, end: float, 
                          uuid: Optional[str] = None, start_offset: Optional[int] = None) -> Optional[LogValueSetBooleanArray]:
        """Reads a set of BooleanArray values from the field."""
        field = self.fields.get(key)
        return field.get_boolean_array(start, end, uuid, start_offset) if field else None
    
    def get_number_array(self, key: str, start: float, end: float, 
                         uuid: Optional[str] = None, start_offset: Optional[int] = None) -> Optional[LogValueSetNumberArray]:
        """Reads a set of NumberArray values from the field."""
        field = self.fields.get(key)
        return field.get_number_array(start, end, uuid, start_offset) if field else None
    
    def get_string_array(self, key: str, start: float, end: float, 
                         uuid: Optional[str] = None, start_offset: Optional[int] = None) -> Optional[LogValueSetStringArray]:
        """Reads a set of StringArray values from the field."""
        field = self.fields.get(key)
        return field.get_string_array(start, end, uuid, start_offset) if field else None
    
    # Data writing methods
    def put_raw(self, key: str, timestamp: float, value: bytes) -> None:
        """Writes a new Raw value to the field."""
        self.create_blank_field(key, LoggableType.RAW)
        self.fields[key].put_raw(timestamp, value)
        self.changed_fields.add(key)
        if self.fields[key].get_type() == LoggableType.RAW:
            self._process_timestamp(key, timestamp)
    
    def put_boolean(self, key: str, timestamp: float, value: bool) -> None:
        """Writes a new Boolean value to the field."""
        self.create_blank_field(key, LoggableType.BOOLEAN)
        self.fields[key].put_boolean(timestamp, value)
        self.changed_fields.add(key)
        if self.fields[key].get_type() == LoggableType.BOOLEAN:
            self._process_timestamp(key, timestamp)
    
    def put_number(self, key: str, timestamp: float, value: float) -> None:
        """Writes a new Number value to the field."""
        self.create_blank_field(key, LoggableType.NUMBER)
        self.fields[key].put_number(timestamp, value)
        self.changed_fields.add(key)
        if self.fields[key].get_type() == LoggableType.NUMBER:
            self._process_timestamp(key, timestamp)
    
    def put_string(self, key: str, timestamp: float, value: str) -> None:
        """Writes a new String value to the field."""
        self.create_blank_field(key, LoggableType.STRING)
        self.fields[key].put_string(timestamp, value)
        self.changed_fields.add(key)
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
            else:
                queue = self.queued_struct_arrays if is_array else self.queued_structs
                queue.append(QueuedStructure(
                    key=key,
                    timestamp=timestamp,
                    value=value,
                    schema_type=schema_type
                ))
    
    def put_pose(self, key: str, timestamp: float, pose: Pose2d) -> None:
        """Writes a pose with the 'Pose2d' structured type."""
        translation_key = f"{key}/translation"
        rotation_key = f"{key}/rotation"
        
        self.put_number(f"{translation_key}/x", timestamp, pose.translation[0])
        self.put_number(f"{translation_key}/y", timestamp, pose.translation[1])
        self.put_number(f"{rotation_key}/value", timestamp, pose.rotation)
        
        if key not in self.fields:
            self.create_blank_field(key, LoggableType.EMPTY)
            self.set_structured_type(key, "Pose2d")
            self.set_generated_parent(key)
            self._process_timestamp(key, timestamp)
        
        if translation_key not in self.fields:
            self.create_blank_field(translation_key, LoggableType.EMPTY)
            self.set_structured_type(translation_key, "Translation2d")
            self._process_timestamp(translation_key, timestamp)
        
        if rotation_key not in self.fields:
            self.create_blank_field(rotation_key, LoggableType.EMPTY)
            self.set_structured_type(rotation_key, "Rotation2d")
            self._process_timestamp(rotation_key, timestamp)
    
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
    
    def to_serialized(self) -> Dict[str, Any]:
        """Returns a serialized version of the data from this log."""
        result = {
            "fields": {},
            "generated_parents": list(self.generated_parents),
            "timestamp_range": self.timestamp_range,
            "struct_decoder": self.struct_decoder.to_serialized(),
            "proto_decoder": self.proto_decoder.to_serialized(),
            "queued_structs": [
                {
                    "key": qs.key,
                    "timestamp": qs.timestamp,
                    "value": qs.value,
                    "schema_type": qs.schema_type
                } for qs in self.queued_structs
            ],
            "queued_struct_arrays": [
                {
                    "key": qs.key,
                    "timestamp": qs.timestamp,
                    "value": qs.value,
                    "schema_type": qs.schema_type
                } for qs in self.queued_struct_arrays
            ],
            "queued_protos": [
                {
                    "key": qs.key,
                    "timestamp": qs.timestamp,
                    "value": qs.value,
                    "schema_type": qs.schema_type
                } for qs in self.queued_protos
            ]
        }
        
        for key, field in self.fields.items():
            result["fields"][key] = field.to_serialized()
        
        return result
    
    @classmethod
    def from_serialized(cls, serialized_data: Dict[str, Any]) -> 'Log':
        """Creates a new log based on the data from to_serialized()."""
        log = cls()
        
        # Restore fields
        for key, field_data in serialized_data["fields"].items():
            log.fields[key] = LogField.from_serialized(field_data)
        
        # Restore other properties
        log.generated_parents = set(serialized_data["generated_parents"])
        log.timestamp_range = serialized_data["timestamp_range"]
        log.struct_decoder = StructDecoder.from_serialized(serialized_data["struct_decoder"])
        log.proto_decoder = ProtoDecoder.from_serialized(serialized_data["proto_decoder"])
        
        # Restore queued structures
        log.queued_structs = [
            QueuedStructure(
                key=qs["key"],
                timestamp=qs["timestamp"],
                value=qs["value"],
                schema_type=qs["schema_type"]
            ) for qs in serialized_data.get("queued_structs", [])
        ]
        log.queued_struct_arrays = [
            QueuedStructure(
                key=qs["key"],
                timestamp=qs["timestamp"],
                value=qs["value"],
                schema_type=qs["schema_type"]
            ) for qs in serialized_data.get("queued_struct_arrays", [])
        ]
        log.queued_protos = [
            QueuedStructure(
                key=qs["key"],
                timestamp=qs["timestamp"],
                value=qs["value"],
                schema_type=qs["schema_type"]
            ) for qs in serialized_data.get("queued_protos", [])
        ]
        
        return log

# === Usage Example ===

if __name__ == "__main__":
    # Create a new log
    log = Log()
    
    # Add some data
    log.put_number("/robot/velocity", 1.0, 5.2)
    log.put_number("/robot/velocity", 1.1, 5.8)
    log.put_boolean("/robot/enabled", 1.0, True)
    log.put_string("/robot/mode", 1.0, "teleop")
    
    # Add a pose
    pose = Pose2d(translation=(1.5, 2.3), rotation=0.785)
    log.put_pose("/robot/pose", 1.0, pose)
    
    # Get timestamps for specific fields
    timestamps = log.get_timestamps(["/robot/velocity", "/robot/enabled"])
    print(f"Combined timestamps: {timestamps}")
    
    # Get field tree
    tree = log.get_field_tree()
    print(f"Field tree keys: {list(tree.keys())}")
    
    # Serialize and deserialize
    serialized = log.to_serialized()
    restored_log = Log.from_serialized(serialized)
    print(f"Restored log has {len(restored_log.get_field_keys())} fields")