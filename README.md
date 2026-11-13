# promptbench

**A prompt test suite you can run in CI.**

When you change a prompt, a model or a retrieval step, the only question that matters is: did anything get worse? `promptbench` answers it with deterministic checks, repeat runs, retries and a baseline comparison that fails the build when quality drops.

```bash
promptbench init                                          # writes promptbench.suite.json
promptbench run promptbench.suite.json --provider echo
promptbench run suite.json --provider openai --model gpt-4o-mini --baseline baseline/run.json
```

No API key needed to see it work: the `echo` provider reflects the prompt back, and the `command` provider shells out to anything that reads a prompt on stdin and writes an answer on stdout, which is how you benchmark a local model, a CLI, or your own RAG pipeline.

## Why not a notebook

A notebook gives you a score for one prompt on one day. A suite gives you:

- **The same cases every time**, in version control next to the prompt.
- **Assertions that do not need an LLM to judge them**: substring checks, JSON shape, regex, numeric tolerance, length, word overlap, latency. Cheap, deterministic, and impossible to argue with.
- **A threshold per case**, so one hard case cannot hide behind a good average.
- **Repeats and retries**, because a single sample of a temperature-0.7 model means nothing. The report shows a stability percentage next to each score.
- **A baseline diff**: `--baseline` marks every case as new, fixed, broken or unchanged and exits non-zero when anything regressed.

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
      "expect": {"json": {"ok": true, "items.2": 3}},
      "tags": ["format"]
    }
  ]
}
```

Each key inside `expect` is a scorer (`max_words` is a `length` option, `json` takes dotted paths). A case passes when its score reaches its `threshold`, default `1.0`. Suites are JSON; `defaults` apply to every case and can be overridden per case.

```bash
promptbench init      # starter suite to edit
promptbench validate  promptbench.suite.json
promptbench scorers   # what you can assert on
```

## Scorers

```console
$ promptbench scorers

  contains       exact          json           latency        length
  non_empty      not_contains   numeric        regex          word_overlap
```

| Scorer | Passes when |
| --- | --- |
| `contains` | the reply contains the string, or every item of a list |
| `not_contains` | none of the strings appear |
| `regex` | a regular expression matches |
| `json` | dotted paths exist and equal the expected values |
| `exact` | the reply equals the expected text after trimming |
| `numeric` | numbers in the reply are within a tolerance |
| `length` | character or word count stays inside the bounds |
| `word_overlap` | share of expected words present, 0 to 1 |
| `latency` | reply time under a limit in milliseconds |
| `non_empty` | the model said something at all |

Scores are numbers between 0 and 1, so the report can show partial credit instead of a bare pass/fail. `word_overlap` and `latency` are the two you will tune; the rest are guardrails.

## Providers

| Provider | Use it for |
| --- | --- |
| `echo` | Smoke-testing a suite with no model at all |
| `command` | Any local model or script: `--command "ollama run llama3"` |
| `openai` | Any OpenAI-compatible endpoint: `--model`, `--base-url`, `--api-key-env` |

```bash
# A local model, no API key
promptbench run examples/starter.json --provider command \
  --command "python examples/demo_provider.py"

# A hosted model
export OPENAI_API_KEY=sk-...
promptbench run suite.json --provider openai --model gpt-4o-mini --temperature 0
```

Keys are read from the environment variable named by `--api-key-env` (default `OPENAI_API_KEY`) and never appear in the report.

## Running in CI

```bash
promptbench run suite.json --provider openai --model gpt-4o-mini \
  --baseline baseline/run.json --fail-under 0.95 --out results
```

- `--baseline` diffs against a previous `run.json` and fails on any regression.
- `--fail-under` fails when the mean score drops below the number.
- `--out DIR` writes `DIR/run.json` (the next baseline) and `DIR/report.html` (to send to a person). The Markdown table is printed to stdout, so piping it into a pull request comment is a one-liner.
- `--json` prints the machine-readable report instead of the table.

Exit codes: `0` everything passed, `1` a failure, regression or threshold miss, `2` invalid usage or an unreadable suite.

## Reading the report

```
## starter: 2/3 passed (67%)

provider `command` | mean score 0.833 | 0.0s total

| result | case | score | stability | latency | tags |
| --- | --- | --- | --- | --- | --- |
| FAIL | `capital-france` | 0.00 | 0% | 29ms | knowledge |
| pass | `json-shape` | 1.00 | 100% | 31ms | format |
| pass | `refuses-unknown` | 1.00 | 100% | 28ms | safety |

### Failures

**`capital-france`**
- contains (score 0.00): missing: 'Paris'
- length (score 0.00): max_words: 13 > 3
- reply: `[echo] What is the capital of France? Reply with the city name only.`
```

Totals by tag, a failures section with the offending scorer and the actual reply, and a `Versus baseline` table appear when there is something to say. Markdown for the terminal or a PR comment, HTML for a colleague, JSON for the next run.

## Limits

- The scorers are lexical. They catch regressions, broken JSON, truncation and drift; they do not judge whether prose is *good*. Pair them with human review for open-ended writing.
- One prompt per case. Multi-turn conversations are out of scope.
- No hosted dashboard and no database: reports are files, which is the point.
- A passing suite is evidence of stability, not of quality. Write the cases by hand and keep them small enough to read.

## Tests

```bash
python -m pytest
```

Covered: suite loading and validation, every scorer, the echo and command providers, retries, repeats, threshold handling, report rendering, baseline comparison and CLI exit codes.

MIT licensed.
