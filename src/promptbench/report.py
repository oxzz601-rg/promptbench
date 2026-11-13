"""Markdown, HTML and JSON reporting plus baseline comparison."""

from __future__ import annotations

import html
import json
import pathlib

from .core import RunSummary


def compare(baseline, current):
    """Diff two runs case by case. Regressions are cases that used to pass."""
    if not isinstance(baseline, RunSummary):
        baseline = RunSummary.from_dict(baseline)
    before = {case.case_id: case for case in baseline.cases}
    after = {case.case_id: case for case in current.cases}
    regressions, fixed, changed = [], [], []
    for case_id, now in after.items():
        old = before.get(case_id)
        if old is None:
            continue
        if old.passed and not now.passed:
            regressions.append({"id": case_id, "before": round(old.score, 4), "after": round(now.score, 4)})
        elif not old.passed and now.passed:
            fixed.append(case_id)
        delta = now.score - old.score
        if abs(delta) >= 0.01:
            changed.append({"id": case_id, "delta": round(delta, 4), "after": round(now.score, 4)})
    changed.sort(key=lambda item: item["delta"])
    return {
        "regressions": regressions,
        "fixed": fixed,
        "changed": changed,
        "added": [case_id for case_id in after if case_id not in before],
        "removed": [case_id for case_id in before if case_id not in after],
        "pass_rate_before": round(baseline.pass_rate, 4),
        "pass_rate_after": round(current.pass_rate, 4),
    }


def markdown(summary, comparison=None):
    lines = [
        "## {}: {}/{} passed ({:.0%})".format(summary.suite, summary.passed, summary.total, summary.pass_rate),
        "",
        "provider `{}` | mean score {:.3f} | {:.1f}s total{}".format(
            summary.provider,
            summary.mean_score,
            summary.duration_s,
            " | {} tokens".format(summary.total_tokens) if summary.total_tokens else "",
        ),
        "",
        "| result | case | score | stability | latency | tags |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in summary.cases:
        lines.append(
            "| {} | `{}` | {:.2f} | {:.0%} | {:.0f}ms | {} |".format(
                "pass" if case.passed else "FAIL",
                case.case_id,
                case.score,
                case.stability,
                case.latency_ms,
                ", ".join(case.tags) or "-",
            )
        )
    tags = summary.by_tag()
    if len(tags) > 1 or "untagged" not in tags:
        lines += ["", "### By tag", "", "| tag | passed | score |", "| --- | --- | --- |"]
        for tag, stats in tags.items():
            lines.append("| {} | {}/{} | {:.2f} |".format(tag, stats["passed"], stats["total"], stats["mean_score"]))
    failures = summary.failed_cases
    if failures:
        lines += ["", "### Failures", ""]
        for case in failures:
            lines.append("**`{}`**".format(case.case_id))
            if case.error:
                lines.append("- error: `{}`".format(case.error))
            failed_runs = [run for run in case.runs if not run.passed] or list(case.runs)
            run = failed_runs[0]
            for detail in run.details:
                if detail["score"] < 1.0:
                    lines.append("- {} (score {:.2f}): {}".format(detail["scorer"], detail["score"], detail["detail"]))
            if run.output:
                snippet = run.output.replace("\n", " ")
                lines.append("- reply: `{}`".format(snippet[:220] + ("..." if len(snippet) > 220 else "")))
            lines.append("")
    if comparison:
        lines += ["", "### Versus baseline", ""]
        lines.append("pass rate {:.0%} -> {:.0%}".format(comparison["pass_rate_before"], comparison["pass_rate_after"]))
        if comparison["regressions"]:
            lines.append("")
            lines.append("Regressions (these passed before):")
            for item in comparison["regressions"]:
                lines.append("- `{}`: {:.2f} -> {:.2f}".format(item["id"], item["before"], item["after"]))
        elif comparison["added"] or comparison["removed"] or comparison["changed"]:
            lines.append("No regressions.")
        if comparison["fixed"]:
            lines.append("Fixed: " + ", ".join("`{}`".format(case_id) for case_id in comparison["fixed"]))
        if comparison["added"]:
            lines.append("New cases: " + ", ".join("`{}`".format(case_id) for case_id in comparison["added"]))
        if comparison["removed"]:
            lines.append("Removed cases: " + ", ".join("`{}`".format(case_id) for case_id in comparison["removed"]))
    return "\n".join(lines) + "\n"


def html_report(summary, comparison=None):
    rows = []
    for case in summary.cases:
        issues = "; ".join(
            "{}: {}".format(detail["scorer"], detail["detail"]) for detail in (case.runs[0].details if case.runs else ()) if detail["score"] < 1.0
        )
        rows.append(
            "<tr class=\"{}\"><td>{}</td><td><code>{}</code></td><td>{:.2f}</td><td>{:.0%}</td><td>{:.0f}ms</td><td>{}</td></tr>".format(
                "pass" if case.passed else "fail",
                "pass" if case.passed else "FAIL",
                html.escape(case.case_id),
                case.score,
                case.stability,
                case.latency_ms,
                html.escape(issues or case.error or ""),
            )
        )
    banner = ""
    if comparison:
        if comparison["regressions"]:
            banner = "<p class=\"fail\">{} regression(s) against the baseline: {}</p>".format(
                len(comparison["regressions"]), html.escape(", ".join(item["id"] for item in comparison["regressions"]))
            )
        else:
            banner = "<p class=\"pass\">No regressions against the baseline.</p>"
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>promptbench: {suite}</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:60rem;color:#1c1e21;padding:0 1rem}}
code{{background:#f2f3f5;padding:.1rem .3rem;border-radius:3px}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}
td,th{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #e4e6eb;vertical-align:top}}
tr.fail td:first-child{{color:#b00020;font-weight:600}}
tr.pass td:first-child{{color:#1a7f37}}
.fail{{color:#b00020}}.pass{{color:#1a7f37}}
.meta{{color:#606770}}
</style></head><body>
<h1>{suite}</h1>
<p class="meta">provider <code>{provider}</code> | {passed}/{total} passed ({rate:.0%}) | mean score {score:.3f} | {duration:.1f}s</p>
{banner}
<table><thead><tr><th>result</th><th>case</th><th>score</th><th>stability</th><th>latency</th><th>notes</th></tr></thead>
<tbody>{rows}</tbody></table>
<p class="meta">Generated by promptbench at {started}.</p>
</body></html>
""".format(
        suite=html.escape(summary.suite),
        provider=html.escape(summary.provider),
        passed=summary.passed,
        total=summary.total,
        rate=summary.pass_rate,
        score=summary.mean_score,
        duration=summary.duration_s,
        banner=banner,
        rows="\n".join(rows),
        started=html.escape(summary.started_at),
    )


def write_report(directory, summary, comparison=None):
    target = pathlib.Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    payload = summary.to_dict()
    if comparison:
        payload["comparison"] = comparison
    paths = {
        "json": target / "run.json",
        "markdown": target / "report.md",
        "html": target / "report.html",
    }
    paths["json"].write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    paths["markdown"].write_text(markdown(summary, comparison), encoding="utf-8")
    paths["html"].write_text(html_report(summary, comparison), encoding="utf-8")
    return {key: str(value) for key, value in paths.items()}
