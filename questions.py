#! /usr/bin/env python3
"""Keep the questions the team wants the logs to answer, and check they can be.

    ./questions.py check questions2026.json ./logs              # the 10 newest logs
    ./questions.py check questions2027.json ./logs --since 2027-01-10
    ./questions.py answer questions2026.json ./logs             # every answer, for a summary

A questions file lists each question in words, who asked it, and the season.py
command that answers it:

    {
      "season": 2027,
      "questions": [
        {"question": "How far does the robot drive per match?",
         "askedBy": "Geoff",
         "ask": "measure increase /Drivetrain/DistanceMeters"}
      ]
    }

`check` runs every question against recent logs and says which can be answered
yet. The point is to learn in the first practice session, not after the season,
that something is not logged, or is logged but never changes. The 2026 season
had both: the power distribution panel's totals were never filled in, and a
drivetrain speed entry was 0 all year. Statuses:

- **ready**: the entry is in the logs, changes, and gives an answer.
- **check**: it is there, but something looks wrong - it never changed (one
  value per log, often a value nothing ever sets), it gave 0 in every log, or it
  held no value while the question counts (the robot was never enabled?).
- **missing**: no entry matches. The robot code does not log it yet, or the name
  differs; the nearest names are suggested.
- **invalid**: the `ask` is not a season.py command.

`check` exits 1 when any question is missing or invalid. `answer` runs each
question as season.py would, over the logs each one asks for.
"""

import argparse
import contextlib
import io
import json
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import season
from log_index import NUMERIC_SCALAR_TYPES, Manifest, default_index_path
from season import LogInfo, Reader, describe_selection, number_text, select_logs

__all__ = ["Question", "Verdict", "load_questions", "check_question", "main"]

STATUS_ORDER = ("invalid", "missing", "check", "ready")


@dataclass
class Question:
    text: str
    ask: str
    asked_by: Optional[str] = None
    args: Optional[argparse.Namespace] = None
    problem: Optional[str] = None   # why the ask could not be parsed


@dataclass
class Verdict:
    status: str
    details: List[str] = field(default_factory=list)


def parse_ask(ask: str) -> Tuple[Optional[argparse.Namespace], Optional[str]]:
    """The season.py command in an `ask`, or None and what is wrong with it."""
    try:
        tokens = shlex.split(ask)
    except ValueError as error:
        return None, str(error)
    if not tokens:
        return None, "no command"
    if tokens[0] in ("season.py", "./season.py", "python", "python3"):
        return None, "give only what follows the log folder, e.g. \"measure max /X\""
    errors = io.StringIO()
    try:
        with contextlib.redirect_stderr(errors):
            return season.build_parser().parse_args(["-", *tokens]), None
    except SystemExit:
        lines = [line for line in errors.getvalue().splitlines() if line.strip()]
        message = lines[-1] if lines else "not a season.py command"
        return None, message.split("error: ", 1)[-1]


def load_questions(path: str) -> List[Question]:
    with open(path) as handle:
        data = json.load(handle)
    items = data.get("questions", []) if isinstance(data, dict) else data
    questions = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            questions.append(Question(f"question {number}", "", problem="not an object"))
            continue
        text = item.get("question") or f"question {number}"
        ask = item.get("ask") or ""
        question = Question(text, ask, item.get("askedBy"))
        if not ask:
            question.problem = "has no \"ask\""
        else:
            question.args, question.problem = parse_ask(ask)
        questions.append(question)
    return questions


def recent_logs(index, latest: Optional[int], since: Optional[str]) -> List[LogInfo]:
    """The logs to check against: the newest, or those since a date."""
    logs = [log for log in select_logs(index, "all") if log.recorded_at]
    if since:
        return [log for log in logs if log.recorded_at >= since]
    if latest:
        return logs[-latest:]
    return logs


def _is_numeric(manifest: Manifest, name: str) -> bool:
    """A number, or a struct field (whose type the manifest does not record)."""
    types = manifest.types.get(name, set())
    return any(t is None or t in NUMERIC_SCALAR_TYPES for t in types)


def check_question(question: Question, index, logs: Sequence[LogInfo],
                   manifest: Manifest, reader: Reader) -> Verdict:
    """Whether recent logs can answer a question, and if not, why."""
    if question.problem:
        return Verdict("invalid", [question.problem])
    args = question.args
    fields = manifest.matching(args.entry)
    if not fields:
        close = manifest.suggestions(args.entry)
        detail = f"no entry matches {args.entry} in these {manifest.log_count} logs"
        if close:
            detail += "; nearest: " + ", ".join(close)
        return Verdict("missing", [detail])

    details, worries = [], []
    present = set().union(*(manifest.logs[name] for name in fields))
    coverage = f"in {len(present)} of {manifest.log_count} logs"
    if len(fields) > 1:
        coverage = f"{len(fields)} entries, " + coverage
    details.append(coverage)

    # AdvantageKit writes a value only when it changes, so one record per log
    # means the value never moved: a sensor never read, or a total never added
    # to. Only numbers are judged: a boolean that held false all match (no
    # brownout) or a string that never changed is ordinary.
    numeric = [name for name in fields if _is_numeric(manifest, name)]
    frozen = [name for name in numeric
              if manifest.records.get(name, 0) <= len(manifest.logs[name])]
    if frozen:
        shown = frozen[0] if len(frozen) == 1 else f"{len(frozen)} of them"
        worries.append(f"never changed: {shown} held one value per log; is it being "
                       f"set?")

    if args.command == "measure":
        gate = args.gate or "enabled"
        result = season.measure(index, logs, args.kind, args.entry, gate,
                                season.condition_from(args), reader,
                                combined=args.combine)
        values = [value.value for value in result.per_match.values()
                  if value.value is not None]
        if not values:
            worries.append(f"no value while {gate} in these logs (was the robot "
                           f"{'enabled' if gate == 'enabled' else 'in that mode'}?)")
        else:
            if all(value == 0 for value in values) and numeric and not frozen:
                worries.append("0 in every log")
            details.append(f"{args.kind}: {number_text(sum(values) / len(values))} per "
                           f"log on average (lowest {number_text(min(values))}, highest "
                           f"{number_text(max(values))})")
    elif args.command == "find":
        episodes = season.find(index, logs, args.entry, season.condition_from(args),
                               args.gate or "always", reader)
        details.append(f"found {len(episodes)} time{'s' if len(episodes) != 1 else ''} "
                       f"in these logs")
    if reader.unreadable:
        worries.append(f"{len(reader.unreadable)} logs could not be read for it "
                       f"(--read-only?)")
        reader.unreadable.clear()
    return Verdict("check" if worries else "ready", worries + details)


def _check(args: argparse.Namespace, questions: List[Question], index) -> int:
    logs = recent_logs(index, None if args.since else args.latest, args.since)
    if not logs:
        print("No dated logs to check against" + (f" since {args.since}" if args.since
                                                   else ""))
        return 1
    if args.since:
        which = f"the {len(logs)} logs since {args.since}"
    else:
        which = f"the {len(logs)} newest logs"
    span = f"{logs[0].recorded_at[:10]} to {logs[-1].recorded_at[:10]}"
    print(f"{args.questions}: {len(questions)} questions, checked against {which} "
          f"({span})\n")
    manifest = Manifest(index, [log.file_id for log in logs])
    reader = Reader(index, read_only=args.read_only, quiet=True)
    counts: Dict[str, int] = {status: 0 for status in STATUS_ORDER}
    for question in questions:
        verdict = check_question(question, index, logs, manifest, reader)
        counts[verdict.status] += 1
        print(f"{verdict.status:8} {question.text}"
              + (f"  ({question.asked_by})" if question.asked_by else ""))
        if question.ask:
            print(f"{'':8} {question.ask}")
        for detail in verdict.details:
            print(f"{'':8} {detail}")
        print()
    print(", ".join(f"{n} {status}" for status, n in counts.items() if n))
    return 1 if counts["invalid"] or counts["missing"] else 0


def _answer(args: argparse.Namespace, questions: List[Question], index) -> int:
    failed = 0
    for number, question in enumerate(questions, 1):
        print(f"{'=' * 78}\n{number}. {question.text}"
              + (f"  ({question.asked_by})" if question.asked_by else ""))
        print(f"   season.py {args.log_folder} {question.ask}\n")
        if question.problem:
            print(f"Cannot ask it: {question.problem}\n")
            failed += 1
            continue
        if args.read_only:
            question.args.read_only = True
        sys.stdout.flush()  # keep the "storing" note on stderr in order with answers
        if season.run(question.args, index) != 0:
            failed += 1
        print()
    return 1 if failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep the questions the team wants the logs to answer, and check "
                    "that recent logs can answer them.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("check", "check which questions recent logs can answer"),
                       ("answer", "answer every question from the season's logs")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("questions", help="questions JSON file")
        sub.add_argument("log_folder", help="folder the index was built for")
        sub.add_argument("--index", metavar="PATH",
                         help="index file (default: .log-index.sqlite in the log folder)")
        sub.add_argument("--read-only", action="store_true",
                         help="never store samples in the index")
        if name == "check":
            sub.add_argument("--latest", type=int, default=10, metavar="N",
                             help="check against the N newest logs (default 10)")
            sub.add_argument("--since", metavar="YYYY-MM-DD",
                             help="check against every log recorded since this date")
    args = parser.parse_args(argv)

    try:
        questions = load_questions(args.questions)
    except (OSError, json.JSONDecodeError) as error:
        print(f"Error loading {args.questions}: {error}", file=sys.stderr)
        return 2
    index = season.open_index(args.index or default_index_path(args.log_folder),
                              args.log_folder)
    if index is None:
        return 2
    try:
        if args.command == "check":
            return _check(args, questions, index)
        return _answer(args, questions, index)
    finally:
        index.close()


if __name__ == "__main__":
    sys.exit(main())
