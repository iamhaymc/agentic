#!/usr/bin/env python3
"""Evals of long tasks: success, peak context and wall time, per model.

Each task of `TASKS` builds a workspace from a seed (a corpus too large for
one read, or a trail of files), puts one question to an `Engine` over it and
checks the answer. A run is measured on the event bus: whether the answer was
right, the most context the leader held (`context.usage`), how long the run
took, the model calls, tool calls and folds (the leader's and its workers'),
compactions, and the tokens and cost. Everything the harness does for a long
task runs as it would: the time cap, `plan`, `delegate`, `map`, `branch` /
`return` (in the leader and in its workers) and compaction.

    python -m test.agentic_eval --scripted       # offline: a scripted policy, no model
    python -m test.agentic_eval                  # the configured leader
    python -m test.agentic_eval --model qwen3:8b@http://localhost:11434/v1 \\
        --model deepseek/deepseek-v4-flash-0731@https://openrouter.ai/api/v1#OPENROUTER_API_KEY
    python -m test.agentic_eval --task needle --scale 4 --runs 3 --out evals.jsonl

A target is ``MODEL[@URL][#KEY_ENV]``: the model id, its endpoint and the
environment variable holding its key; what is left out comes from the config
and the environment as for any run (a loopback endpoint gets no key).
`--scripted` swaps the model for a policy that solves each task with the
harness's own tools (map, branch, return), so the fixtures, the checks and the
measuring are tested without a provider; the unit suite runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import random
import re
import statistics
import sys
import tempfile
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import app.agentic as A

# ---------------------------------------------------------------------------
# Fixtures
#
# Every workspace is built from a seeded generator, so the same seed gives the
# same files, the same answer and the same picture of the run.
# ---------------------------------------------------------------------------

WORDS = (
    "agent budget cache chunk context draft engine field graph harness index "
    "journal kernel layer model node offset page queue reader slice token "
    "trace window worker yield buffer column driver event filter gauge header "
    "input ledger matrix network output packet record signal table update vector"
).split()


def filler(rng: random.Random, lines: int, words: tuple[int, int] = (8, 14)) -> list[str]:
    """Lines of seeded nonsense: text to read, nothing to find."""
    return [
        " ".join(rng.choice(WORDS) for _ in range(rng.randint(*words))).capitalize() + "."
        for _ in range(lines)
    ]


def write(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_needle(root: Path, rng: random.Random, scale: int) -> dict[str, Any]:
    """`docs/` holds 6 files a scale of 120 lines; one line says the code."""
    names = [f"docs/part-{n:02d}.md" for n in range(6 * scale)]
    code = str(rng.randint(1000, 9999))
    where = rng.choice(names)
    for name in names:
        lines = filler(rng, 120)
        if name == where:
            lines[rng.randrange(len(lines))] = f"Note: the code is {code}."
        write(root / name, lines)
    return {"code": code, "file": where}


def build_tally(root: Path, rng: random.Random, scale: int) -> dict[str, Any]:
    """`logs/` holds 4 files a scale of 200 log lines; some say ERROR."""
    count = 0
    for n in range(4 * scale):
        lines = []
        for second in range(200):
            level = "ERROR" if rng.random() < 0.07 else rng.choice(("INFO", "DEBUG", "WARN"))
            count += level == "ERROR"
            words = " ".join(rng.choice(WORDS) for _ in range(rng.randint(5, 10)))
            lines.append(f"2026-10-15T{n:02d}:{second // 60:02d}:{second % 60:02d} {level} {words}")
        write(root / f"logs/service-{n:02d}.log", lines)
    return {"count": count}


def build_trail(root: Path, rng: random.Random, scale: int) -> dict[str, Any]:
    """`hops/` holds a trail of 5 files a scale, each naming the next."""
    names = ["hops/start.md"] + [f"hops/{rng.getrandbits(32):08x}.md" for _ in range(5 * scale - 1)]
    secret = f"{rng.choice(WORDS)}-{rng.randint(100, 999)}"
    for index, name in enumerate(names):
        lines = filler(rng, 80)
        last = index + 1 == len(names)
        line = f"The answer is {secret}." if last else f"Next: {names[index + 1]}"
        lines.insert(rng.randrange(len(lines)), line)
        write(root / name, lines)
    return {"secret": secret, "hops": len(names)}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_code(answer: str, root: Path, facts: dict[str, Any]) -> bool:
    return re.search(rf"\b{facts['code']}\b", answer) is not None


def check_count(answer: str, root: Path, facts: dict[str, Any]) -> bool:
    numbers = re.findall(r"\b\d+\b", answer.replace(",", ""))
    return bool(numbers) and int(numbers[-1]) == facts["count"]


def check_secret(answer: str, root: Path, facts: dict[str, Any]) -> bool:
    return facts["secret"] in answer


# ---------------------------------------------------------------------------
# Scripted policy
#
# What `--scripted` puts in place of a model: a policy that reads each call and
# answers it with the harness's own tools, deterministic and offline.
# ---------------------------------------------------------------------------

#: How a map worker is told to read its part (`Engine.map_prompt`).
READ_RE = re.compile(r'read_text_file\(path="([^"]+)"(?:, offset=(\d+), limit=(\d+))?\)')
#: How a read page says there is more (`TextPage.footer`).
MORE_RE = re.compile(r"continue with offset=(\d+)\]")
NEEDLE_RE = re.compile(r"the code is (\d+)")
NEXT_RE = re.compile(r"^Next: (\S+)$", re.MULTILINE)
SECRET_RE = re.compile(r"The answer is (\S+?)\.?$", re.MULTILINE)


def outputs(call: Any) -> list[str]:
    """The tool outputs in the input of a model call, oldest first."""
    return [
        str(item.get("output") or "")
        for item in call.input
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


def arguments(call: Any, name: str) -> list[dict[str, Any]]:
    """The arguments of the calls named `name` in the input of a model call."""
    found = []
    for item in call.input:
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == name:
            try:
                found.append(json.loads(item.get("arguments") or "{}"))
            except ValueError:
                found.append({})
    return found


def prompt(call: Any) -> str:
    """The text of the first user message of a model call."""
    for item in call.input:
        if isinstance(item, dict) and item.get("role") == "user":
            return A.content_text(item.get("content"))
    return ""


def lines_of(texts: Iterable[str]) -> list[str]:
    """The lines of read pages, footers left out."""
    return [line for text in texts for line in text.splitlines() if not line.startswith("[lines ")]


class Policy:
    """Solves the tasks with the harness's tools: the leader maps, branches and
    answers; a worker reads its part page by page and answers for it."""

    def __init__(self, task: "Task", facts: dict[str, Any]) -> None:
        self.task = task
        self.facts = facts
        self.ids = itertools.count(1)

    def call(self, name: str, args: dict[str, Any]) -> list[Any]:
        from agents.testing import function_call

        return [function_call(name, args, call_id=f"{name}-{next(self.ids)}")]

    @staticmethod
    def say(text: str) -> list[Any]:
        from agents.testing import assistant_message

        return [assistant_message(text)]

    def __call__(self, call: Any) -> list[Any]:
        if (call.system_instructions or "").startswith(A.DEFAULT_WORKER_INSTRUCTIONS):
            return self.work(call)
        return self.task.lead(self, call)

    def work(self, call: Any) -> list[Any]:
        """A map worker: read the part it was given, a page at a time, then
        answer. A part longer than a page is read in a branch: the pages after
        the first are folded away by `return`, whose summary is the answer."""
        match = READ_RE.search(prompt(call))
        if match is None:
            return self.say("none")
        for summary in arguments(call, "return"):
            return self.say(str(summary.get("summary") or "none"))
        path = match.group(1)
        first = int(match.group(2) or 1)
        end = first + int(match.group(3)) - 1 if match.group(3) else 0
        texts = [text for text in outputs(call) if not text.startswith("Branch opened")]
        if not texts:
            return self.call("read_text_file", {"path": path, "offset": first, "limit": end - first + 1 if end else 0})
        branched = bool(arguments(call, "branch"))
        more = MORE_RE.search(texts[-1])
        if more is not None and (not end or int(more.group(1)) <= end):
            if not branched:
                return self.call("branch", {"goal": f"Read the rest of {path}"})
            offset = int(more.group(1))
            return self.call("read_text_file", {"path": path, "offset": offset, "limit": end - offset + 1 if end else 0})
        answer = self.task.part(lines_of(texts))
        return self.call("return", {"summary": answer}) if branched else self.say(answer)


def lead_needle(policy: Policy, call: Any) -> list[Any]:
    texts = outputs(call)
    if not texts:
        return policy.call("map", {"task": NEEDLE_PART, "inputs": ["docs"]})
    found = NEEDLE_RE.findall(texts[-1])
    return policy.say(found[0] if found else "I could not find the code.")


def lead_tally(policy: Policy, call: Any) -> list[Any]:
    texts = outputs(call)
    if not texts:
        return policy.call("map", {"task": TALLY_PART, "inputs": ["logs"]})
    counts = re.findall(r"^## \d+/\d+ [^\n]*\n(\d+)", texts[-1], re.MULTILINE)
    return policy.say(str(sum(int(count) for count in counts)))


def lead_trail(policy: Policy, call: Any) -> list[Any]:
    """Branch, follow the trail file by file, return what its end says, answer."""
    for summary in arguments(call, "return"):
        found = SECRET_RE.search(str(summary.get("summary") or ""))
        if found:
            return policy.say(found.group(1))
    texts = outputs(call)
    if not texts:
        return policy.call("branch", {"goal": "Follow the trail from hops/start.md to its end"})
    last = texts[-1]
    found = SECRET_RE.search(last)
    if found:
        return policy.call("return", {"summary": f"The trail ends: The answer is {found.group(1)}."})
    hop = NEXT_RE.search(last)
    return policy.call("read_text_file", {"path": hop.group(1) if hop else "hops/start.md"})


#: What a map worker of the scripted policy is asked.
NEEDLE_PART = "Quote the line that says what the code is, or answer 'none'."
TALLY_PART = "Count the lines that contain ERROR and answer with the number alone."


def part_needle(lines: list[str]) -> str:
    return next((line for line in lines if NEEDLE_RE.search(line)), "none")


def part_tally(lines: list[str]) -> str:
    return str(sum(" ERROR " in f" {line} " for line in lines))


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """One long task: a workspace, a question, a check, and how the scripted
    policy solves it (`lead` for the leader, `part` for a map worker)."""

    name: str
    prompt: str
    build: Callable[[Path, random.Random, int], dict[str, Any]]
    check: Callable[[str, Path, dict[str, Any]], bool]
    lead: Callable[[Policy, Any], list[Any]]
    part: Callable[[list[str]], str] = lambda lines: "none"


TASKS = (
    Task(
        "needle",
        "Somewhere under docs/ one line says what the code is. Find it and answer with the code alone.",
        build_needle, check_code, lead_needle, part_needle,
    ),
    Task(
        "tally",
        "Count the lines under logs/ that contain ERROR. Answer with the number alone.",
        build_tally, check_count, lead_tally, part_tally,
    ),
    Task(
        "trail",
        "Start at hops/start.md and follow each 'Next:' line to the end of the trail. "
        "Answer with what the last file says the answer is, alone.",
        build_trail, check_secret, lead_trail,
    ),
)


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A model to evaluate: ``MODEL[@URL][#KEY_ENV]``; empty parts come from
    the config and the environment."""

    label: str
    llm: str = ""
    url: str = ""
    key_env: str = ""

    @classmethod
    def parse(cls, spec: str) -> "Target":
        body, _, key_env = spec.strip().partition("#")
        llm, _, url = body.partition("@")
        return cls(body or "configured", llm.strip(), url.strip(), key_env.strip())

    def overrides(self) -> dict[str, Any]:
        """The config this target runs with, over the layers."""
        options: dict[str, Any] = {}
        if self.llm:
            options["inference_llm"] = self.llm
        if self.url:
            options["inference_url"] = self.url
        if self.key_env:
            options["inference_key"] = os.environ.get(self.key_env, "")
        elif self.url and (urllib.parse.urlparse(self.url).hostname or "") in LOOPBACK:
            options["inference_key"] = ""  # a local server needs none: never send one there
        return options


LOOPBACK = ("localhost", "127.0.0.1", "::1")
#: The target of `--scripted`.
SCRIPTED = Target("scripted")
#: The window the scripted policy runs in: small, so slicing and compaction engage.
SCRIPTED_WINDOW = 16_000


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """One run of one task on one target, as measured."""

    task: str
    target: str
    seed: int
    success: bool = False
    answer: str = ""
    error: str | None = None
    #: The most context the leader held, in tokens, and as a share of its budget.
    peak: int = 0
    fill: float = 0.0
    usable: int = 0
    seconds: float = 0.0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    #: Tool calls of the leader, of its workers, and the map parts they ran in.
    calls: int = 0
    nested: int = 0
    parts: int = 0
    compactions: int = 0
    #: Branches folded by the leader and by its workers.
    folds: int = 0
    nested_folds: int = 0
    facts: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        """The run in one line, for the log."""
        state = "ok" if self.success else ("error" if self.error else "wrong")
        return (
            f"[eval] {self.task:<8} {self.target:<24} seed {self.seed:<3} {state:<5} "
            f"peak {A.compact_tokens(self.peak):>5} ({self.fill:.0%}) "
            f"{self.seconds:6.1f}s calls {self.calls}+{self.nested} "
            f"folds {self.folds}+{self.nested_folds} "
            f"compactions {self.compactions}"
        )


class Recorder:
    """Measures a run from its events: the leader's context, calls and folds
    (and its workers' calls and folds)."""

    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome

    def __call__(self, event: A.Event) -> None:
        data, outcome = event.data, self.outcome
        nested = "parent" in data
        if event.type == A.EventType.CONTEXT_USAGE and not nested:
            outcome.peak = max(outcome.peak, int(data.get("tokens") or 0))
            outcome.fill = max(outcome.fill, float(data.get("fill") or 0.0))
            outcome.usable = int(data.get("usable") or outcome.usable)
        elif event.type == A.EventType.TOOL_START:
            name = str(data.get("name") or "")
            if not nested:
                outcome.calls += 1
            elif name.startswith("part "):
                outcome.parts += 1
            else:
                outcome.nested += 1
        elif event.type == A.EventType.TOOL_END and data.get("name") == "return":
            folded = str(data.get("result") or "").startswith("Folded")
            if nested:
                outcome.nested_folds += folded
            else:
                outcome.folds += folded
        elif event.type == A.EventType.CONTEXT_COMPACT and not nested:
            outcome.compactions += 1


def scripted_engine(task: Task, facts: dict[str, Any], **overrides: Any) -> A.Engine:
    """An engine whose model is the scripted `Policy` for `task`."""
    from agents.testing import ScriptedModel

    policy = Policy(task, facts)

    class PolicyModel(ScriptedModel):
        """A model whose every reply the policy derives from the call."""

        def __init__(self) -> None:
            super().__init__()
            self.enqueue({"responder": self.respond})

        def respond(self, call: Any) -> list[Any]:
            self.enqueue({"responder": self.respond})  # the next call is answered too
            return policy(call)

    model = PolicyModel()

    class Scripted(A.Engine):
        def build_model(self, config: A.AgentConfig, role: str = A.LEADER_ROLE) -> Any:
            return model

    options = dict(
        env=False,
        config_file=False,
        context_window=SCRIPTED_WINDOW,
        inference_ctx=SCRIPTED_WINDOW,  # known: no provider is asked for it
        context_tokenizer="",
        context_summary=False,
    )
    return Scripted(**{**options, **overrides})


async def run_task(
    task: Task,
    target: Target,
    *,
    seed: int = 0,
    scale: int = 1,
    scripted: bool = False,
    **overrides: Any,
) -> Outcome:
    """Build the workspace of `task` from `seed`, run it on `target`, measure it."""
    outcome = Outcome(task.name, target.label, seed)
    rng = random.Random(f"{task.name}:{seed}")
    # an eval answers alone: nobody is there to answer a question, and a shell
    # would let a model grep its way past the reading the eval is about
    options = {"ask_enabled": False, "shell_enabled": False, "console": False, **overrides}
    with tempfile.TemporaryDirectory(prefix=f"agentic-eval-{task.name}-") as tmp:
        root = Path(tmp)
        outcome.facts = facts = task.build(root, rng, scale)
        engine = (
            scripted_engine(task, facts, **options)
            if scripted
            else A.Engine(**{**target.overrides(), **options})
        )
        engine.events.on(A.EventType.ALL, Recorder(outcome))
        started = time.monotonic()
        try:
            result = await engine.run(task.prompt, workspace=root)
        finally:
            await engine.aclose()
        outcome.seconds = round(time.monotonic() - started, 2)
        outcome.answer = result.output.strip()
        outcome.error = result.error
        outcome.success = result.ok and task.check(outcome.answer, root, facts)
        usage = result.usage
        outcome.requests, outcome.cost = usage.requests, round(usage.cost or 0.0, 6)
        outcome.input_tokens, outcome.output_tokens = usage.input_tokens, usage.output_tokens
    return outcome


async def evaluate(
    tasks: Iterable[Task],
    targets: Iterable[Target],
    *,
    runs: int = 1,
    seed: int = 0,
    scale: int = 1,
    scripted: bool = False,
    report: Callable[[Outcome], None] | None = None,
    **overrides: Any,
) -> list[Outcome]:
    """Every task on every target, `runs` times each (seeds `seed`, `seed + 1`, ...)."""
    outcomes = []
    for task in tasks:
        for target in targets:
            for run in range(max(int(runs), 1)):
                outcome = await run_task(
                    task, target, seed=seed + run, scale=scale, scripted=scripted, **overrides
                )
                if report is not None:
                    report(outcome)
                outcomes.append(outcome)
    return outcomes


def summarize(outcomes: Iterable[Outcome]) -> list[dict[str, Any]]:
    """Per task and target: runs, success rate, median and worst peak, median time, cost."""
    groups: dict[tuple[str, str], list[Outcome]] = {}
    for outcome in outcomes:
        groups.setdefault((outcome.task, outcome.target), []).append(outcome)
    rows = []
    for (task, target), group in groups.items():
        rows.append({
            "task": task,
            "target": target,
            "runs": len(group),
            "success": round(sum(o.success for o in group) / len(group), 3),
            "peak": int(statistics.median(o.peak for o in group)),
            "peak_max": max(o.peak for o in group),
            "fill_max": round(max(o.fill for o in group), 3),
            "seconds": round(statistics.median(o.seconds for o in group), 2),
            "cost": round(sum(o.cost for o in group), 6),
        })
    return rows


def table(rows: list[dict[str, Any]]) -> str:
    """Summary rows as an aligned plain text table."""
    if not rows:
        return "(no runs)"
    head = ["task", "target", "runs", "success", "peak", "peak max", "fill max", "time", "cost"]
    body = [
        [
            row["task"], row["target"], str(row["runs"]), f"{row['success']:.0%}",
            A.compact_tokens(row["peak"]), A.compact_tokens(row["peak_max"]),
            f"{row['fill_max']:.0%}", f"{row['seconds']:.1f}s", f"{row['cost']:.4f}",
        ]
        for row in rows
    ]
    widths = [max(len(cell) for cell in column) for column in zip(head, *body)]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(line, widths)) for line in (head, *body)]
    return "\n".join(line.rstrip() for line in lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="eval", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--model", action="append", default=[],
        help="a target MODEL[@URL][#KEY_ENV] (repeatable, default: the configured leader)",
    )
    parser.add_argument(
        "--task", action="append", choices=[task.name for task in TASKS],
        help="run only this task (repeatable, default: all of them)",
    )
    parser.add_argument("--runs", type=int, default=1, help="runs per task and target")
    parser.add_argument("--seed", type=int, default=0, help="seed of the first run")
    parser.add_argument("--scale", type=int, default=1, help="size of every workspace")
    parser.add_argument("--window", type=int, default=0, help="context window to run in (0: the model's)")
    parser.add_argument("--shell", action="store_true", help="give the model the shell too")
    parser.add_argument("--scripted", action="store_true", help="no model: the scripted policy")
    parser.add_argument("--out", default="", help="append every run as a JSON line to this file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the evals, log every run, print the summary. Scripted runs that fail exit 1."""
    args = parse_args(argv)
    tasks = [task for task in TASKS if not args.task or task.name in args.task]
    targets = [SCRIPTED] if args.scripted else [Target.parse(s) for s in args.model] or [Target.parse("")]
    overrides: dict[str, Any] = {"shell_enabled": True} if args.shell else {}
    if args.window:
        overrides["context_window"] = args.window
    out = Path(args.out).expanduser() if args.out else None

    def report(outcome: Outcome) -> None:
        print(outcome.line(), flush=True)
        if out is not None:
            with out.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(outcome)) + "\n")

    outcomes = asyncio.run(
        evaluate(
            tasks, targets, runs=args.runs, seed=args.seed, scale=args.scale,
            scripted=args.scripted, report=report, **overrides,
        )
    )
    print(table(summarize(outcomes)))
    return 1 if args.scripted and not all(o.success for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
