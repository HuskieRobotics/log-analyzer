# Copyright (c) 2021-2025 Littleton Robotics
# http://github.com/Mechanical-Advantage
#
# Use of this source code is governed by a BSD
# license that can be found in the LICENSE file
# at the root directory of this project.

"""Class to manage decoding WPILib structs.

Specification: https://github.com/wpilibsuite/allwpilib/blob/main/wpiutil/doc/struct.adoc

Converted from the TypeScript version at:
https://github.com/Mechanical-Advantage/AdvantageScope/blob/main/src/shared/log/StructDecoder.ts
"""

import struct
from enum import Enum
from typing import Dict, List, Tuple, Any, Optional, Union
import math

__all__ = ["StructDecoder"]


class ValueType(Enum):
    BOOL = "bool"
    CHAR = "char"
    INT8 = "int8"
    INT16 = "int16"
    INT32 = "int32"
    INT64 = "int64"
    UINT8 = "uint8"
    UINT16 = "uint16"
    UINT32 = "uint32"
    UINT64 = "uint64"
    FLOAT = "float"
    FLOAT32 = "float32"
    DOUBLE = "double"
    FLOAT64 = "float64"


VALID_TYPE_STRINGS = [vt.value for vt in ValueType]

BITFIELD_VALID_TYPES = [
    ValueType.BOOL,
    ValueType.INT8,
    ValueType.INT16,
    ValueType.INT32,
    ValueType.INT64,
    ValueType.UINT8,
    ValueType.UINT16,
    ValueType.UINT32,
    ValueType.UINT64
]

VALUE_TYPE_MAX_BITS = {
    ValueType.BOOL: 8,
    ValueType.CHAR: 8,
    ValueType.INT8: 8,
    ValueType.INT16: 16,
    ValueType.INT32: 32,
    ValueType.INT64: 64,
    ValueType.UINT8: 8,
    ValueType.UINT16: 16,
    ValueType.UINT32: 32,
    ValueType.UINT64: 64,
    ValueType.FLOAT: 32,
    ValueType.FLOAT32: 32,
    ValueType.DOUBLE: 64,
    ValueType.FLOAT64: 64
}


class ValueSchema:
    def __init__(self, name: str, type_: Union[ValueType, str], enum: Optional[Dict[int, str]] = None,
                 bitfield_width: Optional[int] = None, array_length: Optional[int] = None,
                 bit_range: Tuple[int, int] = (0, 0)):
        self.name = name
        self.type = type_
        self.enum = enum
        self.bitfield_width = bitfield_width
        self.array_length = array_length
        self.bit_range = bit_range


class Schema:
    def __init__(self, length: int, value_schemas: List[ValueSchema]):
        self.length = length
        self.value_schemas = value_schemas


class StructDecoder:
    def __init__(self):
        self.schema_strings: Dict[str, str] = {}
        self.schemas: Dict[str, Schema] = {}

    def add_schema(self, name: str, schema: bytes) -> None:
        schema_str = schema.decode('utf-8')
        if name in self.schema_strings:
            return
        self.schema_strings[name] = schema_str

        # Try to compile any missing schemas
        while True:
            compile_success = False
            for schema_name in self.schema_strings.keys():
                if schema_name not in self.schemas:
                    success = self._compile_schema(schema_name, self.schema_strings[schema_name])
                    compile_success = compile_success or success
            if not compile_success:
                # Nothing was compiled (either everything was already
                # compiled or a schema dependency is missing)
                break

    def _compile_schema(self, name: str, schema: str) -> bool:
        value_schema_strs = [s for s in schema.strip().split(";") if len(s) > 0]
        value_schemas: List[ValueSchema] = []
        
        for i in range(len(value_schema_strs)):
            schema_str = value_schema_strs[i]

            # Get enum data
            enum_data: Optional[Dict[int, str]] = None
            if schema_str.startswith("enum"):
                enum_data = {}
                enum_str_start = schema_str.index("{") + 1
                enum_str_end = schema_str.index("}")
                enum_str = "".join([char for char in schema_str[enum_str_start:enum_str_end] if char != " "])
                
                for pair_str in [s for s in enum_str.split(",") if len(s) > 0]:
                    pair = pair_str.split("=")
                    if len(pair) == 2 and pair[1].isdigit():
                        enum_data[int(pair[1])] = pair[0]
                
                schema_str = schema_str[enum_str_end + 1:]

            # Remove type from schema string
            schema_str_split = [s for s in schema_str.split(" ") if len(s) > 0]
            type_ = schema_str_split.pop(0)
            if type_ not in VALID_TYPE_STRINGS and type_ not in self.schemas:
                # Missing struct, can't finish compiling
                return False
            name_str = "".join(schema_str_split)

            # Get name and (bit length or array)
            bitfield_width: Optional[int] = None
            array_length: Optional[int] = None
            
            if ":" in name_str:
                # Bitfield
                split = name_str.split(":")
                field_name = split[0]
                bitfield_width = int(split[1])

                # Check for invalid bitfield
                value_type = ValueType(type_) if type_ in VALID_TYPE_STRINGS else None
                if value_type not in BITFIELD_VALID_TYPES:
                    continue
                if value_type == ValueType.BOOL and bitfield_width != 1:
                    continue
            elif "[" in name_str:
                # Array
                split = name_str.split("[")
                field_name = split[0]
                array_length = int(split[1].split("]")[0])
            else:
                # Normal value
                field_name = name_str

            # Create schema
            value_schemas.append(ValueSchema(
                name=field_name,
                type_=type_,
                enum=enum_data,
                bitfield_width=bitfield_width,
                array_length=array_length,
                bit_range=(0, 0)
            ))

        # Find bit positions
        bit_position = 0
        bitfield_position: Optional[int] = None
        bitfield_length: Optional[int] = None
        
        for i in range(len(value_schemas)):
            value_schema = value_schemas[i]
            if value_schema.type not in VALID_TYPE_STRINGS:
                # Referencing another struct
                if bitfield_position is not None and bitfield_length is not None:
                    bit_position += bitfield_length - bitfield_position
                bitfield_position = None
                bitfield_length = None
                length = self.schemas[value_schema.type].length
                if value_schema.array_length is not None:
                    length *= value_schema.array_length
                value_schema.bit_range = (bit_position, bit_position + length)
                bit_position += length
            elif value_schema.bitfield_width is None:
                # Normal or array value
                if bitfield_position is not None and bitfield_length is not None:
                    bit_position += bitfield_length - bitfield_position
                bitfield_position = None
                bitfield_length = None
                value_type = ValueType(value_schema.type)
                bit_length = VALUE_TYPE_MAX_BITS[value_type]
                if value_schema.array_length is not None:
                    bit_length *= value_schema.array_length
                value_schema.bit_range = (bit_position, bit_position + bit_length)
                bit_position += bit_length
            else:
                # Bitfield value
                value_type = ValueType(value_schema.type)
                type_length = VALUE_TYPE_MAX_BITS[value_type]
                value_bit_length = min(value_schema.bitfield_width, type_length)
                
                if (bitfield_position is None or
                    bitfield_length is None or
                    (value_type != ValueType.BOOL and bitfield_length != type_length) or
                    bitfield_position + value_bit_length > bitfield_length):
                    # Start new bitfield
                    if bitfield_position is not None and bitfield_length is not None:
                        bit_position += bitfield_length - bitfield_position
                    bitfield_position = 0
                    bitfield_length = type_length
                
                value_schema.bit_range = (bit_position, bit_position + value_bit_length)
                bitfield_position += value_bit_length
                bit_position += value_bit_length

        if bitfield_position is not None and bitfield_length is not None:
            bit_position += bitfield_length - bitfield_position

        # Save schema
        self.schemas[name] = Schema(
            length=bit_position,
            value_schemas=value_schemas
        )
        return True

    def decode(self, name: str, value: bytes, bit_length: Optional[int] = None) -> Dict[str, Any]:
        """Converts struct-encoded data with a known schema to an object."""
        if name not in self.schemas:
            raise ValueError("Schema not defined")
        if bit_length is None:
            bit_length = len(value) * 8
        
        output_data: Dict[str, Any] = {}
        output_schema_types: Dict[str, str] = {}
        schema = self.schemas[name]
        
        for value_schema in schema.value_schemas:
            value_array, value_bit_length = self._slice_bits(value, value_schema.bit_range)
            
            if value_schema.type in VALID_TYPE_STRINGS:
                value_type = ValueType(value_schema.type)
                if value_schema.array_length is None:
                    # Normal type
                    output_data[value_schema.name] = self._decode_value(value_array, value_type, value_schema.enum)
                else:
                    # Array type
                    decoded_values: List[Any] = []
                    item_length = (value_schema.bit_range[1] - value_schema.bit_range[0]) // value_schema.array_length
                    for position in range(0, value_bit_length, item_length):
                        item_value, _ = self._slice_bits(value_array, (position, position + item_length))
                        decoded_values.append(self._decode_value(item_value, value_type, value_schema.enum))
                    
                    if value_type == ValueType.CHAR:
                        output_data[value_schema.name] = "".join(decoded_values)
                    else:
                        output_data[value_schema.name] = decoded_values
            else:
                # Child struct
                is_array = value_schema.array_length is not None
                output_schema_types[value_schema.name] = value_schema.type + ("[]" if is_array else "")
                
                if is_array:
                    child = self.decode_array(value_schema.type, value_array, value_schema.array_length)
                else:
                    child = self.decode(value_schema.type, value_array, value_bit_length)
                
                output_data[value_schema.name] = child["data"]
                for field, schema_type in child["schema_types"].items():
                    output_schema_types[f"{value_schema.name}/{field}"] = schema_type

        return {
            "data": output_data,
            "schema_types": output_schema_types
        }

    def decode_array(self, name: str, value: bytes, array_length: Optional[int] = None) -> Dict[str, Any]:
        """Converts struct-encoded data with a known array schema to an object."""
        if name not in self.schemas:
            raise ValueError("Schema not defined")
        
        output_data: List[Any] = []
        output_schema_types: Dict[str, str] = {}
        schema_length = self.schemas[name].length // 8
        length = len(value) // schema_length if array_length is None else array_length
        
        for i in range(length):
            start_idx = i * schema_length
            end_idx = (i + 1) * schema_length
            decoded_data = self.decode(name, value[start_idx:end_idx])
            output_data.append(decoded_data["data"])
            
            for item_key, item_schema_type in decoded_data["schema_types"].items():
                output_schema_types[f"{i}/{item_key}"] = item_schema_type
            output_schema_types[str(i)] = name

        return {
            "data": output_data,
            "schema_types": output_schema_types
        }

    @staticmethod
    def _decode_value(value: bytes, value_type: ValueType, enum_data: Optional[Dict[int, str]]) -> Any:
        """Decode a bytes array as a single value based on the known type."""
        max_bits = VALUE_TYPE_MAX_BITS[value_type]
        padded_value = value + b'\x00' * (max_bits // 8 - len(value))
        
        if value_type == ValueType.BOOL:
            output = padded_value[0] > 0
        elif value_type == ValueType.CHAR:
            output = value.decode('utf-8', errors='ignore')
        elif value_type == ValueType.INT8:
            output = struct.unpack('<b', padded_value[:1])[0]
        elif value_type == ValueType.INT16:
            output = struct.unpack('<h', padded_value[:2])[0]
        elif value_type == ValueType.INT32:
            output = struct.unpack('<i', padded_value[:4])[0]
        elif value_type == ValueType.INT64:
            output = struct.unpack('<q', padded_value[:8])[0]
        elif value_type == ValueType.UINT8:
            output = struct.unpack('<B', padded_value[:1])[0]
        elif value_type == ValueType.UINT16:
            output = struct.unpack('<H', padded_value[:2])[0]
        elif value_type == ValueType.UINT32:
            output = struct.unpack('<I', padded_value[:4])[0]
        elif value_type == ValueType.UINT64:
            output = struct.unpack('<Q', padded_value[:8])[0]
        elif value_type in (ValueType.FLOAT, ValueType.FLOAT32):
            output = struct.unpack('<f', padded_value[:4])[0]
        elif value_type in (ValueType.DOUBLE, ValueType.FLOAT64):
            output = struct.unpack('<d', padded_value[:8])[0]
        else:
            raise ValueError(f"Unknown value type: {value_type}")

        if enum_data is not None and output in enum_data:
            output = enum_data[output]
        
        return output

    @staticmethod
    def _slice_bits(input_bytes: bytes, bit_range: Tuple[int, int]) -> Tuple[bytes, int]:
        """Extract a range of bits from a bytes array."""
        start_bit, end_bit = bit_range
        if start_bit % 8 == 0 and end_bit % 8 == 0:
            return input_bytes[start_bit // 8:end_bit // 8], end_bit - start_bit
        else:
            bool_array = StructDecoder._to_bool_array(input_bytes)
            sliced_bool_array = bool_array[start_bit:end_bit]
            return StructDecoder._to_bytes_array(sliced_bool_array), end_bit - start_bit

    @staticmethod
    def _to_bool_array(values: bytes) -> List[bool]:
        """Convert a bytes array to an array of booleans for each bit."""
        output: List[bool] = []
        for value in values:
            for shift in range(8):
                output.append(((1 << shift) & value) > 0)
        return output

    @staticmethod
    def _to_bytes_array(values: List[bool]) -> bytes:
        """Convert an array of booleans to a bytes array."""
        array = bytearray(math.ceil(len(values) / 8))
        for i, value in enumerate(values):
            if value:
                byte_idx = i // 8
                bit_idx = i % 8
                array[byte_idx] |= 1 << bit_idx
        return bytes(array)

    def to_serialized(self) -> Dict[str, Any]:
        """Returns a serialized version of the data from this decoder."""
        # Convert schemas to serializable format
        serialized_schemas = {}
        for name, schema in self.schemas.items():
            serialized_value_schemas = []
            for vs in schema.value_schemas:
                serialized_value_schemas.append({
                    'name': vs.name,
                    'type': vs.type.value if isinstance(vs.type, ValueType) else vs.type,
                    'enum': vs.enum,
                    'bitfield_width': vs.bitfield_width,
                    'array_length': vs.array_length,
                    'bit_range': vs.bit_range
                })
            serialized_schemas[name] = {
                'length': schema.length,
                'value_schemas': serialized_value_schemas
            }

        return {
            'schema_strings': self.schema_strings,
            'schemas': serialized_schemas
        }

    @classmethod
    def from_serialized(cls, serialized_data: Dict[str, Any]) -> 'StructDecoder':
        """Creates a new decoder based on the data from `to_serialized()`"""
        decoder = cls()
        decoder.schema_strings = serialized_data['schema_strings']
        
        # Reconstruct schemas from serialized format
        for name, schema_data in serialized_data['schemas'].items():
            value_schemas = []
            for vs_data in schema_data['value_schemas']:
                # Convert type back to ValueType or keep as string
                type_ = vs_data['type']
                if type_ in VALID_TYPE_STRINGS:
                    type_ = ValueType(type_)
                
                value_schemas.append(ValueSchema(
                    name=vs_data['name'],
                    type_=type_,
                    enum=vs_data['enum'],
                    bitfield_width=vs_data['bitfield_width'],
                    array_length=vs_data['array_length'],
                    bit_range=tuple(vs_data['bit_range'])
                ))
            
            decoder.schemas[name] = Schema(
                length=schema_data['length'],
                value_schemas=value_schemas
            )
        
        return decoder
    
    # print schema strings and schemas for debugging
    def __str__(self) -> str:
        schema_strings_str = "\n".join(f"{name}: {schema}" for name, schema in self.schema_strings.items())
        schemas_str = "\n".join(f"{name}: {schema.length} bits, {len(schema.value_schemas)} fields" for name, schema in self.schemas.items())
        return f"StructDecoder:\nSchema Strings:\n{schema_strings_str}\nSchemas:\n{schemas_str}"
    def __repr__(self) -> str:
        return self.__str__()