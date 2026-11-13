"""Suite loading and validation.

A suite is a JSON (or YAML) document:

    {"name": "support-bot", "defaults": {"threshold": 1.0},
     "cases": [{"id": "greets", "prompt": "Say hi", "expect": {"contains": "hi"}}]}
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

DEFAULT_THRESHOLD = 1.0


class SuiteError(ValueError):
    """A suite file is malformed."""


@dataclass(frozen=True)
class Case:
    id: str
    prompt: str
    scorers: tuple = ()
    tags: tuple = ()
    system: str = None
    repeats: int = 1
    threshold: float = DEFAULT_THRESHOLD


@dataclass(frozen=True)
class Suite:
    name: str
    cases: tuple = ()
    defaults: dict = field(default_factory=dict)
    source: str = None

    def case(self, case_id):
        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(case_id)


_EXPECT = {
    "text": ("exact", "value"),
    "equals": ("exact", "value"),
    "contains": ("contains", "value"),
    "not_contains": ("not_contains", "value"),
    "regex": ("regex", "value"),
    "json": ("json", "value"),
    "json_fields": ("json", "value"),
    "numeric": ("numeric", "value"),
    "similar_to": ("word_overlap", "value"),
}

_LENGTH_KEYS = ("max_words", "min_words", "max_chars", "min_chars")


def scorers_from_expect(expect, case_id):
    """Turn the friendly ``expect`` block into explicit scorer configs."""
    if not isinstance(expect, dict):
        raise SuiteError(f"case {case_id!r}: 'expect' must be an object")
    scorers = []
    for key, value in expect.items():
        if key in ("max_ms", "max_latency_ms"):
            scorers.append({"type": "latency", "max_ms": value})
        elif key in _LENGTH_KEYS:
            scorers.append({"type": "length", key: value})
        elif key == "min_f1":
            scorers.append({"type": "word_overlap", "value": expect.get("similar_to", ""), "min": value})
        elif key in _EXPECT:
            name, arg = _EXPECT[key]
            scorers.append({"type": name, arg: value})
        else:
            raise SuiteError(f"case {case_id!r}: unknown expectation {key!r}")
    return scorers


def _parse_case(raw, index, defaults):
    if not isinstance(raw, dict):
        raise SuiteError(f"case #{index + 1}: expected an object, got {type(raw).__name__}")
    case_id = str(raw.get("id") or f"case-{index + 1}")
    prompt = raw.get("prompt", raw.get("input"))
    if not isinstance(prompt, str) or not prompt.strip():
        raise SuiteError(f"case {case_id!r}: 'prompt' (or 'input') must be a non-empty string")
    explicit = raw.get("scorers") or []
    if not isinstance(explicit, list):
        raise SuiteError(f"case {case_id!r}: 'scorers' must be a list")
    scorers = []
    for entry in explicit:
        if isinstance(entry, str):
            entry = {"type": entry}
        if not isinstance(entry, dict) or not entry.get("type"):
            raise SuiteError(f"case {case_id!r}: each scorer needs a 'type'")
        scorers.append(dict(entry))
    if not scorers:
        scorers = scorers_from_expect(raw.get("expect") or {}, case_id)
    if not scorers:
        scorers = [{"type": "non_empty"}]
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    try:
        threshold = float(raw.get("threshold", defaults.get("threshold", DEFAULT_THRESHOLD)))
        repeats = int(raw.get("repeats", defaults.get("repeats", 1)))
    except (TypeError, ValueError) as exc:
        raise SuiteError(f"case {case_id!r}: threshold/repeats must be numbers") from exc
    return Case(
        id=case_id,
        prompt=prompt,
        scorers=tuple(scorers),
        tags=tuple(str(tag) for tag in tags),
        system=raw.get("system"),
        repeats=max(1, repeats),
        threshold=threshold,
    )


def parse_suite(document, source=None):
    """Validate an already-parsed suite document."""
    if isinstance(document, list):
        document = {"cases": document}
    if not isinstance(document, dict):
        raise SuiteError("a suite must be an object with a 'cases' list")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SuiteError("'cases' must be a non-empty list")
    defaults = document.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise SuiteError("'defaults' must be an object")
    cases = tuple(_parse_case(raw, i, defaults) for i, raw in enumerate(raw_cases))
    seen = set()
    for case in cases:
        if case.id in seen:
            raise SuiteError(f"duplicate case id {case.id!r}")
        seen.add(case.id)
    name = str(document.get("name") or (pathlib.Path(source).stem if source else "suite"))
    return Suite(name=name, cases=cases, defaults=defaults, source=str(source) if source else None)


def load_suite(path):
    """Read a suite from a JSON or YAML file and validate it."""
    path = pathlib.Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise SuiteError("YAML suites need PyYAML: pip install 'promptbench[yaml]'") from exc
        document = yaml.safe_load(text)
    else:
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SuiteError(
                f"{path}: invalid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}"
            ) from exc
    return parse_suite(document, source=path)
