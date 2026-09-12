"""Segment-scoped glob matching for log entry names.

Entry names are "/"-delimited paths, so "/" is a hard boundary:

    *      matches within a single segment
    **     spans zero or more whole segments
    ?      matches one character within a segment
    [abc]  matches a character class within a segment

Plain fnmatch is deliberately not applied to the whole path: its "*" crosses "/",
so "/RealOutputs/*/state" would also match "/RealOutputs/a/b/c/state". Keeping "/"
significant is what makes "/RealOutputs/Vision/*/sending frames" mean "each
camera" rather than "anything below Vision".

A name containing none of * ? [ is a literal and matches only itself.
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["WILDCARD_CHARS", "has_wildcard", "split_segments", "EntryPattern",
           "expand_roles"]

WILDCARD_CHARS = "*?["


def has_wildcard(pattern: str) -> bool:
    """Whether a configured entry name should be treated as a pattern."""
    return any(char in pattern for char in WILDCARD_CHARS)


def split_segments(name: str) -> List[str]:
    """Split an entry name into its "/"-delimited segments.

    Only the empty segment produced by the leading "/" is dropped. Interior empty
    segments are significant: some robot code emits a doubled slash (e.g.
    "/RealOutputs//ShooterModes/DistanceToHub"), and a pattern must address that
    name as it is actually logged rather than silently normalising it away.
    """
    segments = name.split("/")
    return segments[1:] if segments and segments[0] == "" else segments


def _segment_regex(segment: str, capture: bool) -> str:
    """Translate one segment's glob syntax to regex, never crossing "/"."""
    out = []
    i = 0
    while i < len(segment):
        char = segment[i]
        if char == "*":
            out.append("([^/]*)" if capture else "[^/]*")
            i += 1
        elif char == "?":
            out.append("([^/])" if capture else "[^/]")
            i += 1
        elif char == "[":
            close = segment.find("]", i + 1)
            if close == -1:
                out.append(re.escape(char))
                i += 1
            else:
                body = segment[i + 1:close]
                body = ("^" + body[1:]) if body.startswith("!") else body
                char_class = "[" + body.replace("\\", "\\\\") + "]"
                out.append("(" + char_class + ")" if capture else char_class)
                i = close + 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


def _compile(segments: Sequence[str], capture: bool) -> "re.Pattern":
    parts = []
    for segment in segments:
        if segment == "**":
            parts.append("((?:/[^/]+)*)" if capture else "(?:/[^/]+)*")
        else:
            parts.append("/" + _segment_regex(segment, capture))
    return re.compile("^" + "".join(parts) + "$")


class EntryPattern:
    """A compiled entry-name pattern.

    Compiled once per configured name; matching is then a regex test, which keeps
    it off the per-record hot path when results are memoized per entry.
    """

    def __init__(self, pattern: str):
        self.pattern = pattern
        self.segments = split_segments(pattern)
        self.has_wildcard = has_wildcard(pattern)
        self._full = _compile(self.segments, capture=False)
        self._captured = _compile(self.segments, capture=True)
        # Prefixes let an ancestor be recognised without matching the whole
        # pattern, which is how a struct parent is captured for a leaf pattern.
        self._prefixes = [_compile(self.segments[:k], capture=False)
                          for k in range(1, len(self.segments) + 1)]

    def __repr__(self) -> str:
        return f"EntryPattern({self.pattern!r})"

    def matches(self, name: str) -> bool:
        """Whether a concrete entry name matches this pattern in full."""
        return self._full.match(name) is not None

    def could_contain(self, name: str) -> bool:
        """Whether an entry could produce a field matching this pattern.

        True for a full match, and for any ancestor of one: a struct logged as
        "/a/b" can produce the flattened field "/a/b/c/d", so "/a/b" must be
        captured for the pattern "/a/b/c/d". Deliberately over-inclusive; the
        pattern is resolved against real field names again after decoding.
        """
        return any(prefix.match(name) is not None for prefix in self._prefixes)

    def captures(self, name: str) -> Optional[Tuple[str, ...]]:
        """The text each wildcard matched, or None if the name does not match."""
        found = self._captured.match(name)
        return found.groups() if found else None

    def substitute(self, captures: Sequence[str]) -> str:
        """Rebuild a concrete entry name by filling wildcards with captured text.

        The inverse of `captures()`: given ("BCL",) and the pattern
        "/RealOutputs/Vision/*/sending frames", returns
        "/RealOutputs/Vision/BCL/sending frames". Used to name an entry that a
        rule expected but never saw, which by definition is not in the log.
        """
        values = list(captures)

        def take() -> str:
            return values.pop(0) if values else ""

        parts = []
        for segment in self.segments:
            if segment == "**":
                # A "**" capture already carries its own leading separators.
                parts.append(take())
                continue
            out = []
            i = 0
            while i < len(segment):
                char = segment[i]
                if char in "*?":
                    out.append(take())
                    i += 1
                elif char == "[":
                    close = segment.find("]", i + 1)
                    if close == -1:
                        out.append(char)
                        i += 1
                    else:
                        out.append(take())
                        i = close + 1
                else:
                    out.append(char)
                    i += 1
            parts.append("/" + "".join(out))
        return "".join(parts)

    def expand(self, names: Sequence[str]) -> List[str]:
        """Every name matching this pattern, in sorted order."""
        return sorted(name for name in names if self.matches(name))

    def group_by_captures(self, names: Sequence[str]) -> Dict[Tuple[str, ...], str]:
        """Map each distinct capture tuple to the first name producing it."""
        grouped: Dict[Tuple[str, ...], str] = {}
        for name in sorted(names):
            captured = self.captures(name)
            if captured is not None and captured not in grouped:
                grouped[captured] = name
        return grouped


def expand_roles(roles: Dict[str, str], names: Sequence[str]) -> List[Dict[str, str]]:
    """Expand one analysis's entry names into concrete analyses.

    An analysis names entries by role ("startEntry", "endEntry", ...). Roles
    holding a wildcard are paired by what their wildcards matched, so
    "/RealOutputs/Vision/*/sending frames" in both the start and end role yields
    one analysis per camera rather than a cross product. Roles without a wildcard
    are literal and repeat across every expansion.

    Args:
        roles: Role name -> configured entry name
        names: The concrete field names available to match against

    Returns:
        One dict per expansion, same keys as `roles` with concrete names. A
        literal-only analysis returns a single unchanged expansion, so configs
        without wildcards behave exactly as before.
    """
    patterns = {role: EntryPattern(value) for role, value in roles.items() if value}
    wildcard_roles = [role for role, pattern in patterns.items() if pattern.has_wildcard]

    if not wildcard_roles:
        return [dict(roles)]

    driver = patterns[wildcard_roles[0]]
    grouped = {role: patterns[role].group_by_captures(names) for role in wildcard_roles}

    expansions = []
    for captured, driver_name in sorted(driver.group_by_captures(names).items(),
                                        key=lambda item: item[1]):
        expansion = {}
        for role, value in roles.items():
            if role not in patterns or not patterns[role].has_wildcard:
                expansion[role] = value
            elif patterns[role].pattern == driver.pattern:
                expansion[role] = driver_name
            else:
                # Pair by what the wildcards matched; if this role has no such
                # name, keep the pattern so the caller reports it missing.
                expansion[role] = grouped[role].get(captured, value)
        expansions.append(expansion)
    return expansions
