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


def print_record_value(record: DataLogRecord, entry: StartRecordData, timestamp: float) -> None:
    """
    Print the value associated with a record based on the entry type.
    
    Args:
        record: The DataLogRecord containing the data
        entry: The StartRecordData containing type information
    """
    from datetime import datetime
    
    try:
        print(f"{entry.name} ({timestamp}):", end="")

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


def print_cycles_and_calculations(time_differences, calculations, context_prefix="", no_cycles_message=None):
        """Print cycle times and perform calculations on time differences.
        
        Args:
            time_differences: List of time differences (floats)
            calculations: List of calculation configs from analysis config
            context_prefix: Prefix for output (e.g., "Aggregated " for aggregated results)
            no_cycles_message: Custom message when no cycles found
        """
        if time_differences:
            if context_prefix == "":
                print(f"  Total cycles found in this file: {len(time_differences)}")
                for i, time_diff in enumerate(time_differences):
                    print(f"  Found cycle {i+1}: {time_diff:.6f}s")
            else:
                print(f"  Total cycles found across all files: {len(time_differences)}")
                print(f"  Individual cycle times: {[f'{t:.6f}s' for t in time_differences]}")
            
            # Perform calculations
            for calc in calculations:
                calc_type = calc.get('type')
                calc_name = calc.get('name', f'{calc_type} calculation')
                
                if calc_type == 'average':
                    result = sum(time_differences) / len(time_differences)
                    print(f"  {context_prefix}{calc_name}: {result:.6f} seconds")
                elif calc_type == 'max':
                    result = max(time_differences)
                    print(f"  {context_prefix}{calc_name}: {result:.6f} seconds")
                elif calc_type == 'min':
                    result = min(time_differences)
                    print(f"  {context_prefix}{calc_name}: {result:.6f} seconds")
                else:
                    print(f"  Unknown calculation type: {calc_type}")
        else:
            if no_cycles_message:
                print(f"  {no_cycles_message}")
            else:
                message = "No complete cycles found for this analysis"
                if context_prefix:
                    message += " across all files"
                else:
                    message += " in this file"
                print(f"  {message}")


if __name__ == "__main__":
    import json
    import mmap
    import os
    import sys
    from datetime import datetime

    if len(sys.argv) != 3:
        print("Usage: datalog.py <log_folder> <config_json_file>", file=sys.stderr)
        sys.exit(1)

    log_folder = sys.argv[1]
    if not os.path.isdir(log_folder):
        print(f"Error: {log_folder} is not a directory", file=sys.stderr)
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
            
            # Load analysis configuration
            analysis_configs = config.get('analysis', [])
            
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading config file: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Always capture these entry names regardless of JSON configuration
    mandatory_entries = {"/DriverStation/Enabled", "/DriverStation/Autonomous", "/DriverStation/FMSAttached"}
    target_entry_names.update(mandatory_entries)
    
    # Add analysis entries to target entries to ensure they're captured
    for analysis in analysis_configs:
        start_entry = analysis.get('startEntry')
        end_entry = analysis.get('endEntry')
        if start_entry:
            target_entry_names.add(start_entry)
        if end_entry:
            target_entry_names.add(end_entry)

    # Get list of files to process
    log_files = []
    for filename in os.listdir(log_folder):
        if filename.endswith('.wpilog'):
            log_files.append(os.path.join(log_folder, filename))
    
    if not log_files:
        print(f"No log files found in {log_folder}", file=sys.stderr)
        sys.exit(1)
    
    print(f"Found {len(log_files)} log files to process:")
    for log_file in sorted(log_files):
        print(f"  {os.path.basename(log_file)}")

    # Aggregated data across all files
    all_captured_records = []  # List to store records from all files
    all_files_time_differences = []  # List to store time differences for analysis
    aggregated_analysis_results = {}  # Dictionary to store aggregated time differences by analysis index
    
    def should_capture_record(driver_station_enabled, driver_station_autonomous, driver_station_fms_attached):
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

    def process_log_file(log_file_path):
        """Process a single log file and return captured records and final driver station state."""
        print(f"\nProcessing: {os.path.basename(log_file_path)}")
        
        try:
            with open(log_file_path, "rb") as f:
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                reader = DataLogReader(mm)
                if not reader:
                    print(f"  Warning: {os.path.basename(log_file_path)} is not a valid log file")
                    return [], None, None, None

                entries = {}
                captured_records = []  # List to store records matching target entry names
                
                # Track most recent values of DriverStation entries for filtering
                driver_station_enabled = None
                driver_station_autonomous = None
                driver_station_fms_attached = None
                
                for record in reader:
                    timestamp = record.timestamp / 1000000
                    if record.isStart():
                        try:
                            data = record.getStartData()
                            if data.entry in entries:
                                print("...DUPLICATE entry ID, overriding")
                            entries[data.entry] = data
                        except TypeError:
                            print("Start(INVALID)")
                    elif record.isFinish():
                        try:
                            entry = record.getFinishEntry()
                            if entry not in entries:
                                print("...ID not found")
                            else:
                                del entries[entry]
                        except TypeError:
                            print("Finish(INVALID)")
                    elif record.isSetMetadata():
                        try:
                            data = record.getSetMetadataData()
                            if data.entry not in entries:
                                print("...ID not found")
                        except TypeError:
                            print("SetMetadata(INVALID)")
                    elif record.isControl():
                        print("Unrecognized control record")
                    else:
                        entry = entries.get(record.entry)
                        if entry is None:
                            continue

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
                        if entry.name in mandatory_entries or (entry.name in target_entry_names and should_capture_record(driver_station_enabled, driver_station_autonomous, driver_station_fms_attached)):
                            captured_records.append((record, entry, timestamp))
                
                print(f"  Captured {len(captured_records)} records from {os.path.basename(log_file_path)}")
                return captured_records
                
        except Exception as e:
            print(f"  Error processing {os.path.basename(log_file_path)}: {e}")
            return []

    def analyze_file_records(file_records, analysis_configs):
        """
        Analyze file records and return time differences for each analysis configuration.
        
        Args:
            file_records: List of (record, entry, timestamp) tuples
            analysis_configs: List of analysis configuration dictionaries
            
        Returns:
            Dictionary mapping analysis index to list of time differences
        """
        all_analysis_results = {}
        
        for analysis_idx, analysis in enumerate(analysis_configs):
            start_entry = analysis.get('startEntry')
            start_value = analysis.get('startValue')
            end_entry = analysis.get('endEntry')
            end_value = analysis.get('endValue')
            calculations = analysis.get('calculations', [])
            
            if not all([start_entry, end_entry, calculations]):
                all_analysis_results[analysis_idx] = []
                continue
            
            # Find time differences between start and end events
            time_differences = []
            start_timestamp = None
            
            for record, entry, timestamp in file_records:
                try:
                    if (start_entry != end_entry and entry.name == start_entry) or (start_entry == end_entry and entry.name == start_entry and start_timestamp is None):
                        # Check if this record has the start value
                        if entry.type == "string" and record.getString() == start_value:
                            start_timestamp = timestamp
                        elif entry.type == "boolean" and record.getBoolean() == start_value:
                            start_timestamp = timestamp
                        elif entry.type == "int64" and record.getInteger() == start_value:
                            start_timestamp = timestamp
                        elif entry.type == "double" and record.getDouble() == start_value:
                            start_timestamp = timestamp
                            
                    elif entry.name == end_entry and start_timestamp is not None:
                        # Check if this record has the end value
                        end_matched = False
                        if entry.type == "string" and record.getString() == end_value:
                            end_matched = True
                        elif entry.type == "boolean" and record.getBoolean() == end_value:
                            end_matched = True
                        elif entry.type == "int64" and record.getInteger() == end_value:
                            end_matched = True
                        elif entry.type == "double" and record.getDouble() == end_value:
                            end_matched = True
                            
                        if end_matched:
                            time_diff = timestamp - start_timestamp
                            time_differences.append(time_diff)
                            if start_entry == end_entry:
                                # If start and end are the same, reset start_timestamp
                                start_timestamp = timestamp
                            else:
                                start_timestamp = None  # Reset for next cycle
                            
                except TypeError:
                    # Skip invalid records
                    continue
            
            all_analysis_results[analysis_idx] = time_differences
        
        return all_analysis_results

    # Process all log files
    for log_file in sorted(log_files):
        file_records = process_log_file(log_file)
        all_captured_records.extend(file_records)

        # Perform analysis calculations on individual file data
        if analysis_configs and file_records:
            print(f"\n=== ANALYSIS RESULTS FOR {os.path.basename(log_file)} ===")
            
            # Analyze this file's records using the function
            analysis_results = analyze_file_records(file_records, analysis_configs)
            
            # Aggregate results for later cross-file analysis
            for analysis_idx, time_differences in analysis_results.items():
                if analysis_idx not in aggregated_analysis_results:
                    aggregated_analysis_results[analysis_idx] = []
                aggregated_analysis_results[analysis_idx].extend(time_differences)
            
            for analysis_idx, analysis in enumerate(analysis_configs):
                start_entry = analysis.get('startEntry')
                start_value = analysis.get('startValue')
                end_entry = analysis.get('endEntry')
                end_value = analysis.get('endValue')
                calculations = analysis.get('calculations', [])
                
                if not all([start_entry, end_entry, calculations]):
                    print(f"Skipping incomplete analysis configuration")
                    continue
                
                print(f"\nAnalyzing: {start_entry} ({start_value}) -> {end_entry} ({end_value})")
                
                time_differences = analysis_results.get(analysis_idx, [])
                
                # Print found cycles and perform calculations for this file
                print_cycles_and_calculations(time_differences, calculations)

    # Perform aggregated analysis across all files
    if analysis_configs and aggregated_analysis_results:
        print(f"\n=== AGGREGATED ANALYSIS RESULTS ACROSS ALL FILES ===")
        
        for analysis_idx, analysis in enumerate(analysis_configs):
            start_entry = analysis.get('startEntry')
            start_value = analysis.get('startValue')
            end_entry = analysis.get('endEntry')
            end_value = analysis.get('endValue')
            calculations = analysis.get('calculations', [])
            
            if not all([start_entry, end_entry, calculations]):
                print(f"Skipping incomplete analysis configuration")
                continue
            
            print(f"\nAggregated Analysis: {start_entry} ({start_value}) -> {end_entry} ({end_value})")
            
            all_time_differences = aggregated_analysis_results.get(analysis_idx, [])
            
            # Print aggregated cycles summary and perform calculations
            print_cycles_and_calculations(all_time_differences, calculations, "Aggregated ")

    # Print summary of captured records
    print(f"\n=== CAPTURED RECORDS SUMMARY ===")
    print(f"Total captured records: {len(all_captured_records)}")
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
    
    if all_captured_records:
        print(f"\nCaptured records by entry name:")
        entry_counts = {}
        for record, entry, timestamp in all_captured_records:
            if entry.name not in entry_counts:
                entry_counts[entry.name] = 0
            entry_counts[entry.name] += 1
        
        for entry_name in sorted(entry_counts.keys()):
            print(f"  {entry_name}: {entry_counts[entry_name]} records")
    else:
        print("No records captured matching the specified entry names.")

