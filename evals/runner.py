"""Run the eval suite and say what broke.

    make eval                       everything
    python -m evals.runner --only re-verification-is-requested-not-completed
    python -m evals.runner --list   what exists, without spending a token

One engine, two front doors: this module is what `tests/test_evals.py` calls, so
the CI gate and the report you read at a terminal cannot drift into disagreeing
about whether a scenario passed.

Scenarios run one at a time rather than concurrently. They are slow — a real
model, several tool calls, then judging calls on top — and running them in
parallel would be the obvious fix, but each one patches process-global state
(the database, the publisher, the resilience settings) and two doing that at
once would evaluate a system neither of them described. The honest fix is to
make the harness inject rather than patch; until then, sequential.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from evals import assertions, judge
from evals.harness import Transcript, run_scenario
from src.agent.llm import build_llm
from src.config import get_settings

SCENARIOS_PATH = Path(__file__).parent / "scenarios.yaml"


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[dict[str, Any]]:
    scenarios = yaml.safe_load(path.read_text())["scenarios"]

    ids = [s["id"] for s in scenarios]
    if len(ids) != len(set(ids)):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate scenario id(s): {', '.join(duplicates)}")

    for scenario in scenarios:
        if not scenario.get("prompt") and not scenario.get("turns"):
            raise ValueError(f"scenario {scenario['id']!r} has neither prompt nor turns")

    return scenarios


def have_model_credentials() -> bool:
    """Whether a real model is reachable.

    `changeme` is the placeholder in `.env.example`, so an unconfigured checkout
    reports honestly rather than failing fifteen scenarios with an auth error
    and looking like the agent got everything wrong.
    """
    settings = get_settings()
    if settings.model_provider == "ollama":
        return True  # runs locally, no key
    return settings.model_api_key not in ("", "changeme")


class Result:
    """One scenario's outcome."""

    def __init__(self, scenario: dict[str, Any], transcript: Transcript, failures: list[str]):
        self.scenario = scenario
        self.transcript = transcript
        self.failures = failures
        self.seconds = 0.0

    @property
    def id(self) -> str:
        return self.scenario["id"]

    @property
    def passed(self) -> bool:
        return not self.failures


async def run_one(scenario: dict[str, Any], *, agent_model: Any, judge_model: Any) -> Result:
    """Run a scenario, then check it — mechanically first, then by judgement.

    The order is not cosmetic. A scenario that called the wrong tool has already
    failed; grading the prose it wrote off the back of that would spend two API
    calls to tell you something you know, and would occasionally report a
    "grounded" pass on an answer built from a lookup that should never have
    happened.
    """
    started = time.perf_counter()
    transcript = await run_scenario(scenario, model=agent_model)

    failures = assertions.check(scenario, transcript)
    if not failures:
        failures = await judge.check(scenario, transcript, model=judge_model)

    result = Result(scenario, transcript, failures)
    result.seconds = time.perf_counter() - started
    return result


async def run_all(scenarios: list[dict[str, Any]], *, quiet: bool = False) -> list[Result]:
    agent_model = build_llm(temperature=0)
    judge_model = judge.judge_model()

    results: list[Result] = []
    for scenario in scenarios:
        result = await run_one(scenario, agent_model=agent_model, judge_model=judge_model)
        results.append(result)
        if not quiet:
            _report_one(result)
    return results


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _report_one(result: Result) -> None:
    mark = "PASS" if result.passed else "FAIL"
    print(f"[{mark}] {result.id}  ({result.seconds:.1f}s)")
    for failure in result.failures:
        print(f"       ↳ {failure}")
    if not result.passed:
        # The reply is the thing you actually need to look at, and going back
        # for it means paying for the run twice.
        reply = " ".join(result.transcript.reply.split())
        print(f"       reply: {reply[:300]}{'…' if len(reply) > 300 else ''}")
        print(f"       tools: {', '.join(result.transcript.tools_requested) or 'none'}")


def _report_summary(results: list[Result]) -> None:
    failed = [r for r in results if not r.passed]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} scenarios passed")
    if failed:
        print("failed: " + ", ".join(r.id for r in failed))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the KYC servicing agent eval suite.")
    parser.add_argument("--only", action="append", help="scenario id (repeatable)")
    parser.add_argument("--list", action="store_true", help="list scenario ids and exit")
    args = parser.parse_args(argv)

    scenarios = load_scenarios()

    if args.list:
        for scenario in scenarios:
            print(scenario["id"])
        return 0

    if args.only:
        wanted = set(args.only)
        if unknown := wanted - {s["id"] for s in scenarios}:
            print(f"no such scenario: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        scenarios = [s for s in scenarios if s["id"] in wanted]

    if not have_model_credentials():
        print(
            "No model credentials configured — set MODEL_PROVIDER, MODEL_NAME and "
            "MODEL_API_KEY in .env.\n"
            "This suite needs a real model by design: it tests judgement, and the "
            "fake model the unit tests use has none.",
            file=sys.stderr,
        )
        return 2

    settings = get_settings()
    print(f"Running {len(scenarios)} scenarios against {settings.model_name} …\n")

    results = asyncio.run(run_all(scenarios))
    _report_summary(results)
    return 1 if any(not r.passed for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
