#! /usr/bin/env python3
# Copyright (c) FIRST and other WPILib contributors.
# Open Source Software; you can modify and/or share it under the terms of
# the WPILib BSD license file in the root directory of this project.

import array
import struct
from typing import List, SupportsBytes

import msgpack

__all__ = ["StartRecordData", "MetadataRecordData", "DataLogRecord", "DataLogReader"]

floatStruct = struct.Struct("<f")
doubleStruct = struct.Struct("<d")

kControlStart = 0
kControlFinish = 1
kControlSetMetadata = 2


class StartRecordData:
    """Data contained in a start control record as created by DataLog.start() when
    writing the log. This can be read by calling DataLogRecord.getStartData().

    entry: Entry ID; this will be used for this entry in future records.
    name: Entry name.
    type: Type of the stored data for this entry, as a string, e.g. "double".
    metadata: Initial metadata.
    """

    def __init__(self, entry: int, name: str, type: str, metadata: str):
        self.entry = entry
        self.name = name
        self.type = type
        self.metadata = metadata


class MetadataRecordData:
    """Data contained in a set metadata control record as created by
    DataLog.setMetadata(). This can be read by calling
    DataLogRecord.getSetMetadataData().

    entry: Entry ID.
    metadata: New metadata for the entry.
    """

    def __init__(self, entry: int, metadata: str):
        self.entry = entry
        self.metadata = metadata


class DataLogRecord:
    """A record in the data log. May represent either a control record
    (entry == 0) or a data record."""

    def __init__(self, entry: int, timestamp: int, data: SupportsBytes):
        self.entry = entry
        self.timestamp = timestamp
        self.data = data

    def isControl(self) -> bool:
        return self.entry == 0

    def _getControlType(self) -> int:
        return self.data[0]

    def isStart(self) -> bool:
        return (
            self.entry == 0
            and len(self.data) >= 17
            and self._getControlType() == kControlStart
        )

    def isFinish(self) -> bool:
        return (
            self.entry == 0
            and len(self.data) == 5
            and self._getControlType() == kControlFinish
        )

    def isSetMetadata(self) -> bool:
        return (
            self.entry == 0
            and len(self.data) >= 9
            and self._getControlType() == kControlSetMetadata
        )

    def getStartData(self) -> StartRecordData:
        if not self.isStart():
            raise TypeError("not a start record")
        entry = int.from_bytes(self.data[1:5], byteorder="little", signed=False)
        name, pos = self._readInnerString(5)
        type, pos = self._readInnerString(pos)
        metadata = self._readInnerString(pos)[0]
        return StartRecordData(entry, name, type, metadata)

    def getFinishEntry(self) -> int:
        if not self.isFinish():
            raise TypeError("not a finish record")
        return int.from_bytes(self.data[1:5], byteorder="little", signed=False)

    def getSetMetadataData(self) -> MetadataRecordData:
        if not self.isSetMetadata():
            raise TypeError("not a finish record")
        entry = int.from_bytes(self.data[1:5], byteorder="little", signed=False)
        metadata = self._readInnerString(5)[0]
        return MetadataRecordData(entry, metadata)

    def getBoolean(self) -> bool:
        if len(self.data) != 1:
            raise TypeError("not a boolean")
        return self.data[0] != 0

    def getInteger(self) -> int:
        if len(self.data) != 8:
            raise TypeError("not an integer")
        return int.from_bytes(self.data, byteorder="little", signed=True)

    def getFloat(self) -> float:
        if len(self.data) != 4:
            raise TypeError("not a float")
        return floatStruct.unpack(self.data)[0]

    def getDouble(self) -> float:
        if len(self.data) != 8:
            raise TypeError("not a double")
        return doubleStruct.unpack(self.data)[0]

    def getString(self) -> str:
        return str(self.data, encoding="utf-8")

    def getMsgPack(self):
        return msgpack.unpackb(self.data)

    def getBooleanArray(self) -> List[bool]:
        return [x != 0 for x in self.data]

    def getIntegerArray(self) -> array.array:
        if (len(self.data) % 8) != 0:
            raise TypeError("not an integer array")
        arr = array.array("l")
        arr.frombytes(self.data)
        return arr

    def getFloatArray(self) -> array.array:
        if (len(self.data) % 4) != 0:
            raise TypeError("not a float array")
        arr = array.array("f")
        arr.frombytes(self.data)
        return arr

    def getDoubleArray(self) -> array.array:
        if (len(self.data) % 8) != 0:
            raise TypeError("not a double array")
        arr = array.array("d")
        arr.frombytes(self.data)
        return arr

    def getStringArray(self) -> List[str]:
        size = int.from_bytes(self.data[:4], byteorder="little", signed=False)
        if size > ((len(self.data) - 4) / 4):
            raise TypeError("not a string array")
        arr = []
        pos = 4
        for _ in range(size):
            val, pos = self._readInnerString(pos)
            arr.append(val)
        return arr

    def _readInnerString(self, pos: int) -> tuple[str, int]:
        size = int.from_bytes(
            self.data[pos : pos + 4], byteorder="little", signed=False
        )
        end = pos + 4 + size
        if end > len(self.data):
            raise TypeError("invalid string size")
        return str(self.data[pos + 4 : end], encoding="utf-8"), end


class DataLogIterator:
    """DataLogReader iterator."""

    def __init__(self, buf: SupportsBytes, pos: int):
        self.buf = buf
        self.pos = pos

    def __iter__(self):
        return self

    def _readVarInt(self, pos: int, len: int) -> int:
        val = 0
        for i in range(len):
            val |= self.buf[pos + i] << (i * 8)
        return val

    def __next__(self) -> DataLogRecord:
        if len(self.buf) < (self.pos + 4):
            raise StopIteration
        entryLen = (self.buf[self.pos] & 0x3) + 1
        sizeLen = ((self.buf[self.pos] >> 2) & 0x3) + 1
        timestampLen = ((self.buf[self.pos] >> 4) & 0x7) + 1
        headerLen = 1 + entryLen + sizeLen + timestampLen
        if len(self.buf) < (self.pos + headerLen):
            raise StopIteration
        entry = self._readVarInt(self.pos + 1, entryLen)
        size = self._readVarInt(self.pos + 1 + entryLen, sizeLen)
        timestamp = self._readVarInt(self.pos + 1 + entryLen + sizeLen, timestampLen)
        if len(self.buf) < (self.pos + headerLen + size):
            raise StopIteration
        record = DataLogRecord(
            entry,
            timestamp,
            self.buf[self.pos + headerLen : self.pos + headerLen + size],
        )
        self.pos += headerLen + size
        return record


class DataLogReader:
    """Data log reader (reads logs written by the DataLog class)."""

    def __init__(self, buf: SupportsBytes):
        self.buf = buf

    def __bool__(self):
        return self.isValid()

    def isValid(self) -> bool:
        """Returns true if the data log is valid (e.g. has a valid header)."""
        return (
            len(self.buf) >= 12
            and self.buf[:6] == b"WPILOG"
            and self.getVersion() >= 0x0100
        )

    def getVersion(self) -> int:
        """Gets the data log version. Returns 0 if data log is invalid.

        @return Version number; most significant byte is major, least significant is
            minor (so version 1.0 will be 0x0100)"""
        if len(self.buf) < 12:
            return 0
        return int.from_bytes(self.buf[6:8], byteorder="little", signed=False)

    def getExtraHeader(self) -> str:
        """Gets the extra header data.

        @return Extra header data
        """
        if len(self.buf) < 12:
            return ""
        size = int.from_bytes(self.buf[8:12], byteorder="little", signed=False)
        return str(self.buf[12 : 12 + size], encoding="utf-8")

    def __iter__(self) -> DataLogIterator:
        extraHeaderSize = int.from_bytes(
            self.buf[8:12], byteorder="little", signed=False
        )
        return DataLogIterator(self.buf, 12 + extraHeaderSize)


def print_record_value(record: DataLogRecord, entry: StartRecordData) -> None:
    """
    Print the value associated with a record based on the entry type.
    
    Args:
        record: The DataLogRecord containing the data
        entry: The StartRecordData containing type information
    """
    from datetime import datetime
    
    try:
        # handle systemTime specially
        if entry.name == "systemTime" and entry.type == "int64":
            dt = datetime.fromtimestamp(record.getInteger() / 1000000)
            print("  {:%Y-%m-%d %H:%M:%S.%f}".format(dt))
            return

        if entry.type == "double":
            print(f"  {record.getDouble()}")
        elif entry.type == "int64":
            print(f"  {record.getInteger()}")
        elif entry.type in ("string", "json"):
            print(f"  '{record.getString()}'")
        elif entry.type == "msgpack":
            print(f"  '{record.getMsgPack()}'")
        elif entry.type == "boolean":
            print(f"  {record.getBoolean()}")
        elif entry.type == "boolean[]":
            arr = record.getBooleanArray()
            print(f"  {arr}")
        elif entry.type == "double[]":
            arr = record.getDoubleArray()
            print(f"  {arr}")
        elif entry.type == "float[]":
            arr = record.getFloatArray()
            print(f"  {arr}")
        elif entry.type == "int64[]":
            arr = record.getIntegerArray()
            print(f"  {arr}")
        elif entry.type == "string[]":
            arr = record.getStringArray()
            print(f"  {arr}")
    except TypeError:
        print("  invalid")


if __name__ == "__main__":
    import json
    import mmap
    import sys
    from datetime import datetime

    if len(sys.argv) != 3:
        print("Usage: datalog.py <log_file> <config_json_file>", file=sys.stderr)
        sys.exit(1)

    # Load configuration from JSON file
    try:
        with open(sys.argv[2], 'r') as config_file:
            config = json.load(config_file)
            target_entry_names = set(config.get('entryNames', []))
            
            # Load filtering criteria
            filter_enabled = config.get('enabled', False)
            filter_fms_attached = config.get('fmsAttached', False)
            robot_mode = config.get('robotMode', 'both')  # 'auto', 'teleop', or 'both'
            
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading config file: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Always capture these entry names regardless of JSON configuration
    mandatory_entries = {"/DriverStation/Enabled", "/DriverStation/Autonomous", "/DriverStation/FMSAttached"}
    target_entry_names.update(mandatory_entries)

    with open(sys.argv[1], "r") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        reader = DataLogReader(mm)
        if not reader:
            print("not a log file", file=sys.stderr)
            sys.exit(1)

        entries = {}
        captured_records = []  # List to store records matching target entry names
        
        # Track most recent values of DriverStation entries for filtering
        driver_station_enabled = None
        driver_station_autonomous = None
        driver_station_fms_attached = None
        
        def should_capture_record():
            """Check if records should be captured based on current DriverStation state."""

            # Check enabled filter
            if filter_enabled and driver_station_enabled is not None and not driver_station_enabled:
                return False
            
            # Check FMS attached filter
            if filter_fms_attached and driver_station_fms_attached is not None and not driver_station_fms_attached:
                return False
            
            # Check robot mode filter
            if robot_mode == "auto" and driver_station_autonomous is not None and not driver_station_autonomous:
                return False
            elif robot_mode == "teleop" and driver_station_autonomous is not None and driver_station_autonomous:
                return False
            # If robot_mode is "both" or any condition is not set, allow capture
            
            return True
        
        for record in reader:
            timestamp = record.timestamp / 1000000
            if record.isStart():
                try:
                    data = record.getStartData()
                    # print(
                    #     f"Start({data.entry}, name='{data.name}', type='{data.type}', metadata='{data.metadata}') [{timestamp}]"
                    # )
                    if data.entry in entries:
                        print("...DUPLICATE entry ID, overriding")
                    entries[data.entry] = data
                except TypeError:
                    print("Start(INVALID)")
            elif record.isFinish():
                try:
                    entry = record.getFinishEntry()
                    # print(f"Finish({entry}) [{timestamp}]")
                    if entry not in entries:
                        print("...ID not found")
                    else:
                        del entries[entry]
                except TypeError:
                    print("Finish(INVALID)")
            elif record.isSetMetadata():
                try:
                    data = record.getSetMetadataData()
                    # print(f"SetMetadata({data.entry}, '{data.metadata}') [{timestamp}]")
                    if data.entry not in entries:
                        print("...ID not found")
                except TypeError:
                    print("SetMetadata(INVALID)")
            elif record.isControl():
                print("Unrecognized control record")
            else:
                # print(f"Data({record.entry}, size={len(record.data)}) ", end="")
                entry = entries.get(record.entry)
                if entry is None:
                    print("<ID not found>")
                    continue
                # print(f"<name='{entry.name}', type='{entry.type}'> [{timestamp}]")

                # Update DriverStation state tracking for filtering
                try:
                    if entry.name == "/DriverStation/Enabled" and entry.type == "boolean":
                        driver_station_enabled = record.getBoolean()
                    elif entry.name == "/DriverStation/Autonomous" and entry.type == "boolean":
                        driver_station_autonomous = record.getBoolean()
                    elif entry.name == "/DriverStation/FMSAttached" and entry.type == "boolean":
                        driver_station_fms_attached = record.getBoolean()
                except TypeError:
                    # If we can't read the value, continue without updating state
                    pass

                # Check if this record matches any target entry names and meets filtering criteria
                if entry.name in mandatory_entries or (entry.name in target_entry_names and should_capture_record()):
                    captured_records.append((record, entry, timestamp))
                    print(f"  *** CAPTURED: {entry.name} ***")

                # print_record_value(record, entry)
            
        
        # Print summary of captured records
        print(f"\n=== CAPTURED RECORDS SUMMARY ===")
        print(f"Total captured records: {len(captured_records)}")
        print(f"Target entry names: {sorted(target_entry_names)}")
        print(f"Mandatory entries (always captured): {sorted(mandatory_entries)}")
        config_only_entries = target_entry_names - mandatory_entries
        if config_only_entries:
            print(f"Additional entries from JSON config: {sorted(config_only_entries)}")
        else:
            print("Additional entries from JSON config: None")
        
        # Print filtering criteria and final states
        print(f"\n=== FILTERING CRITERIA ===")
        print(f"Filter by enabled: {filter_enabled}")
        print(f"Filter by FMS attached: {filter_fms_attached}")
        print(f"Robot mode filter: {robot_mode}")
        
        print(f"\n=== FINAL DRIVERSTATION STATE ===")
        print(f"DriverStation/Enabled: {driver_station_enabled}")
        print(f"DriverStation/Autonomous: {driver_station_autonomous}")
        print(f"DriverStation/FMSAttached: {driver_station_fms_attached}")
        
        if captured_records:
            print(f"\nCaptured records by entry name:")
            entry_counts = {}
            for record, entry, timestamp in captured_records:
                if entry.name not in entry_counts:
                    entry_counts[entry.name] = 0
                entry_counts[entry.name] += 1
            
            for entry_name in sorted(entry_counts.keys()):
                print(f"  {entry_name}: {entry_counts[entry_name]} records")
        else:
            print("No records captured matching the specified entry names.")