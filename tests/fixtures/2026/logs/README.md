# 2026 test logs: do not delete

The `.wpilog` files here are test fixtures. The goldens in `../golden/` were
recorded from exactly these files, and `../manifest.json` lists them. Most of
the test suite skips or fails without them. They are too large for git, so
keep a copy elsewhere. See [tests/README.md](../../../README.md#log-fixtures).

Don't add other logs here either: an unexpected file changes the output, and
the suite reports it.
