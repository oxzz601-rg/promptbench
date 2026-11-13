import json
import sys

import pytest

from promptbench import load_suite, parse_suite, run_suite
from promptbench.core import CommandProvider, EchoProvider, ProviderError, Reply, RunSummary
from promptbench.report import compare, html_report, markdown, write_report
from promptbench.scorers import evaluate, extract_json
from promptbench.suite import SuiteError


def test_expect_block_becomes_scorers():
    suite = parse_suite(
        {"cases": [{"id": "a", "prompt": "hi", "expect": {"contains": ["x", "y"], "max_words": 4}}]}
    )
    assert [scorer["type"] for scorer in suite.cases[0].scorers] == ["contains", "length"]
    assert suite.cases[0].scorers[1]["max_words"] == 4


def test_defaults_threshold_and_system():
    suite = parse_suite(
        {"defaults": {"threshold": 0.5, "system": "be terse"}, "cases": [{"id": "a", "prompt": "hi"}]}
    )
    assert suite.cases[0].threshold == 0.5
    assert suite.defaults["system"] == "be terse"
    assert suite.cases[0].scorers == ({"type": "non_empty"},)


def test_invalid_suites_are_rejected():
    with pytest.raises(SuiteError, match="duplicate"):
        parse_suite({"cases": [{"id": "a", "prompt": "x"}, {"id": "a", "prompt": "y"}]})
    with pytest.raises(SuiteError, match="unknown expectation"):
        parse_suite({"cases": [{"prompt": "x", "expect": {"smells_right": True}}]})
    with pytest.raises(SuiteError, match="prompt"):
        parse_suite({"cases": [{"id": "a"}]})
    with pytest.raises(SuiteError, match="non-empty"):
        parse_suite({"cases": []})


def test_load_suite_from_json_file(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps({"name": "file-suite", "cases": [{"prompt": "x"}]}))
    suite = load_suite(path)
    assert suite.name == "file-suite" and len(suite.cases) == 1
    path.write_text("{not json}")
    with pytest.raises(SuiteError, match="invalid JSON"):
        load_suite(path)


def test_scorers_score_what_they_promise():
    assert evaluate([{"type": "exact", "value": "Paris"}], "  paris\n")[1] is True
    assert evaluate([{"type": "contains", "value": ["a", "b"]}], "a only")[0] == 0.5
    assert evaluate([{"type": "not_contains", "value": "secret"}], "all clear")[1] is True
    assert evaluate([{"type": "regex", "value": r"^\d{3}-\d{2}$"}], "123-45")[1] is True
    assert evaluate([{"type": "numeric", "value": 42}], "the answer is 42.")[1] is True
    assert evaluate([{"type": "length", "max_words": 2}], "one two")[1] is True
    assert evaluate([{"type": "length", "max_words": 2}], "one two three")[1] is False
    assert evaluate([{"type": "length", "max_words": 2}], "HELLO BRAVE WORLD")[1] is False
    assert evaluate([{"type": "latency", "max_ms": 100}], "x", {"latency_ms": 50})[1] is True
    assert evaluate([{"type": "non_empty"}], "   ")[1] is False
    # "five days" against the longer reference keeps recall low, so f1 stays under 0.9
    assert evaluate([{"type": "word_overlap", "value": "refunds take five days", "min": 0.9}], "five days")[1] is False
    assert evaluate([{"type": "word_overlap", "value": "five days", "min": 0.4}], "five days")[1] is True


def test_json_scorer_finds_json_in_prose_and_fences():
    assert extract_json('here you go:\n```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert extract_json('Sure! {"ok": true, "n": 2} hope that helps')["n"] == 2
    score, passed, _ = evaluate([{"type": "json", "value": {"ok": True, "n": 3}}], '{"ok": true, "n": 2}')
    assert passed is False and round(score, 2) == 0.5
    assert evaluate([{"type": "json", "value": {"name": {"$contains": "ada"}}}], '{"name": "Ada Lovelace"}')[1] is True


def test_unknown_scorer_is_a_programming_error():
    with pytest.raises(ValueError, match="unknown scorer"):
        evaluate([{"type": "vibes"}], "x")


def test_run_suite_orders_and_scores_results():
    suite = parse_suite(
        {
            "cases": [
                {"id": "first", "prompt": "say hi", "expect": {"contains": "hi"}},
                {"id": "second", "prompt": "say hi", "expect": {"contains": "zzz"}, "tags": ["strict"]},
            ]
        }
    )
    summary = run_suite(suite, EchoProvider(), concurrency=2)
    assert [case.case_id for case in summary.cases] == ["first", "second"]
    assert summary.passed == 1 and summary.pass_rate == 0.5
    assert summary.cases[0].runs[0].output == "[echo] say hi"
    assert summary.by_tag()["strict"]["passed"] == 0


def test_provider_failures_are_recorded_not_raised():
    class Down:
        name = "down"

        def complete(self, prompt, system=None):
            raise ProviderError("connection refused")

    summary = run_suite(parse_suite({"cases": [{"id": "a", "prompt": "x"}]}), Down(), retries=1, backoff=0)
    assert summary.pass_rate == 0
    assert "ProviderError" in summary.cases[0].error


def test_repeats_detect_flaky_cases():
    class Flaky:
        name = "flaky"

        def __init__(self):
            self.calls = 0

        def complete(self, prompt, system=None):
            self.calls += 1
            return Reply("yes" if self.calls % 2 else "no")

    suite = parse_suite({"cases": [{"id": "a", "prompt": "x", "expect": {"text": "yes"}}]})
    summary = run_suite(suite, Flaky(), concurrency=1, repeats=2)
    assert summary.cases[0].stability == 0.5
    assert summary.cases[0].passed is False


def test_command_provider_talks_stdin_stdout(tmp_path):
    script = tmp_path / "fake_model.py"
    script.write_text("import sys\nprint(sys.stdin.read().strip().upper())\n")
    provider = CommandProvider([sys.executable, str(script)], timeout=10)
    assert provider.complete("hello").text == "HELLO"


def test_command_provider_surfaces_failures():
    provider = CommandProvider([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], timeout=10)
    with pytest.raises(ProviderError) as error:
        provider.complete("x")
    assert "exited 3" in str(error.value) and "boom" in str(error.value)


def test_comparison_flags_regressions():
    def summary(statuses):
        return RunSummary.from_dict(
            {
                "suite": "s",
                "provider": "echo",
                "cases": [
                    {"id": key, "passed": value, "score": 1.0 if value else 0.0} for key, value in statuses.items()
                ],
            }
        )

    before = summary({"stays": True, "breaks": True, "fixed": False})
    after = summary({"stays": True, "breaks": False, "fixed": True})
    diff = compare(before, after)
    assert [item["id"] for item in diff["regressions"]] == ["breaks"]
    assert diff["fixed"] == ["fixed"]
    assert diff["pass_rate_before"] == pytest.approx(2 / 3, rel=1e-3)


def test_reports_are_written_and_escaped(tmp_path):
    summary = RunSummary.from_dict(
        {"suite": "<script>", "provider": "echo", "cases": [{"id": "<b>", "passed": True, "score": 1.0}]}
    )
    assert "| pass | `<b>` |" in markdown(summary)
    assert "&lt;b&gt;" in html_report(summary)
    paths = write_report(tmp_path / "out", summary)
    with open(paths["json"], encoding="utf-8") as handle:
        assert json.load(handle)["suite"] == "<script>"
    with open(paths["markdown"], encoding="utf-8") as handle:
        assert "pass" in handle.read()
