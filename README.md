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
        "startValue": "WAITING_FOR_CORAL",
        "endEntry": "/RealOutputs/LEDS/state",
        "endValue": "SCORING",
        "calculations": [
            {"type": "average", "name": "Average Cycle Time"},
            {"type": "max", "name": "Max Cycle Time"},
            {"type": "min", "name": "Min Cycle Time"},
            {"type": "count", "name": "Cycle Count"},
            {"type": "outlier_2std", "name": "Outliers (2 std dev)"}
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
        "entry": "/RealOutputs/DriveToReef/difference (reef frame)/translation/y",
        "entryUnit": "m",
        "triggerEntry": "/Manipulator/IsIndexerIRBlocked",
        "triggerValue": false,
        "calculations": [
            {"type": "average", "name": "Average Y Diff at Reef"},
            {"type": "max", "name": "Max Y Diff at Reef"},
            {"type": "min", "name": "Min Y Diff at Reef"},
            {"type": "abs_average", "name": "Average Y Diff at Reef (abs)"},
            {"type": "abs_max", "name": "Max Y Diff at Reef (abs)"},
            {"type": "abs_min", "name": "Min Y Diff at Reef (abs)"},
            {"type": "abs_outlier_2std", "name": "Outliers (2 std dev; abs)"}
        ]
    }]
```

#### Value Analysis Properties

| Property | Type | Description |
|----------|------|-------------|
| `entry` | string | Log entry name to capture values from |
| `entryUnit` | string | Unit name to display for values (optional)  |
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
| `"abs_average"` | Arithmetic mean of absolute values | numeric values |
| `"abs_min"` | Minimum value of absolute values | numeric values |
| `"abs_max"` | Maximum value of absolute values | numeric values |
| `"count"` | Count of items | Time differences, numeric values |
| `"outlier_2std"` | All values greater than 2 standard deviations from the mean | Time differences, numeric values |
| `"abs_outlier_2std"` | All values greater than 2 standard deviations from the mean of absolute values | numeric values |

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
            {"type": "count", "name": "Cycle Count"},
            {"type": "outlier_2std", "name": "Outliers (2 std dev)"}
        ]
    }],
    "valueAnalysis": [{
        "entry": "/RealOutputs/DriveToReef/difference (reef frame)/translation/y",
        "entryUnit": "m",
        "triggerEntry": "/Manipulator/IsIndexerIRBlocked",
        "triggerValue": false,
        "calculations": [
            {"type": "average", "name": "Average Y Diff at Reef"},
            {"type": "max", "name": "Max Y Diff at Reef"},
            {"type": "min", "name": "Min Y Diff at Reef"},
            {"type": "abs_average", "name": "Average Y Diff at Reef (abs)"},
            {"type": "abs_max", "name": "Max Y Diff at Reef (abs)"},
            {"type": "abs_min", "name": "Min Y Diff at Reef (abs)"},
            {"type": "abs_outlier_2std", "name": "Outliers (2 std dev; abs)"}
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

There is a VERBOSE variable defined in the analysis.py file. If True, additional information will be displayed.

## Data Types Supported

The tool supports these WPILib data types:
- `boolean` / `boolean[]`
- `int64` / `int64[]`  
- `double` / `double[]`
- `float` / `float[]`
- `string` / `string[]`
- `json`
- `msgpack`

In addition, the tool supports stucts whose schema is encoded in the log file.

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
=== FILTERING CRITERIA ===
Filter by enabled: True
Filter by FMS attached: True
Robot mode filter: teleop

=== ANALYSIS ===
Found 2 log files to process:
  akit_25-04-17_14-51-30_curie_q40.wpilog
  akit_25-04-19_09-36-19_curie_e6.wpilog

Processing: akit_25-04-17_14-51-30_curie_q40.wpilog

=== TIME ANALYSIS RESULTS FOR akit_25-04-17_14-51-30_curie_q40.wpilog ===

Analyzing: /RealOutputs/Manipulator/State (SHOOT_CORAL) -> /Manipulator/IsIndexerIRBlocked (False)
  Total cycles found in this file: 13
  Average Shooting Time: 0.120009 s
  Max Shooting Time: 0.140015 s
    @ 293.243832 s 
  Min Shooting Time: 0.100003 s
    @ 337.654072 s 

Analyzing: /RealOutputs/Manipulator/State (WAITING_FOR_CORAL) -> /RealOutputs/LEDS/state (SCORING)
  Total cycles found in this file: 16
  Average Cycle Time: 6.804564 s
  Max Cycle Time: 9.882420 s
    @ 337.774090 s 
  Min Cycle Time: 3.967845 s
    @ 289.296021 s 

=== VALUE ANALYSIS RESULTS FOR akit_25-04-17_14-51-30_curie_q40.wpilog ===

Analyzing: /RealOutputs/DriveToReef/difference (reef frame)/translation/y when /Manipulator/IsIndexerIRBlocked = False
  Total values captured in this file: 13
  Average Y Diff at Reef: -0.003209 m
  Max Y Diff at Reef: 0.012699 m
    @ 289.276043 s 
  Min Y Diff at Reef: -0.012387 m
    @ 293.383847 s 
  Average Y Diff at Reef (abs): 0.008027 m
  Max Y Diff at Reef (abs): 0.012699 m
    @ 289.276043 s 
  Min Y Diff at Reef (abs): 0.000263 m
    @ 337.754075 s 

Processing: akit_25-04-19_09-36-19_curie_e6.wpilog

=== TIME ANALYSIS RESULTS FOR akit_25-04-19_09-36-19_curie_e6.wpilog ===

Analyzing: /RealOutputs/Manipulator/State (SHOOT_CORAL) -> /Manipulator/IsIndexerIRBlocked (False)
  Total cycles found in this file: 14
  Average Shooting Time: 0.112864 s
  Max Shooting Time: 0.120074 s
    @ 193.556561 s 
  Min Shooting Time: 0.099947 s
    @ 152.181621 s 

Analyzing: /RealOutputs/Manipulator/State (WAITING_FOR_CORAL) -> /RealOutputs/LEDS/state (SCORING)
  Total cycles found in this file: 15
  Average Cycle Time: 7.291700 s
  Max Cycle Time: 16.860040 s
    @ 158.646889 s 
  Min Cycle Time: 4.157296 s
    @ 137.682737 s 
  Outliers (2 std dev): 16.860040 s
    @ 158.646889 s 

=== VALUE ANALYSIS RESULTS FOR akit_25-04-19_09-36-19_curie_e6.wpilog ===

Analyzing: /RealOutputs/DriveToReef/difference (reef frame)/translation/y when /Manipulator/IsIndexerIRBlocked = False
  Total values captured in this file: 14
  Average Y Diff at Reef: -0.000523 m
  Max Y Diff at Reef: 0.012569 m
    @ 210.868924 s 
  Min Y Diff at Reef: -0.012030 m
    @ 175.586898 s 
  Average Y Diff at Reef (abs): 0.007575 m
  Max Y Diff at Reef (abs): 0.012569 m
    @ 210.868924 s 
  Min Y Diff at Reef (abs): 0.000153 m
    @ 187.954405 s 

=== AGGREGATED TIME ANALYSIS RESULTS ACROSS ALL FILES ===

Aggregated Analysis: /RealOutputs/Manipulator/State (SHOOT_CORAL) -> /Manipulator/IsIndexerIRBlocked (False)
  Files processed: 2
  Total cycles found across all files: 27
  Average Shooting Time: 0.116304 s
  Max Shooting Time: 0.140015 s
    @ 293.243832 s in akit_25-04-17_14-51-30_curie_q40.wpilog
  Min Shooting Time: 0.099947 s
    @ 152.181621 s in akit_25-04-19_09-36-19_curie_e6.wpilog

Aggregated Analysis: /RealOutputs/Manipulator/State (WAITING_FOR_CORAL) -> /RealOutputs/LEDS/state (SCORING)
  Files processed: 2
  Average cycles per file: 15.50
  Minimum cycles in any file: 15 in akit_25-04-19_09-36-19_curie_e6.wpilog
  Maximum cycles in any file: 16 in akit_25-04-17_14-51-30_curie_q40.wpilog
  Total cycles found across all files: 31
  Average Cycle Time: 7.040275 s
  Max Cycle Time: 16.860040 s
    @ 158.646889 s in akit_25-04-19_09-36-19_curie_e6.wpilog
  Min Cycle Time: 3.967845 s
    @ 289.296021 s in akit_25-04-17_14-51-30_curie_q40.wpilog
  Outliers (2 std dev): 16.860040 s
    @ 158.646889 s in akit_25-04-19_09-36-19_curie_e6.wpilog

=== AGGREGATED VALUE ANALYSIS RESULTS ACROSS ALL FILES ===

Aggregated Value Analysis: /RealOutputs/DriveToReef/difference (reef frame)/translation/y when /Manipulator/IsIndexerIRBlocked = False
  Files processed: 2
  Total values captured across all files: 27
  Average Y Diff at Reef: -0.001816 m
  Max Y Diff at Reef: 0.012699 m
    @ 289.276043 s in akit_25-04-17_14-51-30_curie_q40.wpilog
  Min Y Diff at Reef: -0.012387 m
    @ 293.383847 s in akit_25-04-17_14-51-30_curie_q40.wpilog
  Average Y Diff at Reef (abs): 0.007793 m
  Max Y Diff at Reef (abs): 0.012699 m
    @ 289.276043 s in akit_25-04-17_14-51-30_curie_q40.wpilog
  Min Y Diff at Reef (abs): 0.000153 m
    @ 187.954405 s in akit_25-04-19_09-36-19_curie_e6.wpilog
```

## Error Handling

The tool includes robust error handling for:
- Invalid or missing log files
- Malformed configuration files
- Missing or invalid data entries
- Type conversion errors

Warnings and errors are clearly reported to help diagnose issues.