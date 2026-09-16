#! /usr/bin/env python3
"""Unit tests for segment-scoped entry-name pattern matching."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from entry_patterns import EntryPattern, expand_roles, has_wildcard, split_segments  # noqa: E402

CAMERAS = [
    "/RealOutputs/Vision/BCH/sending frames",
    "/RealOutputs/Vision/BCL/sending frames",
    "/RealOutputs/Vision/BL/sending frames",
    "/RealOutputs/Vision/BR/sending frames",
    "/RealOutputs/Vision/BCH/UpdatePoseCount",
    "/RealOutputs/Vision/CamerasToConsider",
]


class WildcardDetectionTest(unittest.TestCase):

    def test_literals_are_not_patterns(self):
        self.assertFalse(has_wildcard("/RealOutputs/LEDS/state"))
        self.assertFalse(has_wildcard("/RealOutputs//ShooterModes/DistanceToHub"))

    def test_wildcards_are_detected(self):
        for pattern in ("/a/*/b", "/a/**/b", "/a/?/b", "/a/[xy]/b"):
            self.assertTrue(has_wildcard(pattern), pattern)

    def test_doubled_slash_is_significant_not_normalised_away(self):
        doubled = "/RealOutputs//ShooterModes/DistanceToHub"
        self.assertEqual(split_segments(doubled),
                         ["RealOutputs", "", "ShooterModes", "DistanceToHub"])
        # The name is logged with the doubled slash, so only that form matches.
        self.assertTrue(EntryPattern(doubled).matches(doubled))
        self.assertFalse(EntryPattern(doubled).matches(
            "/RealOutputs/ShooterModes/DistanceToHub"))
        self.assertFalse(EntryPattern(
            "/RealOutputs/ShooterModes/DistanceToHub").matches(doubled))


class MatchingTest(unittest.TestCase):

    def test_literal_matches_only_itself(self):
        pattern = EntryPattern("/RealOutputs/LEDS/state")
        self.assertTrue(pattern.matches("/RealOutputs/LEDS/state"))
        self.assertFalse(pattern.matches("/RealOutputs/LEDS/stat"))
        self.assertFalse(pattern.matches("/RealOutputs/LEDS/state/extra"))

    def test_star_stays_inside_one_segment(self):
        pattern = EntryPattern("/RealOutputs/Vision/*/sending frames")
        self.assertEqual(pattern.expand(CAMERAS), [
            "/RealOutputs/Vision/BCH/sending frames",
            "/RealOutputs/Vision/BCL/sending frames",
            "/RealOutputs/Vision/BL/sending frames",
            "/RealOutputs/Vision/BR/sending frames",
        ])

    def test_star_does_not_cross_a_slash(self):
        # The failure mode plain fnmatch would have: * swallowing separators.
        pattern = EntryPattern("/RealOutputs/*/state")
        self.assertTrue(pattern.matches("/RealOutputs/LEDS/state"))
        self.assertFalse(pattern.matches("/RealOutputs/a/b/state"))

    def test_double_star_spans_segments(self):
        pattern = EntryPattern("/RealOutputs/**/state")
        self.assertTrue(pattern.matches("/RealOutputs/LEDS/state"))
        self.assertTrue(pattern.matches("/RealOutputs/a/b/c/state"))
        self.assertTrue(pattern.matches("/RealOutputs/state"))
        self.assertFalse(pattern.matches("/Other/a/state"))

    def test_question_mark_and_character_class(self):
        self.assertTrue(EntryPattern("/a/?/b").matches("/a/x/b"))
        self.assertFalse(EntryPattern("/a/?/b").matches("/a/xy/b"))
        self.assertTrue(EntryPattern("/a/[xy]/b").matches("/a/y/b"))
        self.assertFalse(EntryPattern("/a/[xy]/b").matches("/a/z/b"))

    def test_partial_segment_wildcard(self):
        pattern = EntryPattern("/Drivetrain/*/DriveTempCelsius")
        self.assertTrue(pattern.matches("/Drivetrain/FL/DriveTempCelsius"))
        self.assertFalse(pattern.matches("/Drivetrain/FL/SteerTempCelsius"))


class AncestorCaptureTest(unittest.TestCase):
    """could_contain drives ingest capture, before struct flattening exists."""

    def test_struct_parent_is_captured_for_a_leaf_pattern(self):
        leaf = "/RealOutputs/DriveToReef/difference (reef frame)/translation/y"
        pattern = EntryPattern(leaf)
        self.assertTrue(pattern.could_contain("/RealOutputs/DriveToReef/difference (reef frame)"))
        self.assertTrue(pattern.could_contain("/RealOutputs/DriveToReef"))
        self.assertTrue(pattern.could_contain(leaf))

    def test_mid_segment_prefix_is_not_an_ancestor(self):
        # The accidental over-capture the old substring rule produced.
        pattern = EntryPattern(
            "/RealOutputs/DriveToReef/difference (reef frame)/translation/y")
        self.assertFalse(pattern.could_contain("/RealOutputs/DriveToReef/difference"))

    def test_unrelated_entries_are_not_captured(self):
        pattern = EntryPattern("/RealOutputs/LEDS/state")
        self.assertFalse(pattern.could_contain("/RealOutputs/Other/state"))


class CaptureTest(unittest.TestCase):

    def test_captures_the_matched_text(self):
        pattern = EntryPattern("/RealOutputs/Vision/*/sending frames")
        self.assertEqual(pattern.captures("/RealOutputs/Vision/BCH/sending frames"), ("BCH",))
        self.assertIsNone(pattern.captures("/RealOutputs/Vision/BCH/UpdatePoseCount"))

    def test_group_by_captures(self):
        pattern = EntryPattern("/RealOutputs/Vision/*/sending frames")
        self.assertEqual(
            pattern.group_by_captures(CAMERAS),
            {("BCH",): "/RealOutputs/Vision/BCH/sending frames",
             ("BCL",): "/RealOutputs/Vision/BCL/sending frames",
             ("BL",): "/RealOutputs/Vision/BL/sending frames",
             ("BR",): "/RealOutputs/Vision/BR/sending frames"},
        )


class SubstituteTest(unittest.TestCase):
    """substitute() is the inverse of captures(), used to name a missing entry."""

    def test_round_trips_with_captures(self):
        pattern = EntryPattern("/RealOutputs/Vision/*/sending frames")
        for name in CAMERAS[:4]:
            self.assertEqual(pattern.substitute(pattern.captures(name)), name)

    def test_names_an_entry_that_is_not_in_the_log(self):
        pattern = EntryPattern("/RealOutputs/Vision/*/sending frames")
        self.assertEqual(pattern.substitute(("FRONT",)),
                         "/RealOutputs/Vision/FRONT/sending frames")

    def test_double_star_capture_carries_its_own_separators(self):
        pattern = EntryPattern("/RealOutputs/**/Connected")
        name = "/RealOutputs/a/b/Connected"
        self.assertEqual(pattern.substitute(pattern.captures(name)), name)

    def test_multiple_wildcards_fill_in_order(self):
        pattern = EntryPattern("/Drivetrain/*/*Temp")
        self.assertEqual(pattern.substitute(("FL", "Drive")), "/Drivetrain/FL/DriveTemp")


class ExpandRolesTest(unittest.TestCase):

    def test_literal_analysis_is_returned_unchanged(self):
        roles = {"startEntry": "/a/b", "endEntry": "/c/d"}
        self.assertEqual(expand_roles(roles, CAMERAS), [roles])

    def test_same_pattern_in_both_roles_pairs_per_camera(self):
        roles = {"startEntry": "/RealOutputs/Vision/*/sending frames",
                 "endEntry": "/RealOutputs/Vision/*/sending frames"}
        expansions = expand_roles(roles, CAMERAS)
        self.assertEqual(len(expansions), 4)
        for expansion in expansions:
            # Paired by capture, never crossed between cameras.
            self.assertEqual(expansion["startEntry"], expansion["endEntry"])
        self.assertEqual(expansions[0]["startEntry"],
                         "/RealOutputs/Vision/BCH/sending frames")

    def test_literal_role_repeats_across_expansions(self):
        roles = {"entry": "/RealOutputs/Vision/*/sending frames",
                 "triggerEntry": "/RealOutputs/LEDS/state"}
        expansions = expand_roles(roles, CAMERAS + ["/RealOutputs/LEDS/state"])
        self.assertEqual(len(expansions), 4)
        self.assertTrue(all(e["triggerEntry"] == "/RealOutputs/LEDS/state"
                            for e in expansions))

    def test_different_patterns_pair_by_capture(self):
        names = CAMERAS + [
            "/RealOutputs/Vision/BCH/UpdatePoseCount",
            "/RealOutputs/Vision/BR/UpdatePoseCount",
        ]
        roles = {"startEntry": "/RealOutputs/Vision/*/sending frames",
                 "endEntry": "/RealOutputs/Vision/*/UpdatePoseCount"}
        expansions = expand_roles(roles, names)
        paired = {e["startEntry"]: e["endEntry"] for e in expansions}
        self.assertEqual(paired["/RealOutputs/Vision/BCH/sending frames"],
                         "/RealOutputs/Vision/BCH/UpdatePoseCount")
        # BCL has no UpdatePoseCount: the pattern is kept so it reports missing.
        self.assertEqual(paired["/RealOutputs/Vision/BCL/sending frames"],
                         "/RealOutputs/Vision/*/UpdatePoseCount")

    def test_pattern_matching_nothing_yields_no_expansions(self):
        roles = {"startEntry": "/RealOutputs/Nope/*/x"}
        self.assertEqual(expand_roles(roles, CAMERAS), [])

    def test_expansion_order_is_deterministic(self):
        roles = {"startEntry": "/RealOutputs/Vision/*/sending frames"}
        first = [e["startEntry"] for e in expand_roles(roles, CAMERAS)]
        second = [e["startEntry"] for e in expand_roles(roles, list(reversed(CAMERAS)))]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))


if __name__ == "__main__":
    unittest.main(verbosity=2)
