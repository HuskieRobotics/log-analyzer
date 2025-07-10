# log-analyzer

A Python tool for analyzing WPILib DataLog (.wpilog) files with configurable filtering, time-based analysis, and value capture analysis.

## Features

- **Batch Processing**: Analyze multiple log files in a folder simultaneously
- **Configurable Filtering**: Filter records based on robot state (enabled/disabled, autonomous/teleop, FMS attached)
- **Time Analysis**: Calculate time differences between start/end events across cycles
- **Value Analysis**: Capture and analyze values when specific trigger conditions are met
- **Statistical Calculations**: Compute averages, minimums, maximums, and counts
- **Aggregated Reporting**: Per-file and cross-file analysis with detailed statistics

## Usage

```bash
python datalog.py <log_folder> <config_json_file>
```

### Arguments

- `<log_folder>`: Directory containing `.wpilog` files to analyze
- `<config_json_file>`: JSON configuration file specifying analysis parameters

### Example

```bash
python datalog.py ./logs config.json
```

## Configuration File (config.json)

The configuration file controls filtering criteria and defines analysis tasks. Here's the complete structure:

### Basic Structure

```json
{
    "enabled": true,
    "fmsAttached": true,
    "robotMode": "teleop",
    "timeAnalysis": [...],
    "valueAnalysis": [...]
}
```

### Filtering Options

| Property | Type | Values | Description |
|----------|------|--------|-------------|
| `enabled` | boolean | `true`/`false` | Only capture records when robot is enabled |
| `fmsAttached` | boolean | `true`/`false` | Only capture records when FMS is attached |
| `robotMode` | string | `"auto"`, `"teleop"`, `"both"` | Filter by robot mode |

### Time Analysis

Time analysis calculates the duration between start and end events. Multiple analysis configurations can be defined:

```json
"timeAnalysis": [{
    "startEntry": "/RealOutputs/Manipulator/State",
    "startValue": "SHOOT_CORAL",
    "endEntry": "/Manipulator/IsIndexerIRBlocked", 
    "endValue": false,
    "calculations": [
        {"type": "average", "name": "Average Shooting Time"},
        {"type": "max", "name": "Max Shooting Time"},
        {"type": "min", "name": "Min Shooting Time"}
    ]
}]
```

#### Time Analysis Properties

| Property | Type | Description |
|----------|------|-------------|
| `startEntry` | string | Log entry name that marks the start of a cycle |
| `startValue` | any | Value that triggers the start timestamp |
| `endEntry` | string | Log entry name that marks the end of a cycle |
| `endValue` | any | Value that triggers the end timestamp |
| `calculations` | array | List of calculations to perform on time differences |

### Value Analysis

Value analysis captures values from one entry when a trigger condition is met on another entry:

```json
"valueAnalysis": [{
    "entry": "/RealOutputs/DriveToReef/y velocity (reef frame)",
    "triggerEntry": "/Manipulator/IsIndexerIRBlocked",
    "triggerValue": false,
    "calculations": [
        {"type": "average", "name": "Average Y Velocity at Reef"},
        {"type": "max", "name": "Max Y Velocity at Reef"}, 
        {"type": "min", "name": "Min Y Velocity at Reef"}
    ]
}]
```

#### Value Analysis Properties

| Property | Type | Description |
|----------|------|-------------|
| `entry` | string | Log entry name to capture values from |
| `triggerEntry` | string | Log entry name to monitor for trigger condition |
| `triggerValue` | any | Value that triggers value capture |
| `calculations` | array | List of calculations to perform on captured values |

### Calculation Types

Both time and value analysis support these calculation types:

| Type | Description | Applies To |
|------|-------------|------------|
| `"average"` | Arithmetic mean | Time differences, numeric values |
| `"min"` | Minimum value | Time differences, numeric values |
| `"max"` | Maximum value | Time differences, numeric values |
| `"count"` | Count of items | Time differences, numeric values |

### Complete Example Configuration

```json
{
    "enabled": true,
    "fmsAttached": true,
    "robotMode": "teleop",
    "timeAnalysis": [{
        "startEntry": "/RealOutputs/Manipulator/State",
        "startValue": "SHOOT_CORAL",
        "endEntry": "/Manipulator/IsIndexerIRBlocked",
        "endValue": false,
        "calculations": [
            {"type": "average", "name": "Average Shooting Time"},
            {"type": "max", "name": "Max Shooting Time"},
            {"type": "min", "name": "Min Shooting Time"}
        ]
    },
    {
        "startEntry": "/RealOutputs/Manipulator/State", 
        "startValue": "WAITING_FOR_CORAL",
        "endEntry": "/RealOutputs/LEDS/state",
        "endValue": "SCORING",
        "calculations": [
            {"type": "average", "name": "Average Cycle Time"},
            {"type": "max", "name": "Max Cycle Time"},
            {"type": "min", "name": "Min Cycle Time"},
            {"type": "count", "name": "Cycle Count"}
        ]
    }],
    "valueAnalysis": [{
        "entry": "/RealOutputs/DriveToReef/y velocity (reef frame)",
        "triggerEntry": "/Manipulator/IsIndexerIRBlocked",
        "triggerValue": false,
        "calculations": [
            {"type": "average", "name": "Average Y Velocity at Reef"},
            {"type": "max", "name": "Max Y Velocity at Reef"},
            {"type": "min", "name": "Min Y Velocity at Reef"}
        ]
    }]
}
```

## Output Format

The tool produces structured output with several sections:

### 1. File Discovery
Lists all `.wpilog` files found in the specified directory.

### 2. Per-File Analysis
For each log file:
- **Time Analysis Results**: Shows cycles found and calculations for each time analysis
- **Value Analysis Results**: Shows captured values and calculations for each value analysis

### 3. Aggregated Analysis
Cross-file statistics including:
- **Time Analysis**: Combined results across all files with per-file statistics
- **Value Analysis**: Combined results across all files with per-file statistics
- File count, total cycles/values, min/max/average per file

### 4. Summary
- Total captured records
- Target entry names
- Filtering criteria applied

## Data Types Supported

The tool supports these WPILib data types:
- `boolean` / `boolean[]`
- `int64` / `int64[]`  
- `double` / `double[]`
- `float` / `float[]`
- `string` / `string[]`
- `json`
- `msgpack`

## Requirements

- Python 3.6+
- Standard library modules: `json`, `mmap`, `os`, `sys`, `datetime`
- External dependency: `msgpack`

Install msgpack:
```bash
pip install msgpack
```

## Example Output

```
Found 3 log files to process:
  match1.wpilog
  match2.wpilog
  match3.wpilog

Processing: match1.wpilog
  Captured 1247 records from match1.wpilog

=== TIME ANALYSIS RESULTS FOR match1.wpilog ===

Analyzing: /RealOutputs/Manipulator/State (SHOOT_CORAL) -> /Manipulator/IsIndexerIRBlocked (False)
  Total cycles found in this file: 5
  Found cycle 1: 1.234567s
  Found cycle 2: 1.345678s
  ...
  Average Shooting Time: 1.298765 seconds
  Max Shooting Time: 1.456789 seconds
  Min Shooting Time: 1.123456 seconds

=== AGGREGATED TIME ANALYSIS RESULTS ACROSS ALL FILES ===

Aggregated Analysis: /RealOutputs/Manipulator/State (SHOOT_CORAL) -> /Manipulator/IsIndexerIRBlocked (False)
  Files processed: 3
  Total cycles found across all files: 15
  Average cycles per file: 5.00
  Minimum cycles in any file: 4
  Maximum cycles in any file: 6
  Aggregated Average Shooting Time: 1.287643 seconds
  ...
```

## Error Handling

The tool includes robust error handling for:
- Invalid or missing log files
- Malformed configuration files
- Missing or invalid data entries
- Type conversion errors

Warnings and errors are clearly reported to help diagnose issues.