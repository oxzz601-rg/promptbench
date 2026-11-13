"""Where completions come from (providers) and how suites are executed."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .scorers import evaluate


class ProviderError(RuntimeError):
    """The model, or the program standing in for it, could not answer."""


@dataclass
class Reply:
    text: str
    prompt_tokens: int = None
    completion_tokens: int = None
    raw: dict = field(default_factory=dict)


class EchoProvider:
    """Echoes the prompt back. Useful for validating suites and CI smoke tests."""

    name = "echo"

    def __init__(self, template="[echo] {prompt}"):
        self.template = template

    def complete(self, prompt, system=None):
        return Reply(self.template.replace("{prompt}", prompt))


class CommandProvider:
    """Delegate to any program: prompt on stdin, reply on stdout.

    That covers local models (``ollama run llama3``), CLI agents (``llm -m ...``),
    raw ``curl`` calls and project-specific wrppers, without promptbench knowing
    anything about them.
    """

    name = "command"

    def __init__(self, command, timeout=120.0, cwd=None):
        self.argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not self.argv:
            raise ValueError("empty command")
        self.timeout = float(timeout)
        self.cwd = cwd
        self.display = command if isinstance(command, str) else " ".join(command)

    def complete(self, prompt, system=None):
        try:
            proc = subprocess.run(
                self.argv,
                input=prompt.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                cwd=self.cwd,
            )
        except FileNotFoundError as exc:
            raise ProviderError("command not found: {}".format(self.argv[0])) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("command timed out after {:g}s".format(self.timeout)) from exc
        except OSError as exc:
            raise ProviderError("could not run command: {}".format(exc)) from exc
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()[-400:]
            raise ProviderError("command exited {}: {}".format(proc.returncode, stderr or "(no stderr)"))
        return Reply(proc.stdout.decode("utf-8", "replace").strip())


class OpenAIProvider:
    """Any OpenAI-compatible chat completions endpoint (OpenAI, vLLM, Ollama, LiteLLM...)."""

    name = "openai"

    def __init__(self, model, base_url="https://api.openai.com/v1", api_key=None, api_key_env="OPENAI_API_KEY", temperature=None, max_tokens=None, timeout=120.0):
        if not model:
            raise ValueError("OpenAIProvider needs a model name")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = float(timeout)

    def complete(self, prompt, system=None):
        key = self.api_key or os.environ.get(self.api_key_env)
        if not key:
            raise ProviderError("missing API key: set {} or pass --api-key-env".format(self.api_key_env))
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "messages": messages}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ProviderError("HTTP {} from {}: {}".format(exc.code, self.base_url, detail)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError("network error talking to {}: {}".format(self.base_url, exc.reason)) from exc
        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("unexpected response shape: {}".format(str(body)[:300])) from exc
        usage = body.get("usage") or {}
        return Reply(
            text.strip(),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            raw=body,
        )


@dataclass
class Result:
    """A single completion plus its scores."""

    case_id: str
    passed: bool
    score: float
    output: str = ""
    details: tuple = ()
    latency_ms: float = 0.0
    tags: tuple = ()
    error: str = None
    prompt_tokens: int = None
    completion_tokens: int = None

    def to_dict(self):
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "score": round(self.score, 4),
            "latency_ms": round(self.latency_ms, 1),
            "tags": list(self.tags),
            "error": self.error,
            "details": list(self.details),
            "output": self.output,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclass
class CaseOutcome:
    """Every repeat of one case collapsed into a single verdict."""

    case_id: str
    passed: bool
    score: float
    stability: float = 1.0
    latency_ms: float = 0.0
    runs: tuple = ()
    tags: tuple = ()
    error: str = None

    def to_dict(self):
        return {
            "id": self.case_id,
            "passed": self.passed,
            "score": round(self.score, 4),
            "stability": round(self.stability, 3),
            "latency_ms": round(self.latency_ms, 1),
            "tags": list(self.tags),
            "error": self.error,
            "runs": [run.to_dict() for run in self.runs],
        }


@dataclass
class RunSummary:
    suite: str
    provider: str
    cases: tuple = ()
    started_at: str = ""
    duration_s: float = 0.0
    repeats: int = 1

    @property
    def total(self):
        return len(self.cases)

    @property
    def passed(self):
        return sum(1 for case in self.cases if case.passed)

    @property
    def failed_cases(self):
        return [case for case in self.cases if not case.passed]

    @property
    def pass_rate(self):
        return self.passed / self.total if self.total else 0.0

    @property
    def mean_score(self):
        return sum(case.score for case in self.cases) / self.total if self.total else 0.0

    @property
    def total_tokens(self):
        total = 0
        for case in self.cases:
            for run in case.runs:
                total += (run.prompt_tokens or 0) + (run.completion_tokens or 0)
        return total

    def by_tag(self):
        groups = {}
        for case in self.cases:
            for tag in case.tags or ("untagged",):
                groups.setdefault(tag, []).append(case)
        return {
            tag: {
                "total": len(cases),
                "passed": sum(1 for case in cases if case.passed),
                "mean_score": sum(case.score for case in cases) / len(cases),
            }
            for tag, cases in sorted(groups.items())
        }

    def to_dict(self):
        return {
            "suite": self.suite,
            "provider": self.provider,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 2),
            "repeats": self.repeats,
            "pass_rate": round(self.pass_rate, 4),
            "mean_score": round(self.mean_score, 4),
            "cases": [case.to_dict() for case in self.cases],
        }

    @classmethod
    def from_dict(cls, data):
        cases = []
        for raw in data.get("cases") or []:
            runs = tuple(
                Result(
                    case_id=run.get("case_id", raw.get("id")),
                    passed=bool(run.get("passed")),
                    score=float(run.get("score", 0.0)),
                    output=run.get("output", ""),
                    details=tuple(run.get("details") or ()),
                    latency_ms=float(run.get("latency_ms", 0.0)),
                    tags=tuple(run.get("tags") or ()),
                    error=run.get("error"),
                )
                for run in raw.get("runs") or []
            )
            cases.append(
                CaseOutcome(
                    case_id=raw.get("id"),
                    passed=bool(raw.get("passed")),
                    score=float(raw.get("score", 0.0)),
                    stability=float(raw.get("stability", 1.0)),
                    latency_ms=float(raw.get("latency_ms", 0.0)),
                    runs=runs,
                    tags=tuple(raw.get("tags") or ()),
                    error=raw.get("error"),
                )
            )
        return cls(
            suite=data.get("suite", "suite"),
            provider=data.get("provider", "unknown"),
            cases=tuple(cases),
            started_at=data.get("started_at", ""),
            duration_s=float(data.get("duration_s", 0.0)),
            repeats=int(data.get("repeats", 1)),
        )


def run_suite(suite, provider, concurrency=4, repeats=1, retries=2, backoff=0.5):
    """Run every case ``repeats * case.repeats`` times and aggregate the verdicts."""
    started = time.time()
    jobs = [
        (index, repeat, case)
        for index, case in enumerate(suite.cases)
        for repeat in range(max(1, int(repeats)) * case.repeats)
    ]

    def execute(job):
        index, repeat, case = job
        system = case.system or suite.defaults.get("system")
        error = None
        for attempt in range(retries + 1):
            begin = time.perf_counter()
            try:
                reply = provider.complete(case.prompt, system=system)
            except Exception as exc:  # provider failures must not kill the run
                error = "{}: {}".format(type(exc).__name__, exc)
                if attempt < retries:
                    time.sleep(backoff * (2**attempt))
                continue
            latency = (time.perf_counter() - begin) * 1000.0
            score, passed, details = evaluate(case.scorers, reply.text, {"latency_ms": latency}, case.threshold)
            return index, repeat, Result(
                case_id=case.id,
                passed=passed,
                score=score,
                output=reply.text,
                details=tuple(details),
                latency_ms=latency,
                tags=case.tags,
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
            )
        return index, repeat, Result(
            case_id=case.id, passed=False, score=0.0, tags=case.tags, error=error
        )

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        rows = list(pool.map(execute, jobs))
    rows.sort(key=lambda row: (row[0], row[1]))

    grouped = {}
    for index, _repeat, result in rows:
        grouped.setdefault(index, []).append(result)
    outcomes = []
    for index in sorted(grouped):
        runs = grouped[index]
        passes = sum(1 for run in runs if run.passed)
        outcomes.append(
            CaseOutcome(
                case_id=runs[0].case_id,
                passed=passes == len(runs),
                stability=passes / len(runs),
                score=sum(run.score for run in runs) / len(runs),
                latency_ms=sum(run.latency_ms for run in runs) / len(runs),
                runs=tuple(runs),
                tags=runs[0].tags,
                error=next((run.error for run in runs if run.error), None),
            )
        )
    return RunSummary(
        suite=suite.name,
        provider=getattr(provider, "name", type(provider).__name__),
        cases=tuple(outcomes),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        duration_s=time.time() - started,
        repeats=max(1, int(repeats)),
    )
