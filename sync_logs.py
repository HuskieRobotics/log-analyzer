#! /usr/bin/env python3
r"""Copy new .wpilog files off the roboRIO.

A standalone tool. It shares nothing with analysis.py but the destination folder,
which is deliberate: the two have different failure modes, different lifetimes,
and are joined only by files appearing in a directory.

    ./sync_logs.py ./test/2026                     # fetch from 10.30.61.2
    ./sync_logs.py ./test/2026 --dry-run           # show what would be fetched
    ./sync_logs.py ./dest --from-local ./fake_bot  # exercise it without a robot

What counts as "new" is derived from the destination folder rather than a state
file: a remote file whose name and size already match a local one is skipped. Log
names are timestamped and unique, so name and size are sufficient, and because
downloads land under a temporary name and are renamed into place only after their
size is verified, a partial transfer never carries the final name and is simply
retried next run. Nothing to corrupt, and a restart mid-event neither redoes the
day nor skips a match.

The robot is usually absent. That is not an error: an unreachable host exits 0 so
a polling loop stays quiet.

Windows (the pit laptop)
------------------------
Run it as `python sync_logs.py ...`; the shebang and the executable bit do
nothing there. Three differences matter:

  * `ssh` and `scp` come from the OpenSSH Client optional feature. It is present
    by default on current Windows 10/11, but if it is not:
        Settings > System > Optional features > Add > OpenSSH Client
  * `sshpass` has no Windows build, so `--sshpass` is not an option there. Use
    key-based auth instead (below).
  * `UserKnownHostsFile=/dev/null` is a Unix path that Win32-OpenSSH rejects, so
    NULL_DEVICE selects NUL on Windows.

Because ssh reads a password from the console rather than stdin, an empty
password cannot be supplied non-interactively on any platform. Install a key
once, interactively (press Enter at the password prompt):

    ssh-keygen -t ed25519
    # Windows PowerShell - there is no ssh-copy-id:
    type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh admin@10.30.61.2 ^
        "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
    # macOS / Linux:
    ssh-copy-id admin@10.30.61.2

After that every sync is non-interactive on any platform.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

DEFAULT_HOST = "10.30.61.2"
DEFAULT_USER = "admin"

# AdvantageKit commonly writes to a USB stick rather than internal storage, and
# which mount point appears depends on the stick.
DEFAULT_REMOTE_DIRS = ("/media/sda1", "/media/sda2", "/U", "/home/lvuser/logs")

LOG_SUFFIX = ".wpilog"

# An unreachable robot is the normal case, not a failure, so sync still exits 0.
# A caller that needs to tell "nothing to do" from "everything copied" matches
# on this rather than on the exit code; it is shared so the two do not drift.
UNREACHABLE_NOTICE = "Robot not reachable; nothing to do."

# Win32-OpenSSH does not understand /dev/null as a path; it wants NUL. The robot
# is a link-local device that is reimaged regularly, so its host key changes and
# is not worth recording either way.
NULL_DEVICE = "NUL" if os.name == "nt" else "/dev/null"
PARTIAL_SUFFIX = ".part"

# Portable enough for the roboRIO's minimal image: `find` for the paths and
# `wc -c` for the sizes, avoiding GNU-only `find -printf` and `stat -c`.
#
# Deliberately a single line. On Windows the argument list is flattened into one
# command string before CreateProcess sees it, and embedded newlines are a good
# way to have a remote script arrive mangled. The redirections here run on the
# roboRIO, so /dev/null is correct in this string regardless of our platform.
LISTING_SCRIPT = (
    r"""for d in {dirs}; do [ -d "$d" ] || continue; """
    r"""find "$d" -type f -name '*{suffix}' 2>/dev/null; done | """
    r"""while IFS= read -r f; do printf '%s %s\n' "$(wc -c < "$f" 2>/dev/null)" "$f"; done"""
)


@dataclass(frozen=True)
class RemoteFile:
    """One log file on the far side, with the size used to decide freshness."""
    path: str
    size: int

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass
class SyncResult:
    downloaded: List[RemoteFile] = field(default_factory=list)
    skipped: List[RemoteFile] = field(default_factory=list)
    active: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    reachable: bool = True


# === Decision logic (pure; this is the part that is unit tested) ==============

def parse_listing(text: str) -> List[RemoteFile]:
    """Parse '<size> <path>' lines into RemoteFiles, ignoring anything malformed."""
    files = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        size_text, _, path = line.partition(" ")
        path = path.strip()
        if not path or not size_text.isdigit():
            continue
        files.append(RemoteFile(path=path, size=int(size_text)))
    return files


def find_growing_files(first: Sequence[RemoteFile],
                       second: Sequence[RemoteFile]) -> set:
    """Paths whose size changed between two listings, i.e. still being written.

    A powered roboRIO logs continuously, so the log it currently has open grows
    between any two listings. That file must not be downloaded: a snapshot of it
    is truncated, and worse, it would then match on size and look complete.

    A path present only in the second listing counts as growing too - a log that
    appeared in the last couple of seconds is the one being written.
    """
    sizes = {entry.path: entry.size for entry in first}
    return {entry.path for entry in second if sizes.get(entry.path) != entry.size}


def local_index(destination: Path) -> Dict[str, int]:
    """Map each already-downloaded log's name to its size on disk."""
    if not destination.is_dir():
        return {}
    return {entry.name: entry.stat().st_size
            for entry in destination.iterdir()
            if entry.is_file() and entry.name.endswith(LOG_SUFFIX)}


def select_new_files(remote_files: Sequence[RemoteFile],
                     local: Dict[str, int]) -> List[RemoteFile]:
    """Return the remote files not already present locally at the same size.

    A name present locally at a different size is treated as new, so a transfer
    interrupted before the size check is retried rather than left truncated.
    Duplicate names across remote directories resolve to the largest, which is
    the most complete copy.
    """
    best: Dict[str, RemoteFile] = {}
    for candidate in remote_files:
        existing = best.get(candidate.name)
        if existing is None or candidate.size > existing.size:
            best[candidate.name] = candidate

    return [candidate for _, candidate in sorted(best.items())
            if local.get(candidate.name) != candidate.size]


# === Transports ==============================================================

class LocalSource:
    """Reads from a directory instead of a robot, so the flow is testable."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def describe(self) -> str:
        return f"local directory {self.root}"

    def list_logs(self, remote_dirs: Sequence[str]) -> Optional[List[RemoteFile]]:
        if not self.root.is_dir():
            return None
        return [RemoteFile(path=str(p), size=p.stat().st_size)
                for p in sorted(self.root.rglob(f"*{LOG_SUFFIX}")) if p.is_file()]

    def fetch(self, remote: RemoteFile, target: Path) -> None:
        shutil.copyfile(remote.path, target)


class SshSource:
    """Reads from the roboRIO over ssh/scp."""

    def __init__(self, host: str, user: str, identity: Optional[str] = None,
                 timeout: int = 8, use_sshpass: bool = False,
                 password: str = ""):
        self.host = host
        self.user = user
        self.identity = identity
        self.timeout = timeout
        self.use_sshpass = use_sshpass
        self.password = password

    def describe(self) -> str:
        return f"{self.user}@{self.host}"

    def missing_tools(self) -> List[str]:
        """Which required command-line tools are not on PATH."""
        needed = ["ssh", "scp"] + (["sshpass"] if self.use_sshpass else [])
        return [tool for tool in needed if shutil.which(tool) is None]

    def _options(self) -> List[str]:
        options = [
            "-o", f"ConnectTimeout={self.timeout}",
            # The robot's key changes with every reimage and it is a link-local
            # device on a closed network; prompting about it would hang a
            # non-interactive sync.
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={NULL_DEVICE}",
            "-o", "LogLevel=ERROR",
        ]
        if self.identity:
            options += ["-i", self.identity, "-o", "IdentitiesOnly=yes"]
        if not self.use_sshpass:
            # Without sshpass there is no way to answer a prompt, so fail fast
            # rather than block forever waiting on a tty.
            options += ["-o", "BatchMode=yes"]
        return options

    def _wrap(self, command: List[str]) -> List[str]:
        if self.use_sshpass:
            return ["sshpass", "-p", self.password] + command
        return command

    def list_logs(self, remote_dirs: Sequence[str]) -> Optional[List[RemoteFile]]:
        script = LISTING_SCRIPT.format(
            dirs=" ".join(f"'{d}'" for d in remote_dirs), suffix=LOG_SUFFIX)
        command = self._wrap(
            ["ssh"] + self._options() + [f"{self.user}@{self.host}", script])
        try:
            done = subprocess.run(command, capture_output=True, text=True,
                                  timeout=self.timeout + 30)
        except (subprocess.TimeoutExpired, FileNotFoundError) as error:
            print(f"  cannot reach {self.describe()}: {error}", file=sys.stderr)
            return None
        if done.returncode != 0:
            detail = done.stderr.strip().splitlines()
            message = detail[-1] if detail else f"ssh exited {done.returncode}"
            print(f"  cannot reach {self.describe()}: {message}", file=sys.stderr)
            if "denied" in message.lower() or "publickey" in message.lower():
                # ssh reads a password from the tty, not stdin, so an empty
                # password cannot be supplied non-interactively. Either give the
                # robot a key once, or let sshpass answer for us.
                print(f"  authentication failed. Either install a key once:\n"
                      f"      ssh-copy-id {self.user}@{self.host}\n"
                      f"  or re-run with --sshpass (requires the sshpass tool).",
                      file=sys.stderr)
            return None
        return parse_listing(done.stdout)

    def fetch(self, remote: RemoteFile, target: Path) -> None:
        command = self._wrap(
            ["scp"] + self._options()
            + [f"{self.user}@{self.host}:{remote.path}", str(target)])
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError(done.stderr.strip() or f"scp exited {done.returncode}")


# === Orchestration ===========================================================

def sync(source, destination: Path, remote_dirs: Sequence[str],
         dry_run: bool = False, settle_seconds: float = 2.0) -> SyncResult:
    """Copy every finished log not already present in the destination folder.

    Args:
        source: A LocalSource or SshSource
        destination: Folder that receives the logs, and that defines what is new
        remote_dirs: Directories to search on the far side
        dry_run: List what would be copied without copying it
        settle_seconds: Gap between two listings used to spot a log that is still
            being written; 0 disables the check

    Returns:
        A SyncResult naming what was downloaded, skipped, still active and failed
    """
    result = SyncResult()

    listing = source.list_logs(remote_dirs)
    if listing is None:
        result.reachable = False
        return result

    if settle_seconds > 0 and listing:
        # The robot is logging the whole time it is powered, including in the
        # pit, so the newest log is normally open and growing. Comparing two
        # listings identifies it without assuming which one it is.
        time.sleep(settle_seconds)
        second = source.list_logs(remote_dirs)
        if second is None:
            result.reachable = False
            return result
        growing = find_growing_files(listing, second)
        result.active = sorted(growing)
        listing = [entry for entry in second if entry.path not in growing]
        for path in result.active:
            print(f"  still being written, skipping: {os.path.basename(path)}")

    local = local_index(destination)
    wanted = select_new_files(listing, local)
    result.skipped = [f for f in listing if f not in wanted and f.name in local]

    if dry_run:
        result.downloaded = wanted
        return result

    destination.mkdir(parents=True, exist_ok=True)
    for remote in wanted:
        target = destination / remote.name
        partial = destination / (remote.name + PARTIAL_SUFFIX)
        try:
            source.fetch(remote, partial)
            actual = partial.stat().st_size
            if actual != remote.size:
                raise RuntimeError(
                    f"size mismatch: expected {remote.size}, got {actual}")
            # Atomic: the final name never exists until the copy is verified, so
            # a watcher never sees a half-written log.
            os.replace(partial, target)
            result.downloaded.append(remote)
            print(f"  copied {remote.name} ({remote.size:,} bytes)")
        except Exception as error:  # noqa: BLE001 - reported, never fatal
            partial.unlink(missing_ok=True)
            result.failed.append(f"{remote.name}: {error}")
            print(f"  FAILED {remote.name}: {error}", file=sys.stderr)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy new .wpilog files off the roboRIO.")
    parser.add_argument("destination", help="folder to copy logs into")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"roboRIO address (default {DEFAULT_HOST})")
    parser.add_argument("--user", default=DEFAULT_USER,
                        help=f"ssh user (default {DEFAULT_USER})")
    parser.add_argument("--identity", help="ssh private key to use")
    parser.add_argument("--sshpass", action="store_true",
                        help="authenticate via sshpass (needed for an empty "
                             "password, which ssh cannot supply on its own; "
                             "not available on Windows - use a key instead)")
    parser.add_argument("--password", default="",
                        help="password for --sshpass (default: empty)")
    parser.add_argument("--remote-dir", action="append", dest="remote_dirs",
                        help="directory to search on the robot (repeatable; "
                             f"default {' '.join(DEFAULT_REMOTE_DIRS)})")
    parser.add_argument("--from-local", metavar="DIR",
                        help="read from a local directory instead of a robot")
    parser.add_argument("--settle-seconds", type=float, default=2.0,
                        metavar="SECONDS",
                        help="gap between two listings used to detect the log "
                             "the robot still has open; 0 disables (default 2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be copied, copy nothing")
    args = parser.parse_args()

    destination = Path(args.destination)
    remote_dirs = args.remote_dirs or list(DEFAULT_REMOTE_DIRS)

    if args.from_local:
        source = LocalSource(Path(args.from_local))
    else:
        source = SshSource(args.host, args.user, args.identity,
                           use_sshpass=args.sshpass, password=args.password)

    missing = getattr(source, "missing_tools", lambda: [])()
    if missing:
        print(f"Required tool(s) not found on PATH: {', '.join(missing)}",
              file=sys.stderr)
        if os.name == "nt":
            print("  On Windows, ssh and scp come from the OpenSSH Client "
                  "optional feature:\n"
                  "      Settings > System > Optional features > Add > OpenSSH Client\n"
                  "  sshpass has no Windows build; use key-based auth instead "
                  "(see --help).", file=sys.stderr)
        sys.exit(2)

    print(f"Syncing from {source.describe()} into {destination}", flush=True)
    result = sync(source, destination, remote_dirs, args.dry_run,
                  settle_seconds=args.settle_seconds)

    if not result.reachable:
        # The robot is absent most of the time; that is the normal case, so a
        # polling loop should not treat it as a failure.
        print(UNREACHABLE_NOTICE)
        sys.exit(0)

    if args.dry_run:
        for remote in result.downloaded:
            print(f"  would copy {remote.name} ({remote.size:,} bytes)")

    verb = "would copy" if args.dry_run else "copied"
    print(f"{verb} {len(result.downloaded)}, "
          f"already present {len(result.skipped)}, "
          f"still being written {len(result.active)}, "
          f"failed {len(result.failed)}")
    sys.exit(1 if result.failed else 0)


if __name__ == "__main__":
    main()
