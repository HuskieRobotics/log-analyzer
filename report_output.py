#! /usr/bin/env python3
"""Machine-readable and screen-readable renderings of an analysis run.

The analyzer computes results as plain dataclasses (see analysis.py), and this
module turns a collected run into the two outputs that are not terminal text:

    render_json  - the whole run, for scripting, archiving and any later UI
    render_html  - one self-contained page for the pit screen

The HTML is deliberately dumb. Its data is **baked into the page**, not fetched:
browsers block fetch/XHR against file:// origins, so a page that tried to load a
sibling report.json would fail silently on exactly the setup this is for - a file
opened from disk with no server. Baking it in also makes each report a single
archivable artifact.

The page carries a meta refresh, so rewriting the same file in place is all that
is needed to update a screen showing it. There is no JavaScript and nothing is
loaded from the network, so it works offline in a pit with no internet.

This module deliberately does not import from analysis.py: it treats the computed
result objects as opaque and serialises them with dataclasses.asdict, which keeps
the dependency one-way and avoids a cycle.
"""

import html
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

__all__ = ["ReportSection", "FileReport", "Report", "render_json", "render_html"]

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class ReportSection:
    """One analysis, either for a single file or aggregated across files."""
    kind: str                 # "time" or "value"
    title: str                # the same line the terminal prints after "Analyzing: "
    unit: str
    result: Any               # AnalysisResult
    counts: Any = None        # PerFileCounts, aggregate sections only


@dataclass
class FileReport:
    """Everything produced for one log file."""
    name: str
    findings: List[Any] = field(default_factory=list)
    sections: List[ReportSection] = field(default_factory=list)


@dataclass
class Report:
    """A whole run: what was analyzed, and everything it produced."""
    log_folder: str
    config_path: str
    filters: Dict[str, Any] = field(default_factory=dict)
    selection: str = ""
    still_writing: List[str] = field(default_factory=list)
    non_matches: List[str] = field(default_factory=list)
    files: List[FileReport] = field(default_factory=list)
    aggregate_findings: List[Any] = field(default_factory=list)
    aggregate_sections: List[ReportSection] = field(default_factory=list)
    generated_at: str = ""

    def severity_counts(self) -> Dict[str, int]:
        """How many aggregate findings of each severity."""
        counts = {"error": 0, "warning": 0, "info": 0}
        for finding in self.aggregate_findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts


def _plain(value: Any) -> Any:
    """Convert dataclasses and tuples into JSON-friendly structures."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def render_json(report: Report, indent: int = 2) -> str:
    """Serialise a whole run as JSON."""
    return json.dumps(_plain(report), indent=indent, default=str) + "\n"


# === HTML ====================================================================

STYLE = """
:root {
  --bg: #14161a; --panel: #1d2026; --line: #2c313a;
  --text: #e8eaed; --muted: #9aa3af;
  --error: #ff5c5c; --warning: #ffb454; --info: #6fb3ff; --ok: #4ade80;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 0 0 3rem; background: var(--bg); color: var(--text);
       font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { padding: 1.25rem 1.5rem; border-bottom: 1px solid var(--line); }
h1 { margin: 0 0 .25rem; font-size: 1.35rem; }
.meta { color: var(--muted); font-size: .85rem; }
.verdict { display: flex; gap: 1rem; flex-wrap: wrap; padding: 1.25rem 1.5rem; }
.tile { flex: 1 1 180px; padding: 1rem 1.25rem; border-radius: 10px;
        background: var(--panel); border-left: 6px solid var(--line); }
.tile .n { font-size: 2.6rem; font-weight: 700; line-height: 1; }
.tile .k { color: var(--muted); text-transform: uppercase; letter-spacing: .08em;
           font-size: .75rem; margin-top: .35rem; }
.tile.error { border-left-color: var(--error); } .tile.error .n { color: var(--error); }
.tile.warning { border-left-color: var(--warning); } .tile.warning .n { color: var(--warning); }
.tile.info { border-left-color: var(--info); } .tile.info .n { color: var(--info); }
.tile.ok { border-left-color: var(--ok); } .tile.ok .n { color: var(--ok); }
main { padding: 0 1.5rem; }
section { margin: 1.75rem 0; }
h2 { font-size: 1rem; text-transform: uppercase; letter-spacing: .08em;
     color: var(--muted); border-bottom: 1px solid var(--line);
     padding-bottom: .4rem; margin: 0 0 .9rem; }
.finding { background: var(--panel); border-left: 5px solid var(--line);
           border-radius: 8px; padding: .7rem .9rem; margin-bottom: .55rem; }
.finding.error { border-left-color: var(--error); }
.finding.warning { border-left-color: var(--warning); }
.finding.info { border-left-color: var(--info); }
.rule { font-weight: 600; }
.entry { color: var(--muted); font-size: .85rem; word-break: break-all; }
.detail { margin-top: .3rem; }
.where { color: var(--muted); font-size: .82rem; margin-top: .2rem; }
.none { color: var(--ok); }
.notice { margin: 0 1.5rem 1rem; padding: .7rem .9rem; border-radius: 8px;
          background: var(--panel); border-left: 5px solid var(--info);
          font-size: .9rem; }
.notice .k { color: var(--muted); text-transform: uppercase;
             letter-spacing: .08em; font-size: .72rem; }
.notice .f { word-break: break-all; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; font-size: .8rem; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
details { background: var(--panel); border-radius: 8px; padding: .6rem .9rem;
          margin-bottom: .5rem; }
summary { cursor: pointer; font-weight: 600; }
.analysis-title { color: var(--muted); font-size: .85rem; word-break: break-all;
                  margin: .8rem 0 .3rem; }
"""


def _e(value: Any) -> str:
    """Escape a value for HTML. Alert text is arbitrary and must not be trusted."""
    return html.escape(str(value), quote=True)


def _finding_html(finding: Any, is_aggregate: bool) -> str:
    where = []
    if getattr(finding, "occurrences", 1) > 1:
        where.append(f"x{finding.occurrences}")
    if is_aggregate and getattr(finding, "file_count", 1) > 1:
        where.append(f"in {finding.file_count} files")
    if finding.timestamp is not None:
        where.append(("first @ " if where else "@ ") + f"{finding.timestamp:.3f} s")
        if is_aggregate:
            where.append(f"in {finding.log_file_name}")
    where_html = (f'<div class="where">{_e(", ".join(where))}</div>') if where else ""
    return (
        f'<div class="finding {_e(finding.severity)}">'
        f'<div class="rule">{_e(finding.rule_name)}</div>'
        f'<div class="entry">{_e(finding.entry)}</div>'
        f'<div class="detail">{_e(finding.detail)}</div>'
        f"{where_html}</div>"
    )


def _findings_html(findings: List[Any], is_aggregate: bool) -> str:
    if not findings:
        return '<p class="none">No concerns found.</p>'
    ordered = sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9),
                                              f.rule_name, f.entry, f.detail))
    return "".join(_finding_html(f, is_aggregate) for f in ordered)


def _calc_rows(result: Any) -> str:
    rows = []
    for calc in getattr(result, "calculations", []):
        if getattr(calc, "unknown_type", False):
            rows.append(f"<tr><td>{_e(calc.calc_type)}</td>"
                        f'<td class="num">unknown calculation</td><td></td></tr>')
            continue
        if calc.error:
            rows.append(f"<tr><td>{_e(calc.name)}</td>"
                        f'<td class="num">{_e(calc.error)}</td><td></td></tr>')
            continue
        if calc.calc_type in ("outlier_2std", "abs_outlier_2std"):
            # An outlier calculation carries no single value: it yields a row per
            # outlier, and none at all when the data is well behaved. Saying so
            # explicitly is more use on a screen than a silently absent row.
            if not calc.outliers:
                rows.append(f"<tr><td>{_e(calc.name)}</td>"
                            f'<td class="num">none</td><td></td></tr>')
            for value, locations in calc.outliers:
                where = ", ".join(f"{loc.timestamp:.3f} s in {loc.log_file_name}"
                                  for loc in locations)
                rows.append(f"<tr><td>{_e(calc.name)}</td>"
                            f'<td class="num">{value:.6f} {_e(calc.unit)}</td>'
                            f"<td>{_e(where)}</td></tr>")
            continue
        if calc.value is None:
            rows.append(f"<tr><td>{_e(calc.name)}</td>"
                        f'<td class="num">n/a</td><td></td></tr>')
            continue
        if calc.calc_type == "count":
            shown = _e(calc.value)
        else:
            shown = f"{calc.value:.6f} {_e(calc.unit)}"
        where = ", ".join(f"{loc.timestamp:.3f} s in {loc.log_file_name}"
                          for loc in calc.locations)
        rows.append(f"<tr><td>{_e(calc.name)}</td>"
                    f'<td class="num">{shown}</td><td>{_e(where)}</td></tr>')
    if not rows:
        return ""
    return ("<table><tr><th>Calculation</th><th>Value</th><th>Where</th></tr>"
            + "".join(rows) + "</table>")


def _sections_html(sections: List[ReportSection]) -> str:
    if not sections:
        return '<p class="none">None configured.</p>'
    parts = []
    for section in sections:
        body = ""
        if section.counts is not None and section.counts.show_detail:
            counts = section.counts
            body += (f'<table><tr><th>Files</th><th>Average</th>'
                     f"<th>Fewest</th><th>Most</th></tr><tr>"
                     f'<td class="num">{counts.files_processed}</td>'
                     f'<td class="num">{counts.average:.2f}</td>'
                     f'<td class="num">{counts.min_count} ({_e(counts.min_file)})</td>'
                     f'<td class="num">{counts.max_count} ({_e(counts.max_file)})</td>'
                     f"</tr></table>")
        if not getattr(section.result, "has_values", False):
            body += '<p class="entry">No values found.</p>'
        elif not getattr(section.result, "has_numeric", False):
            body += (f'<p class="entry">{section.result.total_count} value(s) '
                     f"captured; none numeric.</p>")
        else:
            body += (f'<p class="entry">{section.result.total_count} value(s) '
                     f"captured.</p>" + _calc_rows(section.result))
        parts.append(f'<div class="analysis-title">{_e(section.title)}</div>{body}')
    return "".join(parts)


def render_html(report: Report, refresh_seconds: int = 30) -> str:
    """Render a whole run as one self-contained page.

    Args:
        report: The collected run
        refresh_seconds: Meta-refresh interval; 0 disables it

    Returns:
        Complete HTML, with all data inlined and nothing loaded externally
    """
    counts = report.severity_counts()
    tiles = []
    if not report.aggregate_findings:
        tiles.append('<div class="tile ok"><div class="n">0</div>'
                     '<div class="k">concerns</div></div>')
    else:
        for severity in ("error", "warning", "info"):
            if counts.get(severity):
                tiles.append(f'<div class="tile {severity}">'
                             f'<div class="n">{counts[severity]}</div>'
                             f'<div class="k">{severity}s</div></div>')
    tiles.append(f'<div class="tile"><div class="n">{len(report.files)}</div>'
                 f'<div class="k">log files</div></div>')

    refresh = (f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
               if refresh_seconds else "")

    # State the fact and name the file; do not guess whether it is a match. An
    # AdvantageKit name usually carries the event and match number, so the reader
    # can tell at a glance - and is better placed to than the tool.
    bands = []
    if report.still_writing:
        bands.append((
            "Newer log still being written" if len(report.still_writing) == 1
            else f"{len(report.still_writing)} newer logs still being written",
            report.still_writing))
    if report.non_matches:
        bands.append((
            "Not a match, skipped" if len(report.non_matches) == 1
            else f"{len(report.non_matches)} logs skipped, not matches",
            report.non_matches))
    notice = "".join(
        f'<div class="notice"><div class="k">{_e(label)}</div>'
        + "".join(f'<div class="f">{_e(name)}</div>' for name in names)
        + "</div>"
        for label, names in bands)

    per_file = []
    for entry in report.files:
        per_file.append(
            f"<details><summary>{_e(entry.name)}</summary>"
            f"<h2>Checks</h2>{_findings_html(entry.findings, False)}"
            f"<h2>Analyses</h2>{_sections_html(entry.sections)}"
            f"</details>")

    filters = ", ".join(f"{key}: {value}" for key, value in report.filters.items())
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
<title>Log report - {_e(report.log_folder)}</title>
<style>{STYLE}</style></head><body>
<header>
  <h1>Match log report</h1>
  <div class="meta">{_e(report.selection or report.log_folder)}
    &#183; {_e(report.log_folder)} &#183; {_e(report.config_path)}
    &#183; {_e(filters)} &#183; generated {_e(report.generated_at)}</div>
</header>
<div class="verdict">{"".join(tiles)}</div>
{notice}
<main>
  <section><h2>Concerns across all files</h2>
    {_findings_html(report.aggregate_findings, True)}</section>
  <section><h2>Aggregated analyses</h2>
    {_sections_html(report.aggregate_sections)}</section>
  <section><h2>Per file</h2>{"".join(per_file)}</section>
</main>
</body></html>
"""


def now_text() -> str:
    """Human-readable generation time; separated so tests can pin it."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
