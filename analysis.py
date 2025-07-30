#! /usr/bin/env python3
# Copyright (c) FIRST and other WPILib contributors.
# Open Source Software; you can modify and/or share it under the terms of
# the WPILib BSD license file in the root directory of this project.

import json
import mmap
import os
import sys
from datetime import datetime
from datalog import DataLogReader, DataLogRecord, StartRecordData
from Log import Log, LoggableType

# Constants for structured types
STRUCT_PREFIX = "struct:"
PHOTON_PREFIX = "photon:"
PROTO_PREFIX = "proto:"


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
            elif calc_type == 'count':
                pass # Count is handled separately, not printed here
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


def print_values_and_calculations(values, calculations, context_prefix="", no_values_message=None):
    """Print captured values and perform calculations on them.
    
    Args:
        values: List of captured values (numbers)
        calculations: List of calculation configs from analysis config
        context_prefix: Prefix for output (e.g., "Aggregated " for aggregated results)
        no_values_message: Custom message when no values found
    """
    if values:
        if context_prefix == "":
            print(f"  Total values captured in this file: {len(values)}")
            print(f"  Values: {values}")
        else:
            print(f"  Total values captured across all files: {len(values)}")
            print(f"  All values: {values}")
        
        # Filter numeric values for calculations
        numeric_values = []
        for val in values:
            if isinstance(val, (int, float)):
                numeric_values.append(val)
        
        if numeric_values:
            # Perform calculations
            for calc in calculations:
                calc_type = calc.get('type')
                calc_name = calc.get('name', f'{calc_type} calculation')
                
                if calc_type == 'average':
                    result = sum(numeric_values) / len(numeric_values)
                    print(f"  {context_prefix}{calc_name}: {result:.6f}")
                elif calc_type == 'max':
                    result = max(numeric_values)
                    print(f"  {context_prefix}{calc_name}: {result:.6f}")
                elif calc_type == 'min':
                    result = min(numeric_values)
                    print(f"  {context_prefix}{calc_name}: {result:.6f}")
                elif calc_type == 'count':
                    result = len(numeric_values)
                    print(f"  {context_prefix}{calc_name}: {result}")
                else:
                    print(f"  Unknown calculation type: {calc_type}")
        else:
            print(f"  No numeric values found for calculations")
    else:
        if no_values_message:
            print(f"  {no_values_message}")
        else:
            message = "No values captured for this analysis"
            if context_prefix:
                message += " across all files"
            else:
                message += " in this file"
            print(f"  {message}")


def analyze_value_records(log, value_analysis_configs):
    """
    Analyze file records and return captured values for each value analysis configuration.
    
    Args:
        log: The log object containing file records
        value_analysis_configs: List of value analysis configuration dictionaries
        
    Returns:
        Dictionary mapping analysis index to list of captured values
    """
    all_value_results = {}
    
    for analysis_idx, analysis in enumerate(value_analysis_configs):
        entry_name = analysis.get('entry')
        trigger_entry = analysis.get('triggerEntry')
        trigger_value = analysis.get('triggerValue')
        calculations = analysis.get('calculations', [])
        
        if not all([entry_name, trigger_entry, calculations]) or trigger_value is None:
            all_value_results[analysis_idx] = []
            continue
        
        # Find values when trigger condition is met
        captured_values = []

        # Get the field and timestamps for the start entry
        trigger_field = log.get_field(trigger_entry)
        field = log.get_field(entry_name)

        if not trigger_field or not field:
            print(f"  Skipping analysis {analysis_idx} due to missing fields: {trigger_entry} or {entry_name}")
            all_value_results[analysis_idx] = []
            continue

        if trigger_field.get_type() == LoggableType.STRING:
            trigger_log_values = trigger_field.get_string(0.0, log.get_last_timestamp())
        elif trigger_field.get_type() == LoggableType.BOOLEAN:
            trigger_log_values = trigger_field.get_boolean(0.0, log.get_last_timestamp())
        elif trigger_field.get_type() == LoggableType.NUMBER:
            trigger_log_values = trigger_field.get_number(0.0, log.get_last_timestamp())
        else:
            print(f"  Skipping analysis {analysis_idx} due to unsupported type for: {trigger_entry} of {trigger_field.get_type()}")
            all_value_results[analysis_idx] = []
            continue
            
        start_timestamp = 0.0

        for i, timestamp in enumerate(trigger_log_values.timestamps):
            if trigger_log_values.values[i] == trigger_value:
                end_timestamp = timestamp
                
                if field.get_type() == LoggableType.STRING:
                    log_values = field.get_string(start_timestamp, end_timestamp)
                elif field.get_type() == LoggableType.BOOLEAN:
                    log_values = field.get_boolean(start_timestamp, end_timestamp)
                elif field.get_type() == LoggableType.NUMBER:
                    log_values = field.get_number(start_timestamp, end_timestamp)
                else:
                    print(f"  Skipping analysis {analysis_idx} due to unsupported type for: {entry_name} of {field.get_type()}")
                    all_value_results[analysis_idx] = []
                    continue

                if len(log_values.values) > 0:
                    captured_values.append(log_values.values[-1])
                start_timestamp = timestamp  # Update start timestamp for next trigger match
        
        all_value_results[analysis_idx] = captured_values
    
    return all_value_results


def main():
    """Main analysis function."""
    if len(sys.argv) != 3:
        print("Usage: analysis.py <log_folder> <config_json_file>", file=sys.stderr)
        sys.exit(1)

    log_folder = sys.argv[1]
    if not os.path.isdir(log_folder):
        print(f"Error: {log_folder} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Load configuration from JSON file
    try:
        with open(sys.argv[2], 'r') as config_file:
            config = json.load(config_file)
            
            # Load filtering criteria
            filter_enabled = config.get('enabled', False)
            filter_fms_attached = config.get('fmsAttached', False)
            robot_mode = config.get('robotMode', 'both')  # 'auto', 'teleop', or 'both'
            
            # Load analysis configuration
            time_analysis_configs = config.get('timeAnalysis', [])
            value_analysis_configs = config.get('valueAnalysis', [])
            
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading config file: {e}", file=sys.stderr)
        sys.exit(1)
    
    target_entry_names = set([])

    # Always capture these entry names regardless of JSON configuration
    mandatory_entries = {"/DriverStation/Enabled", "/DriverStation/Autonomous", "/DriverStation/FMSAttached"}
    target_entry_names.update(mandatory_entries)
    
    # Add analysis entries to target entries to ensure they're captured
    for analysis in time_analysis_configs:
        start_entry = analysis.get('startEntry')
        end_entry = analysis.get('endEntry')
        if start_entry:
            target_entry_names.add(start_entry)
        if end_entry:
            target_entry_names.add(end_entry)
    
    # Add value analysis entries to target entries
    for analysis in value_analysis_configs:
        entry = analysis.get('entry')
        trigger_entry = analysis.get('triggerEntry')
        if entry:
            target_entry_names.add(entry)
        if trigger_entry:
            target_entry_names.add(trigger_entry)

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
    all_logs = []  # List to store records from all files
    all_files_time_differences = []  # List to store time differences for analysis
    aggregated_time_analysis_results = {}  # Dictionary to store aggregated time differences by analysis index
    aggregated_value_analysis_results = {}  # Dictionary to store aggregated values by analysis index
    
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
        
        with open(log_file_path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            reader = DataLogReader(mm)
            if not reader:
                print(f"  Warning: {os.path.basename(log_file_path)} is not a valid log file")
                return [], None, None, None

            entries = {}
            log = Log()
            
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

                    if ".schema" in entry.name:
                        # If the entry is a schema entry, we may want to capture it differently
                        log.struct_decoder.add_schema(entry.name.split("struct:")[1], record.getBytes())
                    
                    # Move this to another method and bring in the struct parser from the other branch

                    # Check if this record matches any target entry names and meets filtering criteria
                    if any(entry.name in name for name in mandatory_entries) or (any(entry.name in name for name in target_entry_names)and should_capture_record(driver_station_enabled, driver_station_autonomous, driver_station_fms_attached)):
                        key = entry.name
                        type_str = entry.type
                        
                        if type_str == "boolean":
                            log.put_boolean(key, timestamp, record.getBoolean())
                        elif type_str in ("int", "int64"):
                            log.put_number(key, timestamp, record.getInteger())
                        elif type_str == "float":
                            log.put_number(key, timestamp, record.getFloat())
                        elif type_str == "double":
                            log.put_number(key, timestamp, record.getDouble())
                        elif type_str == "string":
                            log.put_string(key, timestamp, record.getString())
                        elif type_str == "boolean[]":
                            log.put_boolean_array(key, timestamp, record.getBooleanArray())
                        elif type_str in ("int[]", "int64[]"):
                            log.put_number_array(key, timestamp, record.getIntegerArray())
                        elif type_str == "float[]":
                            log.put_number_array(key, timestamp, record.getFloatArray())
                        elif type_str == "double[]":
                            log.put_number_array(key, timestamp, record.getDoubleArray())
                        elif type_str == "string[]":
                            log.put_string_array(key, timestamp, record.getStringArray())
                        elif type_str == "json":
                            log.put_json(key, timestamp, record.getString())
                        elif type_str == "msgpack":
                            log.put_msgpack(key, timestamp, record.data)  # getRaw() equivalent
                        else:  # Default to raw
                            if type_str.startswith(STRUCT_PREFIX):
                                schema_type = type_str.split(STRUCT_PREFIX)[1]
                                if schema_type.endswith("[]"):
                                    log.put_struct(key, timestamp, record.data, schema_type[:-2], True)
                                else:
                                    log.put_struct(key, timestamp, record.data, schema_type, False)
                            elif type_str.startswith(PHOTON_PREFIX):
                                schema_type = type_str.split(PHOTON_PREFIX)[1]
                                log.put_photon_struct(key, timestamp, record.data, schema_type)
                            elif type_str.startswith(PROTO_PREFIX):
                                schema_type = type_str.split(PROTO_PREFIX)[1]
                                log.put_proto(key, timestamp, record.data, schema_type)
                            else:
                                log.put_raw(key, timestamp, record.data)
                                # Note: CustomSchemas functionality not implemented in Python version
                        
            return log

    def analyze_file_records(log, time_analysis_configs):
        """
        Analyze file records and return time differences for each analysis configuration.
        
        Args:
            log: List of (record, entry, timestamp) tuples
            time_analysis_configs: List of analysis configuration dictionaries
            
        Returns:
            Dictionary mapping analysis index to list of time differences
        """
        all_analysis_results = {}
        
        for analysis_idx, analysis in enumerate(time_analysis_configs):
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

            # Get the field and timestamps for the start entry
            start_field = log.get_field(start_entry)
            end_field = log.get_field(end_entry)

            if not start_field or not end_field:
                print(f"  Skipping analysis {analysis_idx} due to missing fields: {start_entry} or {end_entry}")
                all_analysis_results[analysis_idx] = []
                continue

            if start_field.get_type() == LoggableType.STRING:
                start_log_values = start_field.get_string(0.0, log.get_last_timestamp())
            elif start_field.get_type() == LoggableType.BOOLEAN:
                start_log_values = start_field.get_boolean(0.0, log.get_last_timestamp())
            elif start_field.get_type() == LoggableType.NUMBER:
                start_log_values = start_field.get_number(0.0, log.get_last_timestamp())
            else:
                print(f"  Skipping analysis {analysis_idx} due to unsupported type for: {start_entry} of {start_field.get_type()}")
                all_analysis_results[analysis_idx] = []
                continue
                
            start_timestamp = 0.0

            for i, timestamp in enumerate(start_log_values.timestamps):
                if start_log_values.values[i] == start_value:
                    start_timestamp = timestamp
                    next_timestamp = log.get_last_timestamp()
                    for i, timestamp in enumerate(start_log_values.timestamps):
                        if timestamp > start_timestamp and start_log_values.values[i] == start_value:
                            next_timestamp = timestamp
                            break

                    if end_field.get_type() == LoggableType.STRING:
                        end_log_values = end_field.get_string(start_timestamp, next_timestamp)
                    elif end_field.get_type() == LoggableType.BOOLEAN:
                        end_log_values = end_field.get_boolean(start_timestamp, next_timestamp)
                    elif end_field.get_type() == LoggableType.NUMBER:
                        end_log_values = end_field.get_number(start_timestamp, next_timestamp)
                    else:
                        print(f"  Skipping analysis {analysis_idx} due to unsupported type for: {end_entry} of {end_field.get_type()}")
                        all_analysis_results[analysis_idx] = []
                        continue

                    for i, timestamp in enumerate(end_log_values.timestamps):
                        if end_log_values.values[i] == end_value:
                            time_diff = timestamp - start_timestamp
                            time_differences.append(time_diff)
                            break
            
            all_analysis_results[analysis_idx] = time_differences
        
        return all_analysis_results

    # Process all log files
    for log_file in sorted(log_files):
        log = process_log_file(log_file)
        all_logs.append(log)

        # Analyze this file's records and aggregate for later cross-file analysis
        if time_analysis_configs:
            time_analysis_results = analyze_file_records(log, time_analysis_configs)

            # Aggregate results for later cross-file analysis (even empty results)
            for analysis_idx, time_differences in time_analysis_results.items():
                if analysis_idx not in aggregated_time_analysis_results:
                    aggregated_time_analysis_results[analysis_idx] = []
                aggregated_time_analysis_results[analysis_idx].append(time_differences)

        # Analyze value records and aggregate for later cross-file analysis  
        if value_analysis_configs:
            value_analysis_results = analyze_value_records(log, value_analysis_configs)
            
            # Aggregate results for later cross-file analysis (even empty results)
            for analysis_idx, values in value_analysis_results.items():
                if analysis_idx not in aggregated_value_analysis_results:
                    aggregated_value_analysis_results[analysis_idx] = []
                aggregated_value_analysis_results[analysis_idx].append(values)

        # Perform analysis calculations on individual file data
        if time_analysis_configs:
            print(f"\n=== TIME ANALYSIS RESULTS FOR {os.path.basename(log_file)} ===")
            
            for analysis_idx, analysis in enumerate(time_analysis_configs):
                start_entry = analysis.get('startEntry')
                start_value = analysis.get('startValue')
                end_entry = analysis.get('endEntry')
                end_value = analysis.get('endValue')
                calculations = analysis.get('calculations', [])
                
                if not all([start_entry, end_entry, calculations]):
                    print(f"Skipping incomplete analysis configuration")
                    continue
                
                print(f"\nAnalyzing: {start_entry} ({start_value}) -> {end_entry} ({end_value})")
                
                time_differences = time_analysis_results.get(analysis_idx, [])
                
                # Print found cycles and perform calculations for this file
                print_cycles_and_calculations(time_differences, calculations)

        # Perform value analysis calculations on individual file data
        if value_analysis_configs:
            print(f"\n=== VALUE ANALYSIS RESULTS FOR {os.path.basename(log_file)} ===")
            
            for analysis_idx, analysis in enumerate(value_analysis_configs):
                entry_name = analysis.get('entry')
                trigger_entry = analysis.get('triggerEntry')
                trigger_value = analysis.get('triggerValue')
                calculations = analysis.get('calculations', [])
                
                if not all([entry_name, trigger_entry, calculations]) or trigger_value is None:
                    print(f"Skipping incomplete value analysis configuration")
                    continue
                
                print(f"\nAnalyzing: {entry_name} when {trigger_entry} = {trigger_value}")
                
                values = value_analysis_results.get(analysis_idx, [])
                
                # Print captured values and perform calculations for this file
                print_values_and_calculations(values, calculations)

    # Perform aggregated analysis across all files
    if time_analysis_configs and aggregated_time_analysis_results:
        print(f"\n=== AGGREGATED TIME ANALYSIS RESULTS ACROSS ALL FILES ===")
        
        for analysis_idx, analysis in enumerate(time_analysis_configs):
            start_entry = analysis.get('startEntry')
            start_value = analysis.get('startValue')
            end_entry = analysis.get('endEntry')
            end_value = analysis.get('endValue')
            calculations = analysis.get('calculations', [])
            
            if not all([start_entry, end_entry, calculations]):
                print(f"Skipping incomplete analysis configuration")
                continue
            
            print(f"\nAggregated Analysis: {start_entry} ({start_value}) -> {end_entry} ({end_value})")
            
            all_time_differences_by_file = aggregated_time_analysis_results.get(analysis_idx, [])
            
            if all_time_differences_by_file:
                # Calculate per-file cycle statistics
                cycle_counts = [len(file_diffs) for file_diffs in all_time_differences_by_file]
                total_cycles = sum(cycle_counts)
                
                print(f"  Files processed: {len(all_time_differences_by_file)}")
                print(f"  Total cycles found across all files: {total_cycles}")
                
                if cycle_counts and "count" in [calc.get('type') for calc in calculations]:
                    avg_cycles_per_file = total_cycles / len(cycle_counts)
                    min_cycles_per_file = min(cycle_counts)
                    max_cycles_per_file = max(cycle_counts)
                    
                    print(f"  Average cycles per file: {avg_cycles_per_file:.2f}")
                    print(f"  Minimum cycles in any file: {min_cycles_per_file}")
                    print(f"  Maximum cycles in any file: {max_cycles_per_file}")
                
                # Flatten all time differences for overall calculations
                all_time_differences = [diff for file_diffs in all_time_differences_by_file for diff in file_diffs]

                # Print aggregated cycles summary and perform calculations
                print_cycles_and_calculations(all_time_differences, calculations, context_prefix="Aggregated ")
            else:
                print(f"  No complete cycles found for this analysis across all files")

    # Perform aggregated value analysis across all files
    if value_analysis_configs and aggregated_value_analysis_results:
        print(f"\n=== AGGREGATED VALUE ANALYSIS RESULTS ACROSS ALL FILES ===")
        
        for analysis_idx, analysis in enumerate(value_analysis_configs):
            entry_name = analysis.get('entry')
            trigger_entry = analysis.get('triggerEntry')
            trigger_value = analysis.get('triggerValue')
            calculations = analysis.get('calculations', [])
            
            if not all([entry_name, trigger_entry, calculations]) or trigger_value is None:
                print(f"Skipping incomplete value analysis configuration")
                continue
            
            print(f"\nAggregated Value Analysis: {entry_name} when {trigger_entry} = {trigger_value}")
            
            all_values_by_file = aggregated_value_analysis_results.get(analysis_idx, [])
            
            if all_values_by_file:
                # Calculate per-file value statistics
                value_counts = [len(file_values) for file_values in all_values_by_file]
                total_values = sum(value_counts)
                
                print(f"  Files processed: {len(all_values_by_file)}")
                print(f"  Total values captured across all files: {total_values}")
                
                if value_counts and "count" in [calc.get('type') for calc in calculations]:
                    avg_values_per_file = total_values / len(value_counts)
                    min_values_per_file = min(value_counts)
                    max_values_per_file = max(value_counts)
                    
                    print(f"  Average values per file: {avg_values_per_file:.2f}")
                    print(f"  Minimum values in any file: {min_values_per_file}")
                    print(f"  Maximum values in any file: {max_values_per_file}")
                
                # Flatten all values for overall calculations
                all_values = [val for file_values in all_values_by_file for val in file_values]
                
                if all_values:
                    print(f"  All captured values: {all_values}")
                    
                    # Filter numeric values for calculations
                    numeric_values = [val for val in all_values if isinstance(val, (int, float))]
                    
                    print_values_and_calculations(numeric_values, calculations)
                    
            else:
                print(f"  No values captured for this analysis across all files")

    # Print summary of captured records
    print(f"\n=== CAPTURED RECORDS SUMMARY ===")
    print(f"Total captured logs: {len(all_logs)}")
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
    
    if all_logs:
        print(f"\nCaptured logs by entry name:")
        entry_counts = {}
        for log in all_logs:
            for key in log.get_field_keys():
                if key not in entry_counts:
                    entry_counts[key] = 0
                entry_counts[key] += len(log.get_field(key).get_timestamps())

        for entry_name in sorted(entry_counts.keys()):
            print(f"  {entry_name}: {entry_counts[entry_name]} records")
    else:
        print("No records captured matching the specified entry names.")


if __name__ == "__main__":
    main()