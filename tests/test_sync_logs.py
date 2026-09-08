#! /usr/bin/env python3
"""Unit tests for the log sync tool.

There is no roboRIO here and there will not be one in CI, so the decision logic
is separated from the transport and tested directly, and the whole flow is
exercised end to end through LocalSource.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sync_logs import (  # noqa: E402
    LISTING_SCRIPT,
    NULL_DEVICE,
    LocalSource,
    SshSource,
    RemoteFile,
    local_index,
    parse_listing,
    select_new_files,
    sync,
)


def parse_windows_command_line(command_line):
    """Reference implementation of CommandLineToArgvW's argument rules.

    Used to prove that what subprocess sends on Windows is what ssh.exe parses
    back out, since the real thing cannot be run from here.
    """
    args, current, in_quotes, i = [], [], False, 0
    while i < len(command_line):
        char = command_line[i]
        if char == "\\":
            slashes = 0
            while i < len(command_line) and command_line[i] == "\\":
                slashes += 1
                i += 1
            if i < len(command_line) and command_line[i] == '"':
                current.append("\\" * (slashes // 2))
                if slashes % 2:
                    current.append('"')      # escaped quote
                else:
                    in_quotes = not in_quotes
                i += 1
            else:
                current.append("\\" * slashes)
        elif char == '"':
            in_quotes = not in_quotes
            i += 1
        elif char in " \t" and not in_quotes:
            if current:
                args.append("".join(current))
                current = []
            i += 1
        else:
            current.append(char)
            i += 1
    if current:
        args.append("".join(current))
    return args


class WindowsQuotingTest(unittest.TestCase):
    """The pit laptop is Windows; the remote script must survive its quoting."""

    def test_remote_script_round_trips_through_windows_argv(self):
        script = LISTING_SCRIPT.format(dirs="'/media/sda1' '/U'", suffix=".wpilog")
        flattened = subprocess.list2cmdline(["ssh", "admin@10.30.61.2", script])
        self.assertEqual(parse_windows_command_line(flattened)[2], script)

    def test_the_script_is_a_single_line(self):
        # Embedded newlines are a good way to have a remote script arrive mangled.
        self.assertNotIn("\n", LISTING_SCRIPT)

    def test_script_is_valid_posix_shell(self):
        script = LISTING_SCRIPT.format(dirs="'/definitely/not/here'", suffix=".wpilog")
        done = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")

    def test_null_device_matches_the_platform(self):
        # Win32-OpenSSH rejects /dev/null for UserKnownHostsFile.
        self.assertEqual(NULL_DEVICE, "NUL" if os.name == "nt" else "/dev/null")

    def test_remote_paths_use_posix_separators_regardless_of_host_os(self):
        self.assertEqual(RemoteFile("/media/sda1/deep/x.wpilog", 1).name, "x.wpilog")


class MissingToolsTest(unittest.TestCase):

    def test_reports_tools_that_are_absent(self):
        source = SshSource("10.30.61.2", "admin")
        self.assertNotIn("definitely-not-a-tool", source.missing_tools())
        source_with_pass = SshSource("10.30.61.2", "admin", use_sshpass=True)
        # sshpass has no Windows build and is not installed here either; the
        # tool must say so rather than fail obscurely mid-transfer.
        self.assertEqual(shutil.which("sshpass") is None,
                         "sshpass" in source_with_pass.missing_tools())


class ParseListingTest(unittest.TestCase):

    def test_parses_size_and_path(self):
        text = "12345 /media/sda1/akit_26-05-01_a.wpilog\n67 /U/b.wpilog\n"
        self.assertEqual(parse_listing(text), [
            RemoteFile("/media/sda1/akit_26-05-01_a.wpilog", 12345),
            RemoteFile("/U/b.wpilog", 67),
        ])

    def test_paths_with_spaces_survive(self):
        self.assertEqual(parse_listing("99 /U/my log.wpilog"),
                         [RemoteFile("/U/my log.wpilog", 99)])

    def test_malformed_lines_are_ignored(self):
        # wc -c prints nothing if the file vanished between find and wc.
        text = "\n 12 /U/ok.wpilog\nnotanumber /U/bad.wpilog\n456\n"
        self.assertEqual(parse_listing(text), [RemoteFile("/U/ok.wpilog", 12)])

    def test_name_is_the_basename(self):
        self.assertEqual(RemoteFile("/media/sda1/x/y.wpilog", 1).name, "y.wpilog")


class SelectNewFilesTest(unittest.TestCase):

    def test_absent_locally_is_new(self):
        remote = [RemoteFile("/U/a.wpilog", 10)]
        self.assertEqual(select_new_files(remote, {}), remote)

    def test_same_name_and_size_is_skipped(self):
        remote = [RemoteFile("/U/a.wpilog", 10)]
        self.assertEqual(select_new_files(remote, {"a.wpilog": 10}), [])

    def test_same_name_different_size_is_retried(self):
        """A transfer interrupted before verification must not be left truncated."""
        remote = [RemoteFile("/U/a.wpilog", 10)]
        self.assertEqual(select_new_files(remote, {"a.wpilog": 4}), remote)

    def test_duplicates_across_directories_resolve_to_the_largest(self):
        remote = [RemoteFile("/media/sda1/a.wpilog", 4),
                  RemoteFile("/U/a.wpilog", 10)]
        self.assertEqual(select_new_files(remote, {}), [RemoteFile("/U/a.wpilog", 10)])

    def test_result_is_ordered_by_name(self):
        remote = [RemoteFile("/U/c.wpilog", 1), RemoteFile("/U/a.wpilog", 1),
                  RemoteFile("/U/b.wpilog", 1)]
        self.assertEqual([f.name for f in select_new_files(remote, {})],
                         ["a.wpilog", "b.wpilog", "c.wpilog"])


class LocalIndexTest(unittest.TestCase):

    def test_indexes_only_wpilog_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.wpilog").write_bytes(b"12345")
            (root / "notes.txt").write_text("ignore me")
            (root / "sub").mkdir()
            self.assertEqual(local_index(root), {"a.wpilog": 5})

    def test_missing_folder_is_empty(self):
        self.assertEqual(local_index(Path("/no/such/folder")), {})


class EndToEndTest(unittest.TestCase):
    """The whole flow, through a local directory standing in for the robot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.robot = root / "robot"
        self.dest = root / "dest"
        self.robot.mkdir()
        (self.robot / "match1.wpilog").write_bytes(b"a" * 100)
        (self.robot / "match2.wpilog").write_bytes(b"b" * 200)

    def tearDown(self):
        self._tmp.cleanup()

    def run_sync(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return sync(LocalSource(self.robot), self.dest, [], **kwargs)

    def test_first_run_copies_everything(self):
        result = self.run_sync()
        self.assertEqual(sorted(f.name for f in result.downloaded),
                         ["match1.wpilog", "match2.wpilog"])
        self.assertEqual((self.dest / "match1.wpilog").read_bytes(), b"a" * 100)
        self.assertEqual(result.failed, [])

    def test_second_run_copies_nothing(self):
        self.run_sync()
        result = self.run_sync()
        self.assertEqual(result.downloaded, [])
        self.assertEqual(len(result.skipped), 2)

    def test_only_the_new_match_is_copied(self):
        self.run_sync()
        (self.robot / "match3.wpilog").write_bytes(b"c" * 300)
        result = self.run_sync()
        self.assertEqual([f.name for f in result.downloaded], ["match3.wpilog"])

    def test_a_truncated_local_copy_is_refetched(self):
        self.run_sync()
        (self.dest / "match1.wpilog").write_bytes(b"a" * 10)  # simulate a partial
        result = self.run_sync()
        self.assertEqual([f.name for f in result.downloaded], ["match1.wpilog"])
        self.assertEqual((self.dest / "match1.wpilog").read_bytes(), b"a" * 100)

    def test_dry_run_writes_nothing(self):
        result = self.run_sync(dry_run=True)
        self.assertEqual(len(result.downloaded), 2)
        self.assertFalse(self.dest.exists())

    def test_no_partial_files_are_left_behind(self):
        self.run_sync()
        self.assertEqual([p.name for p in self.dest.iterdir() if ".part" in p.name], [])

    def test_a_failed_fetch_leaves_no_final_file(self):
        class BrokenSource(LocalSource):
            def fetch(self, remote, target):
                target.write_bytes(b"short")  # wrong size, verification fails
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = sync(BrokenSource(self.robot), self.dest, [])
        self.assertEqual(result.downloaded, [])
        self.assertEqual(len(result.failed), 2)
        self.assertFalse((self.dest / "match1.wpilog").exists())
        self.assertEqual(list(self.dest.glob("*.part")), [])

    def test_unreachable_source_is_not_an_error(self):
        result = sync(LocalSource(Path("/no/such/robot")), self.dest, [])
        self.assertFalse(result.reachable)
        self.assertEqual(result.downloaded, [])
        self.assertEqual(result.failed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
