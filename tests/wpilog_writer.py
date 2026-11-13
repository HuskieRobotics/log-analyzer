"""A minimal .wpilog writer, for tests that need a real file on disk.

Writes the WPILib DataLog format that datalog.DataLogReader reads: a header,
then records whose entry id, payload size and timestamp are little-endian
integers. Every record here uses the widest field sizes (4, 4 and 8 bytes),
which the format allows and which keeps the writer trivial.
"""

import struct
from pathlib import Path
from typing import List, Sequence

# Entry id, payload size and timestamp lengths of 4, 4 and 8 bytes, each
# stored as length - 1 in the header byte.
_HEADER = 0x3 | (0x3 << 2) | (0x7 << 4)


def _string(text: str) -> bytes:
    data = text.encode("utf-8")
    return struct.pack("<I", len(data)) + data


class WpilogWriter:
    """Builds a log in memory; `save` writes it out."""

    def __init__(self):
        self._records: List[bytes] = []
        self._next_id = 1

    def _record(self, entry: int, timestamp_us: int, payload: bytes) -> None:
        self._records.append(struct.pack("<BIIQ", _HEADER, entry, len(payload),
                                         timestamp_us) + payload)

    def start(self, name: str, type_str: str, timestamp_us: int = 0,
              metadata: str = "", entry: int = None) -> int:
        """Declare an entry and return its id (a given id can be reused)."""
        if entry is None:
            entry = self._next_id
            self._next_id += 1
        self._record(0, timestamp_us, bytes([0]) + struct.pack("<I", entry)
                     + _string(name) + _string(type_str) + _string(metadata))
        return entry

    def finish(self, entry: int, timestamp_us: int = 0) -> None:
        self._record(0, timestamp_us, bytes([1]) + struct.pack("<I", entry))

    def raw(self, entry: int, timestamp_us: int, payload: bytes) -> None:
        self._record(entry, timestamp_us, payload)

    def boolean(self, entry: int, timestamp_us: int, value: bool) -> None:
        self.raw(entry, timestamp_us, bytes([1 if value else 0]))

    def int64(self, entry: int, timestamp_us: int, value: int) -> None:
        self.raw(entry, timestamp_us, struct.pack("<q", value))

    def double(self, entry: int, timestamp_us: int, value: float) -> None:
        self.raw(entry, timestamp_us, struct.pack("<d", value))

    def string(self, entry: int, timestamp_us: int, value: str) -> None:
        self.raw(entry, timestamp_us, value.encode("utf-8"))

    def string_array(self, entry: int, timestamp_us: int, values: Sequence[str]) -> None:
        self.raw(entry, timestamp_us, struct.pack("<I", len(values))
                 + b"".join(_string(value) for value in values))

    def to_bytes(self) -> bytes:
        return b"WPILOG" + struct.pack("<HI", 0x0100, 0) + b"".join(self._records)

    def save(self, path) -> Path:
        path = Path(path)
        path.write_bytes(self.to_bytes())
        return path
