# promptbench

**A prompt test suite you can run in CI.**

When you change a prompt, a model or a retrieval step, the only question that matters is: did anything get worse? `promptbench` answers it with deterministic checks, repeat runs, retries and a baseline comparison that fails the build when quality drops.

```bash
promptbench init                     # write a starter suite
promptbench run suite.json --provider echo
promptbench run suite.json --provider openai --model gpt-4o-mini --baseline results.json
```

No API key needed to see it work: the `echo` provider reflects the prompt back, and the `command` provider shells out to anything that reads a prompt on stdin and writes an answer on stdout, which is how you benchmark a local model, a CLI, or your own RAG pipeline.

## Why not a notebook

A notebook gives you a score for one prompt on one day. A suite gives you:

- **The same cases every time**, in version control next to the prompt.
- **Assertions that do not need an LLM to judge them**: substring checks, JSON shape, regex, numeric tolerance, length, word overlap, latency. Cheap, deterministic, and impossible to argue with.
- **A pass threshold per case**, so one hard case does not hide behind an average.
- **Repeats and retries**, because a single sample of a temperature-0.7 model means nothing.
- **A baseline diff**: `--baseline` marks each case as new, fixed, broken or unchanged and exits non-zero when anything regressed.

## Install

```bash
pip install -e ".[dev]"
```

Python 3.9+. The OpenAI provider uses plain HTTP, so no SDK is required.

## The suite file

```json
{
  "name": "assistant",
  "defaults": {"threshold": 1.0, "repeats": 2, "retries": 1},
  "cases": [
    {
      "id": "capital-france",
      "prompt": "What is the capital of France? Reply with the city name only.",
      "expect": {"contains": "Paris", "max_words": 3},
      "tags": ["knowledge"]
    },
    {
      "id": "json-shape",
      "prompt": "Return JSON: {\"ok\": true, \"items\": [1, 2, 3]}",
      "expect": {"json": {"ok": true, "items.2": 3}}
    }
  ]
}
```

Every key in `expect` is a scorer; a case passes when its score is at least its `threshold` (default `1.0`, fully satisfied). `bad_*`? No: use `not_contains`.

## Scorers

```console
$ promptbench scorers
contains       response contains the string(s) or every item of the list
not_contains   none of the strings appear
regex          a regular expression matches
json           dotted paths exist and equal the expected values
exact          response equals the expected string after trimming
numeric        numbers in the response are within a tolerance
length         character or word count stays inside the bounds
word_overlap   share of expected words present (0 to 1)
latency        response time under a limit in milliseconds
non_empty      the model said something at all
```

Scores are numbers between 0 and 1, so the report can show partial credit instead of a bare pass/fail. `latency` and `word_overlap` are the two you will tune constantly; the rest are guardrails.

## Providers

| Provider | Use it for |
| --- | --- |
| `echo` | Smoke-testing a suite with no model at all |
| `command` | Any local model or script: `--command "ollama run llama3"` |
| `openai` | Any OpenAI-compatible endpoint: `--model`, `--base-url`, `--api-key-env` |

```bash
# A local model, no API key
export PROMPT_COMMAND='python examples/demo_provider.py'
promptbench run examples/starter.json --provider command --command "$PROMPT_COMMAND"

# A hosted model
export OPENAI_API_KEY=sk-...
promptbench run suite.json --provider openai --model gpt-4o-mini --temperature 0
```

Keys are read from the environment variable named by `--api-key-env` (default `OPENAI_API_KEY`); they never appear in the report.

## Running in CI

```yaml
- run: promptbench run suite.json --provider openai --model gpt-4o-mini \
         --baseline baseline.json --fail-under 0.95 --out report.json
```

- `--baseline` compares against a previous report and fails on any regression.
- `--fail-under` fails when the mean score drops below the number.
- `--out` writes Markdown, HTML and JSON next to each other (`report.md`, `report.html`, `report.json`).

Exit codes: `0` everything passed, `1` a failure, regression or threshold miss, `2` invalid usage or an unreadable suite. That is the whole integration.

## Reading the report

```
assistant: 12/12 passed (mean 0.98)
  capital-france        pass  1.00  contains
  json-shape            pass  1.00  json
  refund-policy         FAIL  0.40  word_overlap below 0.85
    expected: cite the 30 day window
    got:      you can get a refund
```

Markdown for a pull request comment, HTML for something you can send to a colleague, JSON for the next run's baseline.

## Limits

- The scorers are lexical. They catch regressions, broken JSON, truncation and drift; they do not judge whether prose is *good*. Pair them with a human review for open-ended writing.
- One prompt per case. Multi-turn conversations are out of scope.
- No hosted dashboard and no database: reports are files, which is the point.

## Tests

```bash
python -m pytest
```

Covered: suite loading and validation, every scorer, the echo and command providers, retries, repeats, threshold handling, report rendering, baseline comparison and CLI exit codes.

MIT licensed.
