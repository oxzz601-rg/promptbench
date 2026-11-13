"""Deterministic scorers.

Each scorer is called as ``scorer(config, output, context)`` and returns
``(score, detail)`` where score is a float in ``[0, 1]``. Fractional scores are
partial credit: the weighted mean is compared against the case threshold.
"""

from __future__ import annotations

import json
import re
import unicodedata

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_WORD = re.compile(r"[a-z0-9']+")


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text))).strip()


def _items(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _fold(text, ignore_case):
    text = normalize(text)
    return text.casefold() if ignore_case else text


def score_exact(cfg, output, ctx):
    want = cfg.get("value", cfg.get("text", ""))
    ignore = cfg.get("ignore_case", True)
    if _fold(output, ignore) == _fold(want, ignore):
        return 1.0, "exact match"
    return 0.0, "expected {!r}, got {!r}".format(str(want)[:90], str(output)[:90])


def score_contains(cfg, output, ctx):
    needles = _items(cfg.get("value", cfg.get("values")))
    if not needles:
        return 1.0, "no fragments configured"
    ignore = cfg.get("ignore_case", True)
    haystack = _fold(output, ignore)
    missing = [n for n in needles if _fold(n, ignore) not in haystack]
    if not missing:
        return 1.0, "all {} fragment(s) present".format(len(needles))
    return (len(needles) - len(missing)) / len(needles), "missing: {}".format(
        ", ".join(repr(m)[:60] for m in missing)
    )


def score_not_contains(cfg, output, ctx):
    banned = _items(cfg.get("value", cfg.get("values")))
    ignore = cfg.get("ignore_case", True)
    haystack = _fold(output, ignore)
    found = [word for word in banned if _fold(word, ignore) in haystack]
    if found:
        return 0.0, "forbidden text present: {}".format(", ".join(repr(w)[:60] for w in found))
    return 1.0, "none of {} forbidden fragment(s) present".format(len(banned))


def score_regex(cfg, output, ctx):
    pattern = cfg.get("value", cfg.get("pattern", ""))
    flags = 0
    if cfg.get("ignore_case", True):
        flags |= re.IGNORECASE
    if cfg.get("multiline", False):
        flags |= re.MULTILINE
    if cfg.get("dotall", False):
        flags |= re.DOTALL
    try:
        match = re.search(pattern, output, flags)
    except re.error as exc:
        return 0.0, "invalid regex {!r}: {}".format(pattern, exc)
    if match:
        return 1.0, "matched {!r}".format(match.group(0)[:80])
    return 0.0, "no match for {!r}".format(pattern)


def extract_json(text):
    """Pull the first JSON value out of a reply, tolerating prose and fences."""
    text = str(text).strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth = 0
            in_string = escaped = False
            for i in range(start, len(text)):
                char = text[i]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            break
            start = text.find(opener, start + 1)
    raise ValueError("no JSON value found in output")


def _walk(data, path):
    current = data
    for part in str(path).split("."):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(path)
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                raise KeyError(path)
        else:
            raise KeyError(path)
    return current


def _matches(actual, expected, tolerance=1e-6):
    if isinstance(expected, bool) or actual is None or isinstance(actual, bool):
        return actual == expected
    if isinstance(expected, dict) and "$regex" in expected:
        return bool(re.search(str(expected["$regex"]), str(actual), re.IGNORECASE))
    if isinstance(expected, dict) and "$contains" in expected:
        return normalize(expected["$contains"]).casefold() in normalize(actual).casefold()
    if isinstance(expected, (int, float)):
        try:
            return abs(float(actual) - float(expected)) <= tolerance
        except (TypeError, ValueError):
            return False
    if isinstance(expected, str) and isinstance(actual, str):
        return normalize(expected).casefold() == normalize(actual).casefold()
    return actual == expected


def score_json(cfg, output, ctx):
    paths = cfg.get("value", cfg.get("fields")) or {}
    if not isinstance(paths, dict) or not paths:
        return 0.0, "json scorer needs a {path: expected} object"
    try:
        data = extract_json(output)
    except ValueError:
        return 0.0, "reply is not valid JSON"
    tolerance = float(cfg.get("tolerance", 1e-6))
    problems = []
    for path, expected in paths.items():
        try:
            actual = _walk(data, path)
        except KeyError:
            problems.append("{} is missing".format(path))
            continue
        if not _matches(actual, expected, tolerance):
            problems.append("{} = {!r}, wanted {!r}".format(path, actual, expected))
    score = (len(paths) - len(problems)) / len(paths)
    if not problems:
        return 1.0, "all {} field(s) match".format(len(paths))
    return score, "; ".join(problems[:6])


def score_numeric(cfg, output, ctx):
    try:
        want = float(cfg.get("value"))
    except (TypeError, ValueError):
        return 0.0, "numeric scorer needs a numeric 'value'"
    tolerance = float(cfg.get("tolerance", 1e-6))
    numbers = [float(n) for n in _NUMBER.findall(str(output).replace(",", ""))]
    if not numbers:
        return 0.0, "no number found in reply"
    answer = numbers[-1] if cfg.get("take", "last") == "last" else numbers[0]
    if abs(answer - want) <= tolerance:
        return 1.0, "answer {} within {} of {}".format(answer, tolerance, want)
    return 0.0, "answer {} is not within {} of {}".format(answer, tolerance, want)


def score_length(cfg, output, ctx):
    words = len(_WORD.findall(str(output)))
    chars = len(str(output))
    problems = []
    for key, actual in (("max_chars", chars), ("min_chars", chars), ("max_words", words), ("min_words", words)):
        if key in cfg:
            limit = int(cfg[key])
            if key.startswith("max") and actual > limit:
                problems.append("{}: {} > {}".format(key, actual, limit))
            if key.startswith("min") and actual < limit:
                problems.append("{}: {} < {}".format(key, actual, limit))
    if problems:
        return 0.0, "; ".join(problems)
    return 1.0, "{} words, {} chars (within limits)".format(words, chars)


def score_word_overlap(cfg, output, ctx):
    reference = set(_WORD.findall(str(cfg.get("value", "")).casefold()))
    if not reference:
        return 0.0, "word_overlap needs a reference 'value'"
    produced = set(_WORD.findall(str(output).casefold()))
    overlap = len(produced & reference)
    precision = overlap / len(produced) if produced else 0.0
    recall = overlap / len(reference)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    if "min" in cfg:
        target = float(cfg["min"])
        return (1.0 if f1 >= target else 0.0), "f1={:.3f} (min {:.2f})".format(f1, target)
    return f1, "f1={:.3f}".format(f1)


def score_latency(cfg, output, ctx):
    limit = float(cfg.get("max_ms", cfg.get("value", 0)) or 0)
    actual = float(ctx.get("latency_ms", 0.0))
    if limit <= 0:
        return 1.0, "no limit configured"
    if actual <= limit:
        return 1.0, "{:.0f}ms <= {:.0f}ms".format(actual, limit)
    return 0.0, "{:.0f}ms exceeds {:.0f}ms".format(actual, limit)


def score_non_empty(cfg, output, ctx):
    if str(output).strip():
        return 1.0, "reply is not empty"
    return 0.0, "reply is empty"


SCORERS = {
    "contains": score_contains,
    "exact": score_exact,
    "json": score_json,
    "latency": score_latency,
    "length": score_length,
    "non_empty": score_non_empty,
    "not_contains": score_not_contains,
    "numeric": score_numeric,
    "regex": score_regex,
    "word_overlap": score_word_overlap,
}


def evaluate(scorers, output, context=None, threshold=1.0):
    """Run every scorer and return ``(score, passed, details)``."""
    context = context or {}
    details = []
    total_weight = 0.0
    weighted = 0.0
    for config in scorers:
        name = config.get("type")
        scorer = SCORERS.get(name)
        if scorer is None:
            raise ValueError("unknown scorer {!r}; available: {}".format(name, ", ".join(sorted(SCORERS))))
        weight = float(config.get("weight", 1.0))
        score, detail = scorer(config, output, context)
        score = max(0.0, min(1.0, float(score)))
        weighted += score * weight
        total_weight += weight
        details.append({"scorer": name, "score": round(score, 4), "weight": weight, "detail": detail})
    score = weighted / total_weight if total_weight else 0.0
    return score, score + 1e-9 >= float(threshold), details
