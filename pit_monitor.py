#! /usr/bin/env python3
"""Keep a pit screen showing the newest finished match, hands off.

Composes the two standalone tools rather than absorbing them: sync_logs.py
fetches, analysis.py analyses, and this loops.

    ./pit_monitor.py ./logs checks2026.json --sync-from 10.30.61.2

Each cycle it optionally syncs, works out which log *would* be analysed, and
re-runs the analyser only if that has changed. Paired with the report's meta
refresh, a browser left open on the report tracks the newest match with nothing
typed between matches.

Candidates are taken newest-first and the search stops at the first settled match
log, so once the cache is warm a cycle classifies one file in about 10 ms. Cold
is a different story: proving a pit session is *not* a match needs a full read,
and after a week of practice the newest ten logs can all be pit sessions. That
first pass is therefore both cached to disk - so it is paid once ever rather than
once per start - and narrated, because the alternative is a silent terminal.

Nothing in a cycle is allowed to kill the loop. A robot that is absent, an
unreachable host, a bad config or a failed analysis are all reported and the loop
continues; a pit display that dies at the wrong moment is worse than a stale one.
"""

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from analysis import (MatchCache, classify_match_log, load_match_cache,
                      log_recorded_at, save_match_cache)
from sync_logs import UNREACHABLE_NOTICE

HERE = Path(__file__).resolve().parent
ANALYSIS = HERE / "analysis.py"
SYNC = HERE / "sync_logs.py"
LOG_SUFFIX = ".wpilog"

# The monitor only ever analyses one log - the newest finished match - so
# fetching the robot's whole history is pure waste. A season accumulates: the
# first real run against a robot listed 198 files and 6.55 GB, which would also
# have exceeded the sync subprocess timeout and left the loop retrying forever
# without progressing.
#
# Erring large: too small a window and the match log can fall outside it, which
# leaves the display quietly showing an older match. Too large only makes the
# first sync slow. Match logs measured 30-48 MB, so ten is a few hundred MB at
# worst and leaves nine logs of slack for pit sessions in between.
DEFAULT_SYNC_NEWEST = 10


@dataclass
class CycleResult:
    """What one pass round the loop did."""
    target: Optional[str] = None
    analysed: bool = False
    # None when syncing is not configured, else "ok", "unreachable" or "failed".
    # An unreachable robot is expected and must not be reported as a success.
    sync_status: Optional[str] = None
    still_writing: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)


def list_logs(folder: Path) -> List[str]:
    """Every .wpilog in the folder, newest recording first."""
    if not folder.is_dir():
        return []
    paths = [str(p) for p in folder.iterdir()
             if p.is_file() and p.name.endswith(LOG_SUFFIX)]
    return sorted(paths, key=lambda p: (log_recorded_at(p), os.path.basename(p)),
                  reverse=True)


def settled_sizes(paths: List[str], settle_seconds: float) -> Dict[str, bool]:
    """Map each path to whether its size held still over the settle window.

    A log the robot still has open grows, and so does one part-way through being
    copied in by hand. Neither should be analysed.
    """
    def size_of(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return -1

    if settle_seconds <= 0:
        return {path: True for path in paths}
    before = {path: size_of(path) for path in paths}
    time.sleep(settle_seconds)
    return {path: size_of(path) == before[path] for path in paths}


def find_target(folder: Path, settle_seconds: float,
                match_cache: MatchCache,
                announce: Optional[Callable[[str], None]] = None
                ) -> Tuple[Optional[str], List[str]]:
    """Return (newest settled match log, names of logs still being written).

    Args:
        folder: Directory holding the logs
        settle_seconds: Window over which a file must not change size
        match_cache: Name -> (size, verdict), read and written in place; a
            settled file's size is stable, so the costly negative is paid once
        announce: Called before each full read, which can run to tens of seconds

    Returns:
        The path to analyse, or None, plus the growing logs seen along the way
    """
    ordered = list_logs(folder)
    settled = settled_sizes(ordered, settle_seconds)

    def note(path: str, size: int) -> None:
        if announce is not None:
            announce(f"reading {os.path.basename(path)} ({size / 1e6:.0f} MB) "
                     f"to see whether it is a match; first time only")

    growing = []
    for path in ordered:
        if not settled[path]:
            growing.append(os.path.basename(path))
            continue
        if classify_match_log(path, match_cache, note):
            return path, growing
    return None, growing


def run_sync(folder: Path, host: str, extra: List[str]) -> Tuple[str, str]:
    """Fetch new logs. Returns (status, last line of output)."""
    command = [sys.executable, str(SYNC), str(folder), "--host", host] + extra
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as error:
        return "failed", f"sync failed: {error}"
    combined = done.stdout + done.stderr
    output = combined.strip().splitlines()
    last = output[-1] if output else ""
    if done.returncode != 0:
        return "failed", last
    if UNREACHABLE_NOTICE in combined:
        return "unreachable", last
    return "ok", last


def run_analysis(folder: Path, config: Path, html: Path,
                 extra: List[str]) -> Tuple[bool, str]:
    """Regenerate the report. Returns (ok, last line of output)."""
    command = [sys.executable, str(ANALYSIS), str(folder), str(config),
               "--latest", "--matches-only", "--html", str(html)] + extra
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    except (subprocess.TimeoutExpired, OSError) as error:
        return False, f"analysis failed: {error}"

    # Report what the run concluded, not whatever it happened to say last.
    # Progress and warnings go to stderr, so concatenating the two streams and
    # taking the final line surfaced "Scanning <file>..." in place of "Wrote
    # HTML report" on any run that had to classify a log.
    def last_line(*streams: str) -> str:
        for stream in streams:
            lines = stream.strip().splitlines()
            if lines:
                return lines[-1]
        return ""

    ok = done.returncode == 0
    if ok:
        return True, last_line(done.stdout, done.stderr)
    return False, last_line(done.stderr, done.stdout)


class Monitor:
    """One cycle of sync, decide, analyse - kept separable so it can be tested."""

    def __init__(self, folder: Path, config: Path, html: Path,
                 settle_seconds: float = 1.0,
                 sync_host: Optional[str] = None,
                 sync_extra: Optional[List[str]] = None,
                 analysis_extra: Optional[List[str]] = None,
                 syncer: Callable = run_sync,
                 analyser: Callable = run_analysis,
                 announce: Optional[Callable[[str], None]] = None):
        self.folder = folder
        self.config = config
        self.html = html
        self.settle_seconds = settle_seconds
        self.sync_host = sync_host
        self.sync_extra = sync_extra or []
        self.analysis_extra = analysis_extra or []
        self.syncer = syncer
        self.analyser = analyser
        self.announce = announce
        self.last_analysed: Optional[str] = None
        # Seeded from disk so a restart, or a laptop that has already run once
        # today, does not re-read every pit session it has ever synced.
        self.match_cache: MatchCache = load_match_cache(str(folder))

    def say(self, result: CycleResult, message: str) -> None:
        """Record a message and, if anyone is listening, show it now.

        A cycle can run for minutes on a cold cache, so holding its messages
        until it returns is what makes the terminal look hung.
        """
        result.messages.append(message)
        if self.announce is not None:
            self.announce(message)

    def cycle(self) -> CycleResult:
        """Run one pass. Never raises; failures are reported and survived."""
        result = CycleResult()

        if self.sync_host:
            try:
                status, message = self.syncer(self.folder, self.sync_host,
                                              self.sync_extra)
            except Exception as error:  # noqa: BLE001 - a cycle must not be fatal
                status, message = "failed", f"sync raised: {error}"
            result.sync_status = status
            if message:
                self.say(result, message)

        cached_before = dict(self.match_cache)
        try:
            target, growing = find_target(
                self.folder, self.settle_seconds, self.match_cache,
                announce=None if self.announce is None else self.announce)
        except Exception as error:  # noqa: BLE001
            self.say(result, f"could not inspect {self.folder}: {error}")
            return result
        if self.match_cache != cached_before:
            save_match_cache(str(self.folder), self.match_cache)

        result.target = target
        result.still_writing = growing

        if target is None:
            self.say(result, "no finished match log yet")
            return result

        if target == self.last_analysed:
            return result  # nothing new; leave the page alone

        try:
            ok, message = self.analyser(self.folder, self.config, self.html,
                                        self.analysis_extra)
        except Exception as error:  # noqa: BLE001
            ok, message = False, f"analysis raised: {error}"
        if message:
            self.say(result, message)
        if ok:
            self.last_analysed = target
            result.analysed = True
        return result


def describe(result: CycleResult) -> str:
    """One line summarising a cycle, for the terminal."""
    parts = []
    if result.sync_status is not None:
        parts.append({"ok": "synced",
                      "unreachable": "robot not reachable",
                      "failed": "sync FAILED"}[result.sync_status])
    if result.analysed:
        parts.append(f"report updated from {os.path.basename(result.target)}")
    elif result.target:
        parts.append(f"unchanged ({os.path.basename(result.target)})")
    else:
        parts.append("waiting for a match log")
    if result.still_writing:
        parts.append(f"still being written: {', '.join(result.still_writing)}")
    return " | ".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Keep a pit report updated from the newest finished match.")
    parser.add_argument("log_folder", help="folder holding (and receiving) logs")
    parser.add_argument("config_json_file", help="analysis config")
    parser.add_argument("--html", default="report.html",
                        help="report to rewrite in place (default report.html)")
    parser.add_argument("--sync-from", metavar="HOST",
                        help="also fetch new logs from the roboRIO each cycle")
    parser.add_argument("--sync-newest", type=int, default=DEFAULT_SYNC_NEWEST,
                        metavar="N",
                        help=f"fetch only the N most recent logs "
                             f"(default {DEFAULT_SYNC_NEWEST}); 0 fetches every "
                             f"log the robot has, which is rarely wanted")
    parser.add_argument("--sync-arg", action="append", dest="sync_args",
                        metavar="ARG",
                        help="extra argument passed through to sync_logs.py. "
                             "Repeatable, and needs the = form so argparse does "
                             "not read the value as an option of its own: "
                             "--sync-arg=--min-size --sync-arg=1000000")
    parser.add_argument("--interval", type=float, default=20.0, metavar="SECONDS",
                        help="seconds between cycles (default 20)")
    parser.add_argument("--settle-seconds", type=float, default=1.0,
                        metavar="SECONDS",
                        help="window a log must hold its size for (default 1)")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit")
    args = parser.parse_args()

    folder = Path(args.log_folder)
    sync_extra = list(args.sync_args or [])
    if args.sync_newest > 0:
        sync_extra = ["--newest", str(args.sync_newest)] + sync_extra

    def announce(message: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

    monitor = Monitor(folder, Path(args.config_json_file), Path(args.html),
                      settle_seconds=args.settle_seconds,
                      sync_host=args.sync_from, sync_extra=sync_extra,
                      announce=announce)

    print(f"Watching {folder} -> {args.html}"
          + (f", syncing {' '.join(sync_extra)} from {args.sync_from}"
             if args.sync_from else "")
          + (f", every {args.interval:g}s" if not args.once else ""))
    try:
        while True:
            result = monitor.cycle()
            # The detail lines have already been printed by `announce` as they
            # happened; this is only the summary.
            print(f"[{time.strftime('%H:%M:%S')}] {describe(result)}", flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
