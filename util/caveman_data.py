"""Caveman SFT data: shrink an agent's reasoning, keep every action.

Pipeline: **fetch → shrink → check → replay → mix → train → eval**
- fetch:  seeded sample of agent traces (HF dataset viewer, or parquet shards for full scale)
- shrink: an LLM rewrites each assistant turn's think / analysis / plan at a caveman level
- check:  deterministic gates; a turn that fails keeps its original text byte for byte
- replay: a verifier model must derive the same commands from the shrunk reasoning
- mix:    one SFT set, one level per trace, a share of untagged originals, a held-out split
- train:  LoRA SFT on a student (util/caveman_train.py, any CUDA box)
- eval:   held-out next action per budget tag: command similarity vs reasoning tokens

Run:
    uv run python util/caveman_data.py fetch --limit 50
    uv run python util/caveman_data.py shrink --level full
    uv run python util/caveman_data.py stats --level full

Rules adapted from caveman (https://github.com/JuliusBrussee/caveman, Apache-2.0).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import itertools
import json
import logging
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator

log = logging.getLogger("caveman")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "caveman"
SOURCE, TRAIN, TURNS, STATS = "source.jsonl", "train.jsonl", "turns.jsonl", "stats.json"
SHARDS, MIX, PLAN, EVAL = "parquet", "mix", "plan.json", "eval.jsonl"

DATASET = "open-thoughts/OpenThoughts-Agent-SFT-100K"
VIEWER = "https://datasets-server.huggingface.co"
VERSION = "caveman-agent-v3"  # v3: hard turns from line-anchored failures in terminal output only
# Compressor comes from .env (AGENTIC_INFERENCE_URL / _KEY / _LLM: OpenRouter); local Ollama via CAVEMAN_URL / CAVEMAN_LLM:
# DEFAULT_URL = "http://10.0.0.6:11434/v1"
# DEFAULT_LLM = "huihui_ai/Qwen3.8-abliterated:27b"

PROSE = ("analysis", "plan")
FIELDS = ("think", *PROSE)
LEVELS = ("lite", "full", "ultra")  # gentle → terse
CLIP = 160  # chars per command shown to the compressor
CONTEXT = 3000  # chars of task / terminal output shown to the verifier
HARD_TOKENS = 400  # prose above ~p90 of sampled turns marks a hard turn
REPLAY_PASS = 0.8  # command similarity that passes outright
REPLAY_SLACK = 0.1  # allowed drop below the original reasoning's similarity
BUDGET = "Reasoning budget: {level}"

Ask = Callable[[list[dict]], Awaitable[str]]
Count = Callable[[str], int]
Weights = list[tuple[str, str, float]]

RULES = """\
You compress one step of an AI agent's reasoning on a terminal task.
Style: caveman, adapted for reasoning. Every fact stays. Only fluff dies.

Input JSON fields:
- level: how hard to compress (see Levels)
- think: private reasoning before acting (may be empty)
- analysis: what the terminal shows and what it means
- plan: what the next commands do and why
- commands: read-only context; never rewrite or output them

Output JSON only: {"think": str, "analysis": str, "plan": str}. Empty input field -> empty output field.

Levels:
- lite: keep articles and full sentences; cut filler, hedging and repetition only.
- full: short words, fragments fine; drop articles when the sentence still reads in one pass.
- ultra: telegraphic fragments; drop articles, pronouns and linking verbs; common short forms fine (dir, config, env, repo, deps); "->" for cause or sequence.

Rules:
1. Payload verbatim: commands, paths, file names, identifiers, flags, numbers, versions and error text, character for character.
2. Never drop not/never/no/only/except. Numbers and units exact.
3. Kill ceremony: no "Let me", "I will", "I need to", "Now", "Great", "Perfect", recap, hedging, just/really/basically/actually/simply.
4. One idea per sentence, 20 words max. Keep step order. No emoji. Below ultra: no arrows, no invented abbreviations (cfg, impl, fn, pkg).
5. Say each fact once. think = reasoning (cause, hypothesis, decision). analysis = observed state. plan = next steps.
6. Keep reasoning that changes the decision: hypotheses, checks, risks, why one option beats another. Drop narration of the obvious.
7. Never add facts, guesses or steps that are not in the input.
8. Never perform caveman: no "me think", no "caveman". Not shorter? Keep the original wording.
"""

REPLAY = """\
You are an AI agent solving a task in a Linux terminal.
Input: the task, the latest terminal output, and your reasoning for this step.
Write the shell commands this reasoning leads to. Follow its plan exactly; add no steps it does not imply.
Output JSON only: {"commands": [{"keystrokes": str}]}. Empty list when the plan runs nothing.
"""


# ---------------------------------------------------------------- turn format


@dataclass
class Turn:
    """One Terminus-2 assistant turn: optional <think> plus a JSON action body."""

    think: str | None
    body: dict[str, Any]
    sep: str = ""
    fence: str = ""  # "```json\n" when the body sits in a code fence

    @property
    def prose(self) -> dict[str, str]:
        return {"think": self.think or "", **{k: self.body[k] for k in PROSE}}

    def with_prose(self, prose: dict[str, str]) -> Turn:
        body = {**self.body, **{k: prose[k] for k in PROSE}}
        think = None if self.think is None else prose["think"]
        return replace(self, think=think, body=body)


_THINK = re.compile(r"\s*<think>(.*?)</think>(\s*)", re.S)
_FENCE = re.compile(r"```(?:json)?[ \t]*\n")


def split_turn(content: str) -> Turn | None:
    """Parse `<think>…</think>{json}` (body optionally ```json fenced); None for any other shape."""
    think, sep, rest = None, "", content
    match = _THINK.match(content)
    if match:
        think, sep, rest = match.group(1), match.group(2), content[match.end():]
    if "<think>" in rest or "</think>" in rest:
        return None
    text = rest.strip()
    fence = _FENCE.match(text)
    if fence:
        text = text[fence.end():]
    if not text.startswith("{"):
        return None
    try:
        body, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    if text[end:].strip() != ("```" if fence else "") or not isinstance(body, dict):
        return None
    if not all(isinstance(body.get(k), str) for k in PROSE):
        return None
    return Turn(think, body, sep, fence.group(0) if fence else "")


def join_turn(turn: Turn) -> str:
    body = json.dumps(turn.body, indent=2, ensure_ascii=False)
    body = f"{turn.fence}{body}\n```" if turn.fence else body
    return body if turn.think is None else f"<think>{turn.think}</think>{turn.sep}{body}"


def prose_text(turn: Turn) -> str:
    return "\n".join(v for v in turn.prose.values() if v)


def keystrokes(body: dict[str, Any]) -> list[str]:
    commands = body.get("commands")
    return [
        str(c.get("keystrokes", "")) if isinstance(c, dict) else str(c)
        for c in (commands if isinstance(commands, list) else [])
    ]


# ---------------------------------------------------------------- fact checks

_CODE = re.compile(r"`([^`\n]+)`")
_ENUM = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+")
_STEP = re.compile(r"(?i)\b(?:step|phase|option|part)\s+\d+\b")
_FACT_RES = (
    re.compile(r"(?<![\w/~.])(?:~|\.{1,2})?/[\w.@%+=:-]+(?:/[\w.@%+=:-]*)*"),  # /abs, ./rel, ~/home
    re.compile(r"\b[\w.-]+(?:/[\w.@%+=-]+)+/?"),  # rel/path
    re.compile(r"(?<![\w-])--?[A-Za-z][\w-]*"),  # flags
    re.compile(r"\b\d+(?:[.:]\d+)*\b"),  # numbers, versions, times
    re.compile(r"\b(?=\w*\d)(?=\w*[A-Za-z])\w+\b"),  # sha256, x86_64
    re.compile(r"\b[A-Za-z]\w*_\w+\b"),  # snake_case
    re.compile(r"\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z0-9]+[A-Z]\w*\b"),  # camelCase, PascalCase
    re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?:\(\))?"),  # dotted.names, file.ext
)
_SKIP = {"e.g", "i.e", "etc", "vs"}
_NEGATIONS = {  # "didn't find any" -> "none found" swaps form, not meaning: one class
    "not": re.compile(
        r"\bnot\b|n['’]t\b|\bcannot\b"
        r"|\b(?:no|none|nothing|nobody|without|zero|missing|absent|empty|silent)\b",
        re.I,
    ),
    "never": re.compile(r"\bnever\b", re.I),
    "only": re.compile(r"\b(?:only|solely)\b", re.I),
    "except": re.compile(r"\b(?:except|excluding)\b", re.I),
}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def facts(text: str) -> set[str]:
    """Payload tokens that must survive verbatim: code, paths, flags, numbers, identifiers."""
    found = {_squash(m) for m in _CODE.findall(text)}
    plain = _STEP.sub(" ", _ENUM.sub("", _CODE.sub(lambda m: f" {m.group(1)} ", text)))
    for pattern in _FACT_RES:
        for token in pattern.findall(plain):
            token = token.rstrip(".,:;!?)").lstrip("(")
            if len(token) > 1 or token.isdigit():
                found.add(token)
    return {f for f in found if f and f.lower() not in _SKIP}


def present(fact: str, text: str) -> bool:
    head = r"(?<!\w)" if fact[0].isalnum() or fact[0] == "_" else ""
    tail = r"(?!\w)" if fact[-1].isalnum() or fact[-1] == "_" else ""
    return re.search(head + re.escape(fact) + tail, _squash(text)) is not None


def negations(text: str) -> set[str]:
    return {name for name, pattern in _NEGATIONS.items() if pattern.search(text)}


@dataclass
class Replay:
    """Verifier result: command similarity from shrunk (`score`) and original (`base`) reasoning."""

    score: float
    base: float | None = None
    ok: bool = True


@dataclass
class Check:
    reasons: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    negations: list[str] = field(default_factory=list)
    detail: str = ""
    replay: Replay | None = None


def check_turn(before: Turn, after: Turn, count: Count) -> Check:
    """Deterministic gates; an empty `reasons` list means the rewrite is safe to keep."""
    check = Check()
    strip = lambda t: {k: v for k, v in t.body.items() if k not in PROSE}  # noqa: E731
    if strip(before) != strip(after) or (before.think is None) != (after.think is None):
        check.reasons.append("actions")
    old, new = before.prose, after.prose
    if any(old[k].strip() and not new[k].strip() for k in FIELDS):
        check.reasons.append("empty")
    if any("<think>" in v or "</think>" in v for v in new.values()):
        check.reasons.append("tag")
    old_text, new_text = prose_text(before), prose_text(after)
    check.facts = sorted(f for f in facts(old_text) if not present(f, new_text))
    if check.facts:
        check.reasons.append("facts")
    check.negations = sorted(negations(old_text) - negations(new_text))
    if check.negations:
        check.reasons.append("negation")
    if count(new_text) >= count(old_text):
        check.reasons.append("longer")
    return check


# ---------------------------------------------------------------- levels

_ERROR = re.compile(  # failure signals, line-anchored: an `error` word in printed source or prose is no failure
    r"Traceback \(most recent call last\)|command not found|No such file or directory|Permission denied"
    r"|Segmentation fault|core dumped|npm ERR!|make(?:\[\d+\])?: \*\*\*"
    r"|(?m:^(?:E[ \t]+)?[\w.]*(?:Error|Exception)(?::|$))"  # KeyError: x, pytest `E   KeyError`
    r"|(?m:^(?:error|fatal|ERROR|FATAL|E)(?:\[\w+\])?:)"  # error: x, apt E: x, rustc error[E0599]:
    r"|(?m:^[\w./+-]+:(?:[ \t]*(?:\d+|line \d+):)*[ \t]*(?:fatal |syntax )?error\b)"  # cc a.c:1:2: error, bash: syntax error
    r"|(?m:^(?:FAIL|FAILED|ERROR)\b)|(?m:\.\.\. (?:FAIL|ERROR)$)|\b[1-9]\d* (?:failed|errors?)(?![\w.])"
)
_ECHO = re.compile(r"(?m)^(?:\(\w+\) )?\S+@\S+:[^\n]*?[#$] .*$|^> .*$")  # prompt + typed command, heredoc lines
_OUTPUT = ("New Terminal Output:", "Current terminal state:")  # Terminus-2 marks; text before is task or warnings


def gentler(level: str) -> str:
    return LEVELS[max(0, LEVELS.index(level) - 1)]


def terminal_output(message: str) -> str:
    """What the terminal printed: no task text, no harness format warnings, no echoed input."""
    for mark in _OUTPUT:
        _, found, tail = message.partition(mark)
        if found:
            return _ECHO.sub("", tail)
    return "" if message.startswith("Previous response had") else _ECHO.sub("", message)


def is_hard(observation: str, turn: Turn, count: Count) -> bool:
    """Hard turn: the terminal shows a failure, or the original reasoning is long. Gets one level gentler."""
    return bool(_ERROR.search(terminal_output(observation)[-CONTEXT:])) or count(prose_text(turn)) > HARD_TOKENS


def tag_budget(conversation: list[dict], level: str) -> list[dict]:
    """Budget tag as the system prompt: the student learns one style per tag."""
    tag = BUDGET.format(level=level)
    if conversation and conversation[0].get("role") == "system":
        first = conversation[0]
        return [{**first, "content": f"{tag}\n\n{first.get('content') or ''}"}, *conversation[1:]]
    return [{"role": "system", "content": tag}, *conversation]


# ---------------------------------------------------------------- model calls


def build_messages(turn: Turn, level: str = "full") -> list[dict]:
    payload = {
        "level": level,
        **turn.prose,
        "commands": [k if len(k) <= CLIP else k[:CLIP] + "…" for k in keystrokes(turn.body)],
    }
    return [
        {"role": "system", "content": RULES},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)},
    ]


def _json_reply(text: str) -> Any:
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except ValueError:
        return None


def parse_reply(text: str) -> dict[str, str] | None:
    data = _json_reply(text)
    if not isinstance(data, dict):
        return None
    data.setdefault("think", "")
    if not all(isinstance(data.get(k), str) for k in FIELDS):
        return None
    return {k: data[k].strip() for k in FIELDS}


def feedback(check: Check, level: str) -> str:
    lines = [f"Check failed ({', '.join(check.reasons)}). Rewrite the same input at level {level}."]
    if check.facts:
        lines.append("- Keep verbatim: " + ", ".join(f"`{f}`" for f in check.facts))
    if check.negations:
        lines.append("- Keep meaning words: " + ", ".join(check.negations))
    if "longer" in check.reasons:
        lines.append("- Output must be shorter than input.")
    if "replay" in check.reasons:
        lines.append("- A verifier could not derive the same commands from your text. Keep every step, target and value the commands need.")
    if "reply" in check.reasons:
        lines.append('- Reply with JSON only: {"think": str, "analysis": str, "plan": str}.')
    return "\n".join(lines)


# ---------------------------------------------------------------- replay check

Verify = Callable[[Turn], Awaitable[Replay]]


def task_text(task: str) -> str:
    """Task description without the Terminus-2 harness preamble."""
    head, mark, tail = task.partition("Task Description:")
    return (tail if mark else head).strip()


def replay_messages(task: str, observation: str, turn: Turn) -> list[dict]:
    """Verifier prompt: task + latest output + reasoning; never the commands themselves."""
    goal = task_text(task)[:CONTEXT]
    seen = observation[-CONTEXT:]
    reasoning = json.dumps(turn.prose, ensure_ascii=False, indent=1)
    text = f"Task:\n{goal}\n\nTerminal output:\n{seen}\n\nReasoning:\n{reasoning}"
    return [{"role": "system", "content": REPLAY}, {"role": "user", "content": text}]


def parse_commands(text: str) -> list[str]:
    data = _json_reply(text)
    return keystrokes(data) if isinstance(data, dict) else []


def similarity(predicted: list[str], actual: list[str]) -> float:
    """Whitespace-blind similarity of two command lists (1.0 = same keystrokes)."""
    norm = lambda keys: "\n".join(s for s in (_squash(k) for k in keys) if s)[:CONTEXT]  # noqa: E731
    a, b = norm(predicted), norm(actual)
    if a == b:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)


def make_verify(ask: Ask, task: str, observation: str, before: Turn) -> Verify:
    """Replay gate for one turn: pass at REPLAY_PASS, else no worse than the original reasoning."""
    actual = keystrokes(before.body)
    base: list[float] = []  # original reasoning's score, computed once and only when needed

    async def score(turn: Turn) -> float:
        return similarity(parse_commands(await ask(replay_messages(task, observation, turn))), actual)

    async def verify(after: Turn) -> Replay:
        if not any(k.strip() for k in actual):
            return Replay(1.0)
        got = await score(after)
        if got >= REPLAY_PASS:
            return Replay(got)
        if not base:
            base.append(await score(before))
        return Replay(got, base[0], got >= base[0] - REPLAY_SLACK)

    return verify


# ---------------------------------------------------------------- shrink

_GENTLER = {"replay"}  # retry one level gentler; a named lost fact retries at the same level


@dataclass
class Shrunk:
    turn: Turn
    ok: bool
    tries: int
    check: Check
    draft: dict[str, str] | None = None
    level: str = "full"
    attempts: list[list[str]] = field(default_factory=list)  # reasons per try ([] = passed)


async def shrink_turn(
    turn: Turn, ask: Ask, count: Count, tries: int = 2, level: str = "full", verify: Verify | None = None
) -> Shrunk:
    """Rewrite one turn; retry with the gates' findings; fall back to the original."""
    messages, check, draft, attempts = build_messages(turn, level), Check(["reply"]), None, []
    for attempt in range(1, tries + 1):
        try:
            reply = await ask(messages)
        except Exception as error:  # model/network failure keeps the original turn
            return Shrunk(turn, False, attempt, Check(["error"], detail=repr(error)), draft, level, [*attempts, ["error"]])
        prose = parse_reply(reply)
        if prose is None:
            check = Check(["reply"], detail=(reply or "")[:200])
        else:
            draft, after = prose, turn.with_prose(prose)
            check = check_turn(turn, after, count)
            if not check.reasons and verify:
                try:
                    check.replay = await verify(after)
                except Exception as error:
                    return Shrunk(turn, False, attempt, Check(["error"], detail=repr(error)), draft, level, [*attempts, ["error"]])
                if not check.replay.ok:
                    check.reasons.append("replay")
        attempts.append(list(check.reasons))
        if prose is not None and not check.reasons:
            return Shrunk(after, True, attempt, check, draft, level, attempts)
        if attempt < tries:
            level = gentler(level) if _GENTLER & set(check.reasons) else level
            messages = [*messages, {"role": "assistant", "content": reply}, {"role": "user", "content": feedback(check, level)}]
    return Shrunk(turn, False, tries, check, draft, level, attempts)


async def shrink_trace(
    row: dict,
    ask: Ask,
    count: Count,
    *,
    workers: int = 2,
    model: str = "",
    level: str = "full",
    verifier: Ask | None = None,
    tag: bool = False,
    weight: float = 1.0,
) -> tuple[dict, list[dict]]:
    """Shrink every assistant turn of one trace; non-assistant turns pass through untouched."""
    conversation = row.get("conversations") or []
    gate = asyncio.Semaphore(max(1, workers))
    task = next((m.get("content") or "" for m in conversation if m.get("role") == "user"), "")

    async def one(index: int, content: str) -> tuple[int, str, dict]:
        record = {"row_idx": row.get("row_idx"), "turn": index}
        turn = split_turn(content)
        if turn is None:
            size = count(content)
            return index, content, {
                **record, "ok": False, "tries": 0, "reasons": ["unparsed"], "level": None, "attempts": [], "hard": None,
                "replay": None, "prose_tokens": None, "content_tokens": [size, size], "secs": 0.0,
            }
        observation = (conversation[index - 1].get("content") or "") if index else ""
        hard = is_hard(observation, turn, count)
        verify = make_verify(verifier, task, observation, turn) if verifier else None
        async with gate:
            started = time.perf_counter()
            result = await shrink_turn(turn, ask, count, level=gentler(level) if hard else level, verify=verify)
            secs = time.perf_counter() - started
        new = join_turn(result.turn) if result.ok else content
        return index, new, {
            **record,
            "ok": result.ok,
            "tries": result.tries,
            "reasons": result.check.reasons,
            "level": result.level,
            "attempts": result.attempts,
            "hard": hard,
            "replay": asdict(result.check.replay) if result.check.replay else None,
            "missing": {"facts": result.check.facts, "negations": result.check.negations},
            "detail": result.check.detail,
            "before": turn.prose,
            "after": result.turn.prose,
            "draft": result.draft,
            "prose_tokens": [count(prose_text(turn)), count(prose_text(result.turn))],
            "content_tokens": [count(content), count(new)],
            "secs": round(secs, 2),
        }

    jobs = [one(i, m.get("content") or "") for i, m in enumerate(conversation) if m.get("role") == "assistant"]
    results = await asyncio.gather(*jobs)
    updated = [dict(m) for m in conversation]
    for index, content, _ in results:
        updated[index]["content"] = content
    records = [r for _, _, r in results]
    parsed = [r for r in records if r["prose_tokens"]]
    meta = {
        "version": VERSION,
        "model": model,
        "level": level,
        "weight": weight,
        "turns": len(records),
        "parsed": len(parsed),
        "shrunk": sum(r["ok"] for r in records),
        "hard": sum(bool(r["hard"]) for r in parsed),
        "prose_tokens": [sum(r["prose_tokens"][i] for r in parsed) for i in (0, 1)],
        "content_tokens": [sum(r["content_tokens"][i] for r in records) for i in (0, 1)],
    }
    updated = tag_budget(updated, level) if tag else updated
    return {**row, "conversations": updated, "caveman": meta}, records


# ---------------------------------------------------------------- filter / weight


def parse_weights(specs: Iterable[str]) -> Weights:
    """`FIELD=VALUE:WEIGHT` specs; VALUE `None` matches a missing or null field."""
    rules: Weights = []
    for spec in specs:
        key, eq, rest = spec.partition("=")
        value, colon, weight = rest.rpartition(":")
        try:
            if not (key and eq and colon):
                raise ValueError
            rules.append((key, value, float(weight)))
        except ValueError:
            raise ValueError(f"bad weight {spec!r}: expected FIELD=VALUE:WEIGHT") from None
    return rules


def row_weight(row: dict, rules: Weights) -> float:
    """Product of matching rule weights (1.0 when none match); 0 drops the row."""
    weight = 1.0
    for key, value, factor in rules:
        if str(row.get(key)) == value:
            weight *= factor
    return weight


# ---------------------------------------------------------------- statistics


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[round(q * (len(ordered) - 1))], 3)


def _saved(before: int, after: int) -> dict[str, float]:
    return {"before": before, "after": after, "saved_pct": round(100 * (1 - after / before), 1) if before else 0.0}


def summarize(records: Iterable[dict]) -> dict:
    """Stream turn records into run statistics; a (row, turn) seen twice counts once."""
    seen: set[tuple] = set()
    traces: set = set()
    turns = parsed = shrunk = retried = rescued = hard = 0
    prose, content = [0, 0], [0, 0]
    reasons: dict[str, int] = {}
    levels: dict[str, int] = {}
    replay = {"checked": 0, "failed": 0, "based": 0}
    ratios: list[float] = []
    secs: list[float] = []
    scores: list[float] = []
    for r in records:
        key = (r.get("row_idx"), r.get("turn"))
        if key in seen:
            continue
        seen.add(key)
        traces.add(r.get("row_idx"))
        turns += 1
        content = [content[i] + r["content_tokens"][i] for i in (0, 1)]
        if not r.get("prose_tokens"):
            continue
        parsed += 1
        prose = [prose[i] + r["prose_tokens"][i] for i in (0, 1)]
        secs.append(r["secs"])
        hard += bool(r.get("hard"))
        retried += r["tries"] > 1
        rescued += r["tries"] > 1 and r["ok"]
        if r.get("replay"):
            replay["checked"] += 1
            replay["failed"] += not r["replay"]["ok"]
            replay["based"] += r["replay"].get("base") is not None
            scores.append(r["replay"]["score"])
        if r["ok"]:
            shrunk += 1
            if r.get("level"):
                levels[r["level"]] = levels.get(r["level"], 0) + 1
            if r["prose_tokens"][0]:
                ratios.append(r["prose_tokens"][1] / r["prose_tokens"][0])
        else:
            for reason in r["reasons"]:
                reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "version": VERSION,
        "traces": len(traces),
        "turns": turns,
        "unparsed": turns - parsed,
        "shrunk": shrunk,
        "kept": parsed - shrunk,
        "shrink_rate": round(shrunk / parsed, 3) if parsed else 0.0,
        "retried": retried,
        "rescued": rescued,
        "hard": hard,
        "levels": {k: levels[k] for k in LEVELS if k in levels},
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "replay": {**replay, "score_p50": _pct(scores, 0.5)},
        "prose_tokens": _saved(*prose),
        "content_tokens": _saved(*content),
        "prose_ratio": {f"p{q}": _pct(ratios, q / 100) for q in (10, 50, 90)},
        "secs_per_turn_p50": _pct(secs, 0.5),
    }


# ---------------------------------------------------------------- I/O

_ROW_IDX = re.compile(r'\{"row_idx": (-?\d+)[,}]')


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Stream rows; a broken line (crash mid-write) is skipped with a warning."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError:
                log.warning("skip broken line %d in %s", number, path)


def read_jsonl(path: Path) -> list[dict]:
    return list(iter_jsonl(path))


def row_ids(path: Path) -> set:
    """`row_idx` of every complete line; fast path skips parsing rows that lead with it."""
    ids: set = set()
    if not path.exists():
        return ids
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            match = _ROW_IDX.match(line)
            if match and line.endswith("\n"):
                ids.add(int(match.group(1)))
            elif line.strip():
                try:
                    ids.add(json.loads(line).get("row_idx"))
                except ValueError:
                    pass
    return ids


def _trim_partial(path: Path) -> None:
    """Drop a half-written last line so appends start on a clean line."""
    if not path.exists() or not path.stat().st_size:
        return
    with path.open("rb+") as handle:
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return
        pos = handle.seek(0, os.SEEK_END)
        while pos > 0:
            step = min(1 << 16, pos)
            pos -= step
            handle.seek(pos)
            cut = handle.read(step).rfind(b"\n")
            if cut >= 0:
                pos += cut + 1
                break
        handle.truncate(pos)
    log.warning("trimmed half-written last line of %s", path)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def write_jsonl(path: Path, rows: Iterable[dict], mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "a":
        _trim_partial(path)
    with path.open(mode, encoding="utf-8", newline="\n") as handle:
        for item in rows:
            handle.write(json.dumps(item, ensure_ascii=False, default=_jsonable) + "\n")


def _urlopen(url: str):
    return urllib.request.urlopen(url, timeout=60)


def _backoff(error: Exception, attempt: int, wait: float) -> float:
    """Seconds to sleep: the server's Retry-After when given, else exponential (capped)."""
    headers = getattr(error, "headers", None)
    after = headers.get("Retry-After") if headers else None
    if after and after.strip().isdigit():
        return min(float(after), 120.0)
    return min(wait * 2 ** (attempt - 1), 60.0)


def _get_json(url: str, opener=None, tries: int = 6, wait: float = 2.0) -> Any:
    opener = opener or _urlopen
    for attempt in range(1, tries + 1):
        try:
            with opener(url) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:
            if attempt == tries:
                raise
            delay = _backoff(error, attempt, wait)
            log.warning("GET failed (%s), retry %d/%d in %.0fs: %s", error, attempt, tries - 1, delay, url)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _query(dataset: str, **extra: Any) -> str:
    return urllib.parse.urlencode({"dataset": dataset, "config": "default", "split": "train", **extra})


def dataset_size(dataset: str = DATASET, opener=None) -> int:
    size = _get_json(f"{VIEWER}/size?{urllib.parse.urlencode({'dataset': dataset})}", opener)["size"]
    for split in size.get("splits") or []:
        if split.get("config") == "default" and split.get("split") == "train":
            return int(split["num_rows"])
    return int(size["dataset"]["num_rows"])


def sample_offsets(total: int, limit: int, seed: int) -> list[int]:
    return sorted(random.Random(seed).sample(range(total), min(limit, total)))


def fetch_rows(
    offsets: Iterable[int],
    dataset: str = DATASET,
    opener=None,
    tries: int = 6,
    wait: float = 2.0,
    pace: float = 0.0,
) -> Iterator[dict]:
    """Yield `{"row_idx", **row}` per offset; rows the viewer truncated are skipped."""
    for number, offset in enumerate(offsets):
        if pace and number:
            time.sleep(pace)
        data = _get_json(f"{VIEWER}/rows?{_query(dataset, offset=offset, length=1)}", opener, tries, wait)
        for item in data.get("rows") or []:
            if item.get("truncated_cells"):
                log.warning("skip row %s: truncated %s", item.get("row_idx"), item["truncated_cells"])
                continue
            yield {"row_idx": item["row_idx"], **item["row"]}


# ---------------------------------------------------------------- parquet (full scale)


def parquet_files(dataset: str = DATASET, opener=None) -> list[dict]:
    """Train shards of the viewer's parquet export, in row order."""
    data = _get_json(f"{VIEWER}/parquet?{urllib.parse.urlencode({'dataset': dataset})}", opener)
    files = [f for f in data.get("parquet_files") or [] if f.get("config") == "default" and f.get("split") == "train"]
    return sorted(files, key=lambda f: f["filename"])


def download(url: str, path: Path, size: int = 0, opener=None, tries: int = 6, wait: float = 2.0) -> Path:
    """Fetch `url` to `path` via a `.part` file; a complete file (size match) is kept as is."""
    if path.exists() and (not size or path.stat().st_size == size):
        return path
    opener = opener or _urlopen
    part = path.with_name(path.name + ".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, tries + 1):
        started = time.perf_counter()
        try:
            with opener(url) as response, part.open("wb") as handle:
                shutil.copyfileobj(response, handle, 1 << 20)
            got = part.stat().st_size
            if size and got != size:
                raise OSError(f"{path.name}: got {got} bytes, expected {size}")
            part.replace(path)
            log.info("downloaded %s: %.0f MB in %.0fs", path.name, got / 1e6, time.perf_counter() - started)
            return path
        except Exception as error:
            part.unlink(missing_ok=True)
            if attempt == tries:
                raise
            delay = _backoff(error, attempt, wait)
            log.warning("download failed (%s), retry %d/%d in %.0fs", error, attempt, tries - 1, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def parquet_total(paths: list[Path]) -> int:
    import pyarrow.parquet as pq

    return sum(pq.ParquetFile(p).metadata.num_rows for p in paths)


def parquet_rows(paths: list[Path], offsets: Iterable[int] | None = None, batch: int = 64) -> Iterator[dict]:
    """Yield `{"row_idx", **row}` across shards in order; only row groups holding `offsets` are read."""
    import pyarrow.parquet as pq

    wanted = None if offsets is None else sorted(set(offsets))
    base = k = 0
    for path in paths:
        shard = pq.ParquetFile(path)
        for group in range(shard.metadata.num_row_groups):
            end = base + shard.metadata.row_group(group).num_rows
            if wanted is not None and (k >= len(wanted) or wanted[k] >= end):
                if k >= len(wanted):
                    return
                base = end
                continue
            for chunk in shard.iter_batches(batch_size=batch, row_groups=[group]):
                stop = base + chunk.num_rows
                if wanted is None:
                    for i, item in enumerate(chunk.to_pylist()):
                        yield {"row_idx": base + i, **item}
                while wanted is not None and k < len(wanted) and wanted[k] < stop:
                    yield {"row_idx": wanted[k], **chunk.slice(wanted[k] - base, 1).to_pylist()[0]}
                    k += 1
                base = stop


# ---------------------------------------------------------------- runners


def token_counter() -> Count:
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("o200k_base")
        return lambda text: len(encoding.encode(text, disallowed_special=()))
    except Exception as error:  # offline without a cached vocabulary
        log.warning("tiktoken unavailable (%s); estimating tokens as chars/4", error)
        return lambda text: (len(text) + 3) // 4


def make_ask(
    url: str,
    key: str,
    model: str,
    effort: str = "none",
    ctx: int = 0,
    timeout: float = 300.0,
    max_tokens: int = 2048,
    as_json: bool = True,
) -> Ask:
    """Chat call at temperature 0 / seed 0; `as_json` asks for a JSON object, else free text with any think kept."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=url, api_key=key or "none", timeout=timeout, max_retries=5)  # 429s at --traces N
    extra: dict[str, Any] = {"reasoning_effort": effort} if effort else {}
    if ctx:
        extra["extra_body"] = {"options": {"num_ctx": ctx}}
    if as_json:
        extra["response_format"] = {"type": "json_object"}

    async def ask(messages: list[dict]) -> str:
        response = await client.chat.completions.create(
            model=model, messages=messages, temperature=0, seed=0, max_tokens=max_tokens, **extra
        )
        message = response.choices[0].message
        text = message.content or ""
        think = None if as_json else getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None)
        return f"<think>{think}</think>{text}" if think and "</think>" not in text else text  # vLLM / OpenRouter split it

    return ask


async def run_shrink(
    out: Path,
    ask: Ask,
    count: Count,
    *,
    source: Path | None = None,
    limit: int = 0,
    workers: int = 2,
    model: str = "",
    level: str = "full",
    verifier: Ask | None = None,
    tag: bool = False,
    weights: Weights = (),
    traces: int = 1,
    plan: Plan | None = None,
) -> dict:
    """Shrink source rows into train/turns JSONL under `out`; resumes past rows already written.

    `traces` run at once (each with `workers` concurrent turns); rows are written in source order.
    `plan` keeps only the rows it assigns to `level`, so a mixed set shrinks each trace once.
    """
    source = source or out / SOURCE
    out.mkdir(parents=True, exist_ok=True)
    done = row_ids(out / TRAIN)
    planned = lambda idx: plan is None or plan.assign(idx) == level  # noqa: E731
    pending = sum(1 for idx in row_ids(source) - done if planned(idx))
    pending = min(pending, limit) if limit else pending
    log.info(
        "shrink: %d done, %d to do -> %s (level %s, model %s, replay %s, tag %s, %d weight rules, %d traces x %d workers%s)",
        len(done), pending, out, level, model or "-", "on" if verifier else "off", tag, len(weights), traces, workers,
        ", planned" if plan else "",
    )
    dropped = 0

    def todo() -> Iterator[tuple[dict, float]]:
        nonlocal dropped
        taken = 0
        for row in iter_jsonl(source):
            if limit and taken >= limit:
                return
            if row.get("row_idx") in done or not planned(row.get("row_idx")):
                continue
            weight = row_weight(row, weights)
            if not weight:
                dropped += 1
                continue
            taken += 1
            yield row, weight

    gate = asyncio.Semaphore(max(1, traces))

    async def one(row: dict, weight: float) -> tuple[dict, list[dict], float]:
        async with gate:
            started = time.perf_counter()
            shrunk, records = await shrink_trace(
                row, ask, count, workers=workers, model=model, level=level, verifier=verifier, tag=tag, weight=weight
            )
            return shrunk, records, time.perf_counter() - started

    begun, number = time.perf_counter(), 0

    def write(shrunk: dict, records: list[dict], secs: float) -> None:
        nonlocal number
        write_jsonl(out / TURNS, records, mode="a")  # turns first: a train row marks the trace done
        write_jsonl(out / TRAIN, [shrunk], mode="a")
        done.add(shrunk.get("row_idx"))
        number += 1
        meta = shrunk["caveman"]
        (pb, pa), (cb, ca) = meta["prose_tokens"], meta["content_tokens"]
        fails = sum(1 for r in records if "replay" in r["reasons"])
        eta = (time.perf_counter() - begun) / number * max(0, pending - number)
        log.info(
            "[%d/%d] row %s: %d/%d turns shrunk (%d hard, %d replay fails), prose %d->%d, content %d->%d tokens, %.0fs, eta %s",
            number, pending, shrunk.get("row_idx"), meta["shrunk"], meta["parsed"], meta["hard"], fails,
            pb, pa, cb, ca, secs, f"{eta / 3600:.1f}h" if eta >= 3600 else f"{eta / 60:.0f}m",
        )

    window: deque[asyncio.Task] = deque()  # started traces, source order; the head is written first
    for row, weight in todo():
        window.append(asyncio.create_task(one(row, weight)))
        while len(window) > 4 * max(1, traces) or (window and window[0].done()):
            write(*await window.popleft())
    while window:
        write(*await window.popleft())
    if dropped:
        log.info("shrink: dropped %d rows by weight 0", dropped)
    stats = summarize(iter_jsonl(out / TURNS))
    (out / STATS).write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


async def run_replay(
    out: Path, source: Path, verifier: Ask, *, name: str = "", limit: int = 50, workers: int = 4
) -> dict:
    """Re-check the shrunk turns of a level with another verifier: does a student-sized model still derive the commands?"""
    records: dict[Any, dict[int, dict]] = {}
    for r in iter_jsonl(out / TURNS):
        if r.get("ok") and r.get("replay") and (r["row_idx"] in records or not limit or len(records) < limit):
            records.setdefault(r["row_idx"], {}).setdefault(r["turn"], r)
    gate = asyncio.Semaphore(max(1, workers))

    async def one(task: str, observation: str, original: Turn, record: dict) -> dict:
        result = {"row_idx": record["row_idx"], "turn": record["turn"], "level": record["level"], "recorded": record["replay"]}
        async with gate:
            try:
                replay = await make_verify(verifier, task, observation, original)(original.with_prose(record["after"]))
            except Exception as error:  # a verifier outage is counted, not fatal
                return {**result, "error": repr(error)}
        return {**result, **asdict(replay)}

    jobs = []
    for row in iter_jsonl(source):
        wanted = records.get(row.get("row_idx"))
        if not wanted:
            continue
        conversation = row.get("conversations") or []
        task = next((m.get("content") or "" for m in conversation if m.get("role") == "user"), "")
        for index, record in sorted(wanted.items()):
            original = split_turn(conversation[index].get("content") or "") if index < len(conversation) else None
            if original and any(k.strip() for k in keystrokes(original.body)):  # no commands: nothing to derive
                observation = (conversation[index - 1].get("content") or "") if index else ""
                jobs.append(one(task, observation, original, record))
    log.info("replay: %d shrunk turns of %d traces in %s, verifier %s", len(jobs), len(records), out, name or "-")
    results = await asyncio.gather(*jobs)
    slug = re.sub(r"[^\w.-]+", "_", name or "verifier")
    write_jsonl(out / f"replay-{slug}.jsonl", results)
    checked = [r for r in results if "error" not in r]
    levels: dict[str, dict[str, int]] = {}
    for r in checked:
        entry = levels.setdefault(r["level"], {"checked": 0, "failed": 0})
        entry["checked"] += 1
        entry["failed"] += not r["ok"]
    stats = {
        "verifier": name,
        "traces": len(records),
        "checked": len(checked),
        "errors": len(results) - len(checked),
        "failed": sum(not r["ok"] for r in checked),
        "pass_rate": round(sum(r["ok"] for r in checked) / len(checked), 3) if checked else 0.0,
        "levels": {k: levels[k] for k in LEVELS if k in levels},
        "score_p50": _pct([r["score"] for r in checked], 0.5),
        "base_p50": _pct([r["base"] for r in checked if r["base"] is not None], 0.5),
        "recorded_score_p50": _pct([r["recorded"]["score"] for r in checked], 0.5),
    }
    (out / f"replay-{slug}.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


# ---------------------------------------------------------------- mix (one SFT set)


@dataclass(frozen=True)
class Plan:
    """Seeded per-trace assignment: held out for eval, kept original (untagged), or one shrink level."""

    levels: tuple[str, ...] = LEVELS
    original: float = 0.1
    holdout: float = 0.05
    seed: int = 0

    def unit(self, salt: str, idx: Any) -> float:
        return random.Random(f"{self.seed}:{salt}:{idx}").random()  # str seed: stable across runs and processes

    def assign(self, idx: Any) -> str:
        if self.unit("holdout", idx) < self.holdout:
            return "eval"
        if self.unit("original", idx) < self.original:
            return "original"
        return self.levels[int(self.unit("level", idx) * len(self.levels))]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Plan:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{**data, "levels": tuple(data["levels"])})


def line_offsets(path: Path) -> dict[Any, int]:
    """Byte offset of each complete line by `row_idx` (first wins): random access into big JSONL."""
    offsets: dict[Any, int] = {}
    if not path.exists():
        return offsets
    with path.open("rb") as handle:
        position = 0
        for line in handle:
            if line.endswith(b"\n") and line.strip():
                match = _ROW_IDX.match(line[:48].decode("utf-8", "ignore"))
                try:
                    idx = int(match.group(1)) if match else json.loads(line).get("row_idx")
                    offsets.setdefault(idx, position)
                except ValueError:
                    pass
            position += len(line)
    return offsets


def _dump(handle, item: dict) -> None:
    handle.write(json.dumps(item, ensure_ascii=False, default=_jsonable) + "\n")


def run_mix(root: Path, plan: Plan, weights: Weights = ()) -> dict:
    """`mix/train.jsonl`: each trace at its planned level, or original and untagged; `mix/eval.jsonl`: held out."""
    out = root / MIX
    plan.save(out / PLAN)
    offsets = {level: line_offsets(root / level / TRAIN) for level in plan.levels}
    stats: dict[str, Any] = {"plan": asdict(plan), "rows": 0, "train": 0, "eval": 0, "original": 0, "missing": 0, "dropped": 0}
    levels = dict.fromkeys(plan.levels, 0)
    with contextlib.ExitStack() as stack:
        train = stack.enter_context((out / TRAIN).open("w", encoding="utf-8", newline="\n"))
        held = stack.enter_context((out / EVAL).open("w", encoding="utf-8", newline="\n"))
        files = {level: stack.enter_context((root / level / TRAIN).open("rb")) for level in plan.levels if offsets[level]}
        for row in iter_jsonl(root / SOURCE):
            idx, stats["rows"] = row.get("row_idx"), stats["rows"] + 1
            slot = plan.assign(idx)
            if slot == "eval":
                _dump(held, row)
                stats["eval"] += 1
                continue
            weight = row_weight(row, weights)
            if not weight:
                stats["dropped"] += 1
            elif slot == "original":
                _dump(train, {**row, "caveman": {"version": VERSION, "level": None, "weight": weight}})
                stats["original"] += 1
            elif idx in offsets[slot]:
                files[slot].seek(offsets[slot][idx])
                _dump(train, json.loads(files[slot].readline()))  # keeps the weight it got at shrink
                levels[slot] += 1
            else:
                stats["missing"] += 1  # not shrunk at its level yet: `shrink --planned --level <level>`
    stats["levels"] = levels
    stats["train"] = stats["original"] + sum(levels.values())
    (out / STATS).write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


# ---------------------------------------------------------------- eval (next-action proxy)


def eval_cases(rows: Iterable[dict], turns: int, seed: int, max_chars: int) -> Iterator[tuple[dict, int]]:
    """Seeded sample of `turns` parseable assistant turns per trace whose prompt fits `max_chars`."""
    for row in rows:
        conversation = row.get("conversations") or []
        sizes = [len(m.get("content") or "") for m in conversation]
        fit = [
            i for i, m in enumerate(conversation)
            if m.get("role") == "assistant" and sum(sizes[:i]) <= max_chars and split_turn(m.get("content") or "")
        ]
        rng = random.Random(f"{seed}:eval:{row.get('row_idx')}")
        for index in sorted(rng.sample(fit, min(turns, len(fit)))):
            yield row, index


def score_reply(reply: str, original: Turn, count: Count) -> dict:
    """Command similarity, exact match, task_complete agreement and reasoning tokens of one predicted turn."""
    turn = split_turn(reply)
    if turn is None:
        return {"parsed": False, "similarity": 0.0, "exact": False, "complete": False, "prose_tokens": count(reply)}
    score = similarity(keystrokes(turn.body), keystrokes(original.body))
    return {
        "parsed": True,
        "similarity": score,
        "exact": score == 1.0,
        "complete": bool(turn.body.get("task_complete")) == bool(original.body.get("task_complete")),
        "prose_tokens": count(prose_text(turn)),
    }


def summarize_eval(records: Iterable[dict]) -> dict:
    """Per budget tag: parse rate, command similarity, exact match, reasoning tokens."""
    tags: dict[str, list[dict]] = {}
    for r in records:
        if "error" not in r:
            tags.setdefault(r["tag"], []).append(r)
    mean = lambda values: round(sum(values) / len(values), 3) if values else 0.0  # noqa: E731
    out = {}
    for tag, rs in tags.items():
        out[tag] = {
            "cases": len(rs),
            "parsed": mean([r["parsed"] for r in rs]),
            "similarity": mean([r["similarity"] for r in rs]),
            "similarity_p50": _pct([r["similarity"] for r in rs], 0.5),
            "exact": mean([r["exact"] for r in rs]),
            "complete": mean([r["complete"] for r in rs]),
            "prose_tokens": mean([r["prose_tokens"] for r in rs]),
            "prose_tokens_p50": _pct([r["prose_tokens"] for r in rs], 0.5),
            "teacher_prose_tokens": mean([r["teacher_prose_tokens"] for r in rs]),
        }
    return out


async def run_eval(
    source: Path,
    out: Path,
    ask: Ask,
    count: Count,
    *,
    tags: Iterable[str] = ("none",),
    limit: int = 20,
    turns: int = 3,
    seed: int = 0,
    workers: int = 4,
    max_chars: int = 48000,
) -> dict:
    """Held-out next action per budget tag (teacher history as context); resumes past cases in `out`."""
    done = {(r["row_idx"], r["turn"], r["tag"]) for r in iter_jsonl(out) if "error" not in r}
    gate = asyncio.Semaphore(max(1, workers))
    rows = itertools.islice(iter_jsonl(source), limit or None)

    async def one(row: dict, index: int, tag: str) -> dict:
        conversation = [{"role": m["role"], "content": m.get("content") or ""} for m in row["conversations"][:index]]
        original = split_turn(row["conversations"][index]["content"])
        record = {"row_idx": row.get("row_idx"), "turn": index, "tag": tag, "teacher_prose_tokens": count(prose_text(original))}
        async with gate:
            started = time.perf_counter()
            try:
                reply = await ask(conversation if tag == "none" else tag_budget(conversation, tag))
            except Exception as error:  # an endpoint failure is retried on the next run
                return {**record, "error": repr(error)}
            return {**record, **score_reply(reply, original, count), "secs": round(time.perf_counter() - started, 2)}

    jobs = [
        one(row, index, tag)
        for row, index in eval_cases(rows, turns, seed, max_chars)
        for tag in tags
        if (row.get("row_idx"), index, tag) not in done
    ]
    log.info("eval: %d cases to run (%d done) -> %s", len(jobs), len(done), out)
    for number, job in enumerate(asyncio.as_completed(jobs), 1):  # each case written as it lands: a crash loses none
        record = await job
        write_jsonl(out, [record], mode="a")
        if number % max(1, len(jobs) // 20) == 0:
            log.info("eval: %d/%d", number, len(jobs))
    stats = summarize_eval(iter_jsonl(out))
    out.with_suffix(".json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


def _progress(rows: Iterable[dict], total: int) -> Iterator[dict]:
    every = max(10, total // 20)
    for number, item in enumerate(rows, 1):
        yield item
        if number % every == 0:
            log.info("fetched %d/%d", number, total)


def cmd_fetch(args: argparse.Namespace) -> int:
    out = Path(args.out)
    have = row_ids(out / SOURCE)
    if args.parquet:
        shards = [download(f["url"], out / SHARDS / f["filename"], f.get("size") or 0) for f in parquet_files(args.dataset)]
        total = parquet_total(shards)
        offsets = sample_offsets(total, args.limit, args.seed) if args.limit else range(total)
        new = [o for o in offsets if o not in have]
        rows = parquet_rows(shards, new)
    elif args.limit:
        total = args.total or dataset_size(args.dataset)
        new = [o for o in sample_offsets(total, args.limit, args.seed) if o not in have]
        rows = fetch_rows(new, args.dataset, pace=args.pace)
    else:
        log.error("fetch: --limit 0 (every row) needs --parquet")
        return 2
    log.info("fetch: %s has %d rows; sample %s (seed %d), %d new", args.dataset, total, args.limit or "all", args.seed, len(new))
    before = len(have)
    write_jsonl(out / SOURCE, _progress(rows, len(new)), mode="a")
    log.info("fetch: %d -> %d rows in %s", before, len(row_ids(out / SOURCE)), out / SOURCE)
    return 0


def make_verifier(args: argparse.Namespace) -> Ask:
    """Verifier on its own host (`--verifier-url` + key, as a set) or on the compressor's."""
    url, key = (args.verifier_url, args.verifier_key) if args.verifier_url else (args.url, args.key)
    # commands past ~CONTEXT chars are never scored: cap the verifier's answer
    return make_ask(url, key, args.verifier or args.model, args.effort, args.ctx, args.timeout, 1024)


def cmd_shrink(args: argparse.Namespace) -> int:
    root = Path(args.out)
    plan = None
    if args.planned:
        if not (root / MIX / PLAN).exists():
            log.error("shrink --planned: no %s; run `mix` first", root / MIX / PLAN)
            return 2
        plan = Plan.load(root / MIX / PLAN)
    ask = make_ask(args.url, args.key, args.model, args.effort, args.ctx, args.timeout)
    stats = asyncio.run(
        run_shrink(
            root / args.level, ask, token_counter(), source=root / SOURCE, limit=args.limit, workers=args.workers,
            model=args.model, level=args.level, verifier=make_verifier(args) if args.replay else None, tag=args.tag,
            weights=args.weights, traces=args.traces, plan=plan,
        )
    )
    print(json.dumps(stats, indent=2))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    root = Path(args.out)
    name = args.verifier or args.model
    stats = asyncio.run(
        run_replay(root / args.level, root / SOURCE, make_verifier(args), name=name, limit=args.limit, workers=args.workers)
    )
    print(json.dumps(stats, indent=2))
    return 0


def cmd_mix(args: argparse.Namespace) -> int:
    plan = Plan(tuple(args.levels), args.original, args.holdout, args.seed)
    print(json.dumps(run_mix(Path(args.out), plan, args.weights), indent=2))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    root = Path(args.out)
    ask = make_ask(args.url, args.key, args.model, args.effort, 0, args.timeout, args.max_tokens, as_json=False)
    name = re.sub(r"[^\w.-]+", "_", args.name or args.model)
    stats = asyncio.run(
        run_eval(
            Path(args.source) if args.source else root / MIX / EVAL, root / "eval" / f"{name}.jsonl", ask, token_counter(),
            tags=args.tags, limit=args.limit, turns=args.turns, seed=args.seed, workers=args.workers, max_chars=args.max_chars,
        )
    )
    print(json.dumps(stats, indent=2))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    out = Path(args.out) / args.level
    stats = summarize(iter_jsonl(out / TURNS))
    (out / STATS).write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(json.dumps(stats, indent=2))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(OUT_DIR), help="data directory (source.jsonl; one subdir per level)")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="sample source traces into source.jsonl")
    fetch.add_argument("--limit", type=int, default=50, help="traces to sample (0 = every row, needs --parquet)")
    fetch.add_argument("--seed", type=int, default=0)
    fetch.add_argument("--dataset", default=DATASET)
    fetch.add_argument("--parquet", action="store_true", help="read downloaded parquet shards, not the viewer")
    fetch.add_argument("--total", type=int, default=0, help="dataset rows (0 = ask the viewer)")
    fetch.add_argument("--pace", type=float, default=0.5, help="seconds between viewer requests")
    fetch.set_defaults(run=cmd_fetch)

    own = bool(env("CAVEMAN_URL"))  # CAVEMAN_URL brings its own key and model: never mix in AGENTIC_INFERENCE_*

    def models(command: argparse.ArgumentParser) -> None:  # compressor + verifier hosts, shared by shrink and replay
        command.add_argument("--url", default=env("CAVEMAN_URL") or env("AGENTIC_INFERENCE_URL") or "")
        command.add_argument("--key", default=env("CAVEMAN_KEY") or ("" if own else env("AGENTIC_INFERENCE_KEY")) or "")
        command.add_argument("--model", default=env("CAVEMAN_LLM") or ("" if own else env("AGENTIC_INFERENCE_LLM")) or "")
        command.add_argument("--verifier", default=env("CAVEMAN_VERIFIER") or "", help="replay model (empty = --model)")
        command.add_argument("--verifier-url", default=env("CAVEMAN_VERIFIER_URL") or "", help="replay host (empty = --url)")
        command.add_argument("--verifier-key", default=env("CAVEMAN_VERIFIER_KEY") or "", help="key of --verifier-url only")
        command.add_argument("--effort", default="none", help='reasoning_effort sent to the model ("" = omit)')
        command.add_argument("--ctx", type=int, default=0, help="Ollama num_ctx (0 = omit; hosted APIs reject it)")
        command.add_argument("--timeout", type=float, default=300.0)

    shrink = sub.add_parser("shrink", help="rewrite reasoning into <level>/train.jsonl + turns.jsonl")
    shrink.add_argument("--level", choices=LEVELS, default="full", help="target compression level")
    shrink.add_argument("--limit", type=int, default=0, help="traces this run (0 = all pending)")
    shrink.add_argument("--workers", type=int, default=2, help="concurrent model calls per trace")
    shrink.add_argument("--traces", type=int, default=1, help="traces shrunk at once (calls in flight: traces x workers)")
    shrink.add_argument("--planned", action="store_true", help="only rows mix/plan.json assigns to --level")
    shrink.add_argument("--replay", action=argparse.BooleanOptionalAction, default=True, help="verifier replay gate")
    shrink.add_argument("--tag", action=argparse.BooleanOptionalAction, default=True, help="budget tag system prompt")
    shrink.add_argument("--weight", action="append", default=[], metavar="FIELD=VALUE:W", help="row weight rule; 0 drops")
    models(shrink)
    shrink.set_defaults(run=cmd_shrink)

    replay = sub.add_parser("replay", help="re-check a level's shrunk turns with another (student-sized) verifier")
    replay.add_argument("--level", choices=LEVELS, default="full")
    replay.add_argument("--limit", type=int, default=50, help="traces to re-check (0 = all)")
    replay.add_argument("--workers", type=int, default=4, help="concurrent verifier calls")
    models(replay)
    replay.set_defaults(run=cmd_replay)

    mix = sub.add_parser("mix", help="one SFT set: per-trace level, untagged originals, held-out eval split")
    mix.add_argument("--levels", nargs="+", choices=LEVELS, default=list(LEVELS))
    mix.add_argument("--original", type=float, default=0.1, help="share of traces kept original and untagged")
    mix.add_argument("--holdout", type=float, default=0.05, help="share of traces held out for eval")
    mix.add_argument("--seed", type=int, default=0)
    mix.add_argument("--weight", action="append", default=[], metavar="FIELD=VALUE:W", help="weight of original rows")
    mix.set_defaults(run=cmd_mix)

    evaluate = sub.add_parser("eval", help="next-action proxy on held-out traces: similarity vs reasoning tokens")
    evaluate.add_argument("--url", default=env("CAVEMAN_EVAL_URL") or "", help="student endpoint (OpenAI-compatible)")
    evaluate.add_argument("--key", default=env("CAVEMAN_EVAL_KEY") or "")
    evaluate.add_argument("--model", default=env("CAVEMAN_EVAL_LLM") or "")
    evaluate.add_argument("--name", default="", help="result name under eval/ (empty = --model)")
    evaluate.add_argument("--tags", nargs="+", choices=("none", *LEVELS), default=["none", *LEVELS])
    evaluate.add_argument("--source", default="", help="held-out traces (empty = mix/eval.jsonl)")
    evaluate.add_argument("--limit", type=int, default=20, help="held-out traces (0 = all)")
    evaluate.add_argument("--turns", type=int, default=3, help="sampled turns per trace")
    evaluate.add_argument("--seed", type=int, default=0)
    evaluate.add_argument("--workers", type=int, default=4)
    evaluate.add_argument("--max-chars", type=int, default=48000, help="longest prompt (conversation chars)")
    evaluate.add_argument("--max-tokens", type=int, default=4096)
    evaluate.add_argument("--effort", default="", help='reasoning_effort ("" = omit)')
    evaluate.add_argument("--timeout", type=float, default=600.0)
    evaluate.set_defaults(run=cmd_eval)

    stats = sub.add_parser("stats", help="summarize <level>/turns.jsonl into stats.json")
    stats.add_argument("--level", choices=LEVELS, default="full")
    stats.set_defaults(run=cmd_stats)
    args = parser.parse_args(argv)
    try:
        args.weights = parse_weights(getattr(args, "weight", []))
    except ValueError as error:
        parser.error(str(error))
    if args.command == "shrink" and not (args.url and args.model):
        parser.error("no compressor: set AGENTIC_INFERENCE_URL + _LLM (.env), CAVEMAN_URL + CAVEMAN_LLM, or --url + --model")
    if args.command in ("shrink", "replay") and args.verifier_url and not args.verifier:
        parser.error("--verifier-url (CAVEMAN_VERIFIER_URL) needs its own model: --verifier (CAVEMAN_VERIFIER)")
    if args.command == "replay" and not ((args.verifier_url or args.url) and (args.verifier or args.model)):
        parser.error("no verifier: set --verifier-url + --verifier, or a compressor host (see shrink)")
    if args.command == "eval" and not (args.url and args.model):
        parser.error("no student: set --url + --model (CAVEMAN_EVAL_URL + CAVEMAN_EVAL_LLM)")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for name in ("httpx", "httpx2"):  # one line per model call drowns the trace log
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass
    args = parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
