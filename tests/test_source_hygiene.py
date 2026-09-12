#! /usr/bin/env python3
"""Guards against source-level problems that only bite on some interpreters.

An invalid escape sequence is a DeprecationWarning before Python 3.12 and a
SyntaxWarning from 3.12 on, so a docstring containing a Windows path compiles
silently on an older interpreter and prints a warning on a newer one. That is
exactly the kind of thing to find here rather than on the pit laptop.
"""

import glob
import sys
import unittest
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def python_sources():
    return sorted(glob.glob(str(REPO_ROOT / "*.py"))
                  + glob.glob(str(REPO_ROOT / "tests" / "*.py")))


class EscapeSequenceTest(unittest.TestCase):

    def test_no_module_has_an_invalid_escape_sequence(self):
        offenders = []
        for path in python_sources():
            source = Path(path).read_text(encoding="utf-8")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                compile(source, path, "exec")
            offenders += [f"{Path(path).name}:{w.lineno}: {w.message}"
                          for w in caught if "escape" in str(w.message)]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_every_module_compiles(self):
        for path in python_sources():
            with self.subTest(module=Path(path).name):
                compile(Path(path).read_text(encoding="utf-8"), path, "exec")

    def test_the_source_set_is_not_empty(self):
        """A silent empty glob would make the checks above vacuous."""
        self.assertGreater(len(python_sources()), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
