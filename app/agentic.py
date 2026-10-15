"""An application agnostic agent harness.

The module is organized as a set of small, compartmentalized subsystems that can
each be replaced by a consumer:

| Subsystem | Entry points                                     |
| --------- | ------------------------------------------------ |
| config    | `AgentConfig`                                    |
| events    | `Event`, `EventBus`, `EventType`                 |
| prompt    | `PromptRenderer`                                 |
| storage   | `Store`, `SqliteStore`, `Cache`, `LruCache`      |
| sessions  | `Session`, `SessionManager`                      |
| memory    | `Turn`, `Transcript`, `ConversationMemory`,      |
|           | `Rule`, `RuleBook`                               |
| repository| `RepoSpec`, `RepoManager`, `Checkout`            |
| resilience| `ErrorKind`, `RetryPolicy`, `Usage`              |
| models    | `ModelSpec`, `ModelPool`                         |
| tools     | `Workspace`, `ToolRegistry`, `Sandbox`           |
| console   | `ConsoleRenderer`                                |
| commands  | `CommandRegistry`                                |
| core      | `Engine`, `Agent`, `RunResult`                   |
| repl      | `Repl`                                           |
| web       | `Hub`, `WebServer`                               |
| cli       | `main`                                           |

In the simplest case a consumer owns a prompt template and does no more than::

    import asyncio, agentic
    asyncio.run(agentic.Agent().run(MY_TEMPLATE))

An application that already has its own sessions, history and storage embeds
the loop on its own instead, with none of the batteries above::

    engine = agentic.Engine(inference_key=..., inference_llm=..., env=False)
    result = await engine.run(my_messages, tools=my_tools)

The module also works as a data driven application. With no arguments it opens
an interactive terminal; ``--input`` performs one run and ``--serve`` starts the
web layer::

    python agentic.py                  # chat in the terminal
    python agentic.py --input notes.md # one run, streamed to the console
    python agentic.py --serve          # REST API, websocket hub and chat UI

Configuration is read from ``agentic.json``, ``agentic.yaml`` or ``settings.yml`` — the
current directory first, then next to this file — before the ``AGENTIC_*``
environment, which a ``.env`` file found the same way adds to; see
`find_config_file`, `find_env_file` and `main`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import contextlib
import contextvars
import copy
import functools
import glob
import inspect
import itertools
import json
import mimetypes
import os
import queue
import random
import re
import shutil
import signal
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Literal,
    MutableMapping,
    Protocol,
    TypedDict,
    runtime_checkable,
)

__all__ = [
    "Agent",
    "AgentConfig",
    "Block",
    "BubblewrapSandbox",
    "Cache",
    "Checkout",
    "CommandRegistry",
    "ConsoleRenderer",
    "ConversationMemory",
    "Engine",
    "ErrorKind",
    "Event",
    "EventBus",
    "EventType",
    "Hub",
    "LandlockSandbox",
    "LruCache",
    "ModelError",
    "ModelPool",
    "ModelSpec",
    "PromptRenderer",
    "RepoError",
    "RepoManager",
    "RepoSpec",
    "Repl",
    "RetryPolicy",
    "Rule",
    "RuleBook",
    "RunResult",
    "Sandbox",
    "SeatbeltSandbox",
    "Session",
    "SessionManager",
    "SqliteStore",
    "Store",
    "ToolCall",
    "ToolRegistry",
    "Transcript",
    "Turn",
    "Usage",
    "WebServer",
    "Workspace",
    "find_config_file",
    "find_env_file",
    "load_env_file",
    "main",
    "pick_sandbox",
    "read_config_file",
    "read_env_file",
]

ROOT = Path(__file__).resolve().parent
STEM = Path(__file__).stem

Handler = Callable[["Event"], Any]
#: Receives the output of a running command as it is produced; an empty string
#: is a heartbeat, sent while the command is alive but quiet.
Progress = Callable[[str], Awaitable[None]]
#: A `Progress` for any tool call of a run, keyed by the id of the call.
ToolReporter = Callable[[str | None, str], Awaitable[None]]
#: Puts a question to the person behind a run: ``(call_id, question, options)``
#: and returns their answer, empty when they gave none.
Asker = Callable[[str | None, str, list[str]], Awaitable[str]]
#: Runs one step in a fresh context: ``(call_id, task, inputs, step, tools)``
#: and returns the worker's answer, capped, or an ``Error: ...`` line.
Delegator = Callable[[str | None, str, list[str], int, list[str]], Awaitable[str]]
#: Runs one task over the parts of some inputs:
#: ``(call_id, task, inputs, chunk_tokens, step, tools) -> answers``.
Mapper = Callable[[str | None, str, list[str], int, int, list[str]], Awaitable[str]]

# ---------------------------------------------------------------------------
# Config
#
# Values are resolved in layers: dataclass defaults, then a JSON or YAML config
# file, then the environment (``AGENTIC_*``, plus a ``.env`` file loaded into it
# first), then whatever the consumer or the CLI passes in.
# ---------------------------------------------------------------------------

ENV_PREFIX = f"{STEM.upper()}_"

DEFAULT_INFERENCE_URL = "https://openrouter.ai/api/v1"
DEFAULT_INFERENCE_LLM = "deepseek/deepseek-v4-flash-0731"
DEFAULT_INSTRUCTIONS = "You are a helpful, autonomous assistant."
DEFAULT_VISION_INSTRUCTIONS = (
    "You describe images precisely and factually for another agent that cannot "
    "see them. Answer the question when one is given, otherwise describe the "
    "image: its subject, layout, text and anything notable."
)

DEFAULT_SUMMARY_INSTRUCTIONS = """\
You keep the working memory of an AI agent whose conversation has outgrown its \
context window. You are given the current summary (possibly empty) and the next \
segment of the conversation, which is about to leave the agent's context. \
Rewrite the summary so it also covers the segment. The agent continues from \
your summary plus the newest turns, which it still sees verbatim, so keep \
everything it needs to carry on without asking again and drop what it no \
longer needs.

Write markdown with these sections, leaving out a section only when empty:
## Goal: what the user wants overall; constraints and preferences they stated
## User messages: every user request in order, condensed but faithful; quote short instructions verbatim
## Decisions and facts: conclusions, values, names, URLs, ids, commands that worked
## Files and artifacts: paths created, read or changed, and what matters about each
## Errors and fixes: failures met and how they were resolved, or that they are not
## Progress: what is done
## Open work: what was in progress at the end of the segment and the exact next step

Be dense: fragments over sentences, specifics (paths, numbers, identifiers) \
over generalities, no preamble and no remarks about the summary itself. Keep \
earlier content unless the segment supersedes it."""

DEFAULT_RULES_INSTRUCTIONS = (
    "You turn a user's feedback on an agent's answer into one durable rule for "
    "that agent. You are given the rules it already follows, the trace that led "
    "to the answer and whether the user liked it. Work out what the agent did "
    "well (liked) or badly (disliked) that is likely to matter again, and state "
    "it as one short, general, imperative rule. When an existing rule covers the "
    "same behaviour, edit it instead of adding one: merge, sharpen or correct "
    "it. Answer with JSON only: "
    '{"action": "add" | "edit", "id": "<rule id, edit only>", '
    '"polarity": "do" | "avoid", "rule": "<one sentence>"}'
)

DEFAULT_WORKER_INSTRUCTIONS = (
    "You are a worker an AI agent handed one step of a larger task. You start "
    "with a fresh context: the task below is all you are told. Do that step and "
    "nothing more, with the tools you have; read long files a page at a time. "
    "Files you write are the deliverable. Then answer with what the agent needs "
    "to carry on: the result, what you wrote where, and anything that failed. "
    "Only your answer reaches the agent, so make it dense and complete."
)

#: Seconds an idle session lives when neither the layers nor the mode say.
DEFAULT_SESSION_TTL = 3600

#: Tool flags each front end settles when the layers left them unset: a
#: terminal user is on their own machine and at the keyboard; a web client may
#: be anyone who reaches the port, though the page answers questions too.
MODE_TOOLS: dict[str, dict[str, Any]] = {
    "repl": {"outside_read": True, "outside_write": True, "ask_enabled": True},
    "web": {"outside_read": False, "outside_write": False, "ask_enabled": True},
}

#: Durable state each front end settles when it builds its agent (`Agent(mode=)`),
#: so an interactive session outlives a crash; a one shot run keeps nothing.
MODE_STATE: dict[str, dict[str, Any]] = {
    "repl": {"state_dir": Path(f".{STEM}"), "session_durable": True, "session_ttl": 86400},
    "web": {"state_dir": Path(f".{STEM}"), "session_durable": True, "session_ttl": 86400},
}

#: Model roles the harness knows about. `leader` drives the agentic loop; the
#: others are specialists delegated to from tools or from the harness itself.
LEADER_ROLE = "leader"
VISION_ROLE = "vision"
SUMMARY_ROLE = "summary"
WORKER_ROLE = "worker"

#: Config field naming each role's model, where it is not ``<role>_model``.
ROLE_MODEL_FIELDS = {VISION_ROLE: "inference_vlm"}

SECRET_FIELDS = ("api_key", "key", "token", "secret", "password")


def is_secret_key(name: str) -> bool:
    """True when a config field name denotes a credential (``*_api_key``, ...)."""
    lowered = name.lower()
    return any(lowered == key or lowered.endswith(f"_{key}") for key in SECRET_FIELDS)


def mask_secret(value: str) -> str:
    """Mask a secret so only its head and tail remain visible."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:8]}...{value[-4:]}"


def _field_kind(annotation: str) -> Any:
    """Pick the runtime type an environment string should be coerced into."""
    for name, kind in (("bool", bool), ("int", int), ("float", float), ("Path", Path)):
        if name in annotation:
            return kind
    return str


def _coerce(value: str, kind: Any) -> Any:
    """Coerce an environment string into the type of a config field."""
    if kind is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        return int(value)
    if kind is float:
        return float(value)
    if kind is Path:
        return Path(value).expanduser()
    return value


def _coerce_value(value: Any, kind: Any) -> Any:
    """Coerce a value parsed out of a config file into a config field type."""
    if isinstance(value, str):
        return _coerce(value, kind)
    if kind is Path and isinstance(value, Path):
        return value.expanduser()
    if kind is bool and isinstance(value, (int, float)):
        return bool(value)
    if kind is int and isinstance(value, float) and value.is_integer():
        return int(value)
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


# Config files. A file named after this module (``agentic.json``, ``agentic.yaml``
# or ``settings.yml``) is the layer between the dataclass defaults and the
# environment. The current directory wins over the directory this module lives
# in, so a consumer overrides the defaults shipped beside the harness.

CONFIG_SUFFIXES = (".json", ".yaml", ".yml")


def config_directories(directories: Iterable[Path | str] | None = None) -> list[Path]:
    """The directories a config file is looked for in, nearest first."""
    raw = [Path.cwd(), ROOT] if directories is None else [Path(d) for d in directories]
    seen: set[str] = set()
    out: list[Path] = []
    for directory in raw:
        resolved = Path(directory).expanduser()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    return out


def config_candidates(
    stem: str = "", directories: Iterable[Path | str] | None = None
) -> list[Path]:
    """Every config file path that is looked at, in order of precedence."""
    stem = stem or STEM
    return [
        directory / f"{stem}{suffix}"
        for directory in config_directories(directories)
        for suffix in CONFIG_SUFFIXES
    ]


def find_config_file(
    stem: str = "", directories: Iterable[Path | str] | None = None
) -> Path | None:
    """The first config file that exists, or ``None`` when there is none."""
    for path in config_candidates(stem, directories):
        if path.is_file():
            return path
    return None


def read_config_file(path: Path | str) -> dict[str, Any]:
    """Read a JSON or YAML config file into a mapping.

    Parsed with `omegaconf` when it is installed, which also resolves
    interpolations such as ``inference_key: ${oc.env:AGENTIC_INFERENCE_KEY}`` and
    ``inference_llm: ${defaults.llm}``. Without it JSON still works and YAML falls
    back to `pyyaml`.
    """
    file = Path(path).expanduser()
    text = file.read_text(encoding="utf-8")
    data = json.loads(text) if file.suffix.lower() == ".json" else _parse_yaml(text)
    data = _resolve_interpolations(data)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{file} must contain a mapping of settings")
    return data


def _parse_yaml(text: str) -> Any:
    """Parse YAML with whichever of omegaconf or pyyaml is installed."""
    with contextlib.suppress(ImportError):
        from omegaconf import OmegaConf

        return OmegaConf.create(text)
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "reading a YAML config file needs the 'omegaconf' (or 'pyyaml') package"
        ) from exc
    return yaml.safe_load(text)


def _resolve_interpolations(data: Any) -> Any:
    """Resolve ``${...}`` references and return plain Python containers."""
    with contextlib.suppress(ImportError):
        from omegaconf import OmegaConf

        if not OmegaConf.is_config(data):
            if not isinstance(data, (dict, list)):
                return data
            data = OmegaConf.create(data)
        return OmegaConf.to_container(data, resolve=True)
    return data


def flatten_config(data: dict[str, Any], known: Iterable[str]) -> dict[str, Any]:
    """Flatten the nested groups a config file may use onto field names.

    ``inference: {url: u}`` becomes ``inference_url``, but only when every key of
    the group names a real field; anything else is left alone and ends up in
    `AgentConfig.extras`. A nested ``extras`` mapping is folded in as is.
    """
    fields_ = set(known)
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "extras":
            if isinstance(value, dict):
                out.update(value)
            continue
        if (
            isinstance(value, dict)
            and value
            and all(f"{key}_{sub}" in fields_ for sub in value)
        ):
            out.update({f"{key}_{sub}": item for sub, item in value.items()})
            continue
        out[key] = value
    return out


# Env files. A ``.env`` file, looked for in the same directories as the config
# file, is loaded into the process environment before the config file is read,
# so both ``${oc.env:VAR}`` interpolations and the ``AGENTIC_*`` layer see it.
# The file wins over variables already set, so editing it always takes effect.

ENV_FILE = ".env"


def env_candidates(directories: Iterable[Path | str] | None = None) -> list[Path]:
    """Every ``.env`` path that is looked at, in order of precedence."""
    return [directory / ENV_FILE for directory in config_directories(directories)]


def find_env_file(directories: Iterable[Path | str] | None = None) -> Path | None:
    """The first ``.env`` file that exists, or ``None`` when there is none."""
    for path in env_candidates(directories):
        if path.is_file():
            return path
    return None


def read_env_file(path: Path | str) -> dict[str, str]:
    """Read a ``.env`` file into a mapping without touching the environment.

    Parsed with `python-dotenv`, so quoting, ``export`` prefixes, comments and
    ``${VAR}`` expansion behave as usual. A key without a value is dropped.
    """
    try:
        from dotenv import dotenv_values
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "reading a .env file needs the 'python-dotenv' package"
        ) from exc
    values = dotenv_values(Path(path).expanduser(), encoding="utf-8")
    return {key: value for key, value in values.items() if value is not None}


def load_env_file(
    path: Path | str,
    environ: MutableMapping[str, str] | None = None,
    *,
    override: bool = False,
) -> dict[str, str]:
    """Apply a ``.env`` file to `environ` (``os.environ`` by default).

    Variables already set are kept unless `override` is true. Returns the
    variables that were applied.
    """
    environ = os.environ if environ is None else environ
    applied = {
        key: value
        for key, value in read_env_file(path).items()
        if override or key not in environ
    }
    environ.update(applied)
    return applied


def _locate_file(
    value: bool | str | Path | None, find: Callable[[], Path | None], label: str
) -> Path | None:
    """Resolve a file argument: ``True`` searches, ``False`` / ``None`` skips.

    A path is taken as given, and one that does not exist is an error, because
    the caller asked for that file by name.
    """
    if value is None or value is False:
        return None
    if value is True:
        return find()
    file = Path(value).expanduser()
    if not file.is_file():
        raise FileNotFoundError(f"no such {label}: {file}")
    return file


@dataclass(frozen=True)
class ModelSpec:
    """An addressable model: which model, on which endpoint, with which key.

    Roles (`leader`, `vision`, ...) resolve to one of these, and a `ModelPool`
    reuses a single client per endpoint so several roles cost one connection.
    """

    role: str
    name: str
    api_url: str
    api_key: str = ""
    #: Context size asked of the server per request (Ollama `num_ctx`); 0 looks it up.
    num_ctx: int = 0

    @property
    def endpoint(self) -> tuple[str, str]:
        return (self.api_url, self.api_key)


@dataclass
class AgentConfig:
    """Everything an `Agent` needs to run. Consumers may subclass or extend it.

    Application specific values live in `extras` and are reachable from prompt
    templates as ``config.extras.<name>`` or ``config.<name>``.
    """

    name: str = "agent"
    instructions: str = DEFAULT_INSTRUCTIONS
    inference_url: str = DEFAULT_INFERENCE_URL
    inference_key: str = ""
    inference_llm: str = DEFAULT_INFERENCE_LLM
    # Context size sent with every model request as `options.num_ctx` (Ollama's
    # name for it). 0 looks up the length the model was trained for (Ollama
    # `/api/show`) and sends nothing when none is found.
    inference_ctx: int = 0
    max_turns: int = 100
    # Resilience of model calls. Every request to a provider is bounded by a
    # timeout and tried again on a transient failure (a timeout, a rate limit, a
    # connection error or a 5xx) with exponential backoff and jitter. The
    # leader's stream is bound by `stream_timeout` instead, a stall guard — the
    # longest the stream may say nothing, extended by `shell_timeout` while a
    # tool call is outstanding and restarted by every heartbeat of that call —
    # and is only restarted while it has produced nothing. It is longer than a
    # request because some providers (Ollama) send a tool call whole once it is
    # complete, so a model writing a long file is silent until it is done.
    request_timeout: float = 120.0
    stream_timeout: float = 600.0
    # While the leader's stream is silent and no tool call is open, a heartbeat
    # (`model.wait`, with the seconds waited so far) is published every this
    # many seconds, so a client can show a model that is still writing as alive.
    # 0 disables it.
    stream_heartbeat: float = 5.0
    retry_attempts: int = 3
    retry_backoff: float = 0.5
    retry_max_backoff: float = 30.0
    retry_jitter: float = 0.5
    # What a million input / output tokens cost, in whatever currency the
    # provider bills. Left at zero, `agent.end` reports tokens without a price.
    cost_input: float = 0.0
    cost_output: float = 0.0
    # Vision specialist. When `inference_vlm` is set the leader model never sees
    # image data: image tools delegate to this model and return its answer as
    # text, so a leader without vision can still work with images. The endpoint
    # and credential fall back to the leader's when left empty.
    inference_vlm: str = ""
    vision_api_url: str = ""
    vision_api_key: str = ""
    vision_instructions: str = DEFAULT_VISION_INSTRUCTIONS
    vision_max_tokens: int = 1024
    # Conversation memory. Every run journals the exchange to the transcript of
    # its session, which keeps every turn; the model is shown a window onto it
    # (see the context budget below).
    memory_enabled: bool = True
    # Context budget, in tokens of the leader's context window. The model sees
    # the newest items verbatim and a summary of everything older. Past
    # `context_trigger` of the usable budget (window minus `context_reserve`,
    # the room left for the reply and its reasoning) the context is compacted
    # down to `context_target`: old reasoning and old tool outputs are cleared
    # first (the newest `context_keep` outputs never), then the oldest prefix is
    # folded into the summary by the `summary` role (which falls back to the
    # leader), or dropped when `context_summary` is off. `context_window` 0 asks
    # the provider; `context_trigger` 0 turns compaction off.
    context_window: int = 0
    context_reserve: int = 0
    context_trigger: float = 0.85
    context_target: float = 0.5
    context_keep: int = 4
    context_summary: bool = True
    # tiktoken encoding the context is counted with; empty estimates from the
    # byte size. Either way the count is calibrated on the provider's usage.
    context_tokenizer: str = "o200k_base"
    # Work slicing. With `plan_enabled` the leader gets the `plan` tool and every
    # model call ends with a budget note: the context in use, the slice one step
    # may read and the open plan. A slice is `slice_share` of the usable budget;
    # once the leader's read speed is measured the budget is also capped to what
    # it reads from scratch within `slice_stall` of `stream_timeout`, so a slow
    # (local) model is compacted before a cold read could stall. 0 turns the
    # time cap off.
    plan_enabled: bool = True
    # Context folding. With `fold_enabled` (and compaction on) the leader and
    # its workers get `branch` and `return`: the steps between the two are
    # folded out of that context, and the summary written in `return` stands
    # for them.
    fold_enabled: bool = True
    slice_share: float = 0.25
    slice_stall: float = 0.5
    # Sizing question. A plan whose estimate passes one of these (slices to
    # read, minutes at the measured read speed, cost at `cost_input`) asks the
    # user once before it is kept, along with the model's own question about
    # the split when it has one. 0 turns a threshold off; nobody is asked
    # unless `ask_enabled`.
    plan_ask_slices: int = 8
    plan_ask_minutes: float = 10.0
    plan_ask_cost: float = 1.0
    # Workers. With `delegate_enabled` the leader gets the `delegate` tool: one
    # step runs in a fresh context on the `worker` role (falling back to the
    # leader) with `worker_instructions`, the workspace tools and its own
    # `branch` / `return` (no plan, no question, no delegate) and at most
    # `worker_max_turns` turns, and only its answer comes back, clipped to
    # `worker_max_tokens`.
    delegate_enabled: bool = True
    worker_model: str = ""
    worker_api_url: str = ""
    worker_api_key: str = ""
    worker_instructions: str = DEFAULT_WORKER_INSTRUCTIONS
    worker_max_turns: int = 30
    worker_max_tokens: int = 1000
    # `map` (built with `delegate`) cuts its inputs into parts a worker may read
    # and runs one worker per part, at most `map_parallel` at once: a local
    # server answers one request at a time anyway; raise it for a hosted model.
    map_parallel: int = 1
    summary_model: str = ""
    summary_api_url: str = ""
    summary_api_key: str = ""
    summary_instructions: str = DEFAULT_SUMMARY_INSTRUCTIONS
    # The size a summary may grow to, in tokens.
    summary_max_tokens: int = 4096
    # Replay the tool calls of earlier runs (call and clipped output) to the
    # model, not only the words exchanged; the journal keeps them either way.
    memory_tools: bool = True
    # Learned rules. A thumb on an answer (or `/good`, `/bad`) asks the
    # `summary` role to distil a do / avoid rule from the trace behind it, which
    # is appended to the instructions of every later run. A warning is shown
    # once the rules exceed `rules_max_chars` (0 disables it).
    rules_enabled: bool = True
    rules_max_chars: int = 4000
    rules_instructions: str = DEFAULT_RULES_INSTRUCTIONS
    rules_max_tokens: int = 400
    # Input as given on the command line (a file path or raw text) and the raw
    # text it resolves to. Templates read `config.input`.
    input_source: str = ""
    input: str = ""
    # Prompt template used by the data driven application: a file path or the
    # template itself. Left empty, `load_template` looks for `agentic_prompt.md`.
    template: str = ""
    # Durable state. When set, `db_path` and `workspace_root` default to
    # ``<state_dir>/agentic.db`` and ``<state_dir>/workspaces``. ``None`` leaves
    # it to the mode: the repl and the web server keep ``.agentic`` in the
    # current directory, a one shot run and an embedded engine keep nothing.
    state_dir: Path | None = None
    # Workspaces / sessions
    workspace_root: Path | None = None
    # Directory copied into every new session workspace (application content).
    workspace_seed: Path | None = None
    # Seconds an idle session lives. ``None`` leaves it to the mode: a day for
    # the repl and the web server, `DEFAULT_SESSION_TTL` otherwise.
    session_ttl: int | None = None
    # How often (in seconds) live sessions are swept for expiry and the
    # workspace root for directories without an owner. ``0`` disables the timer
    # and leaves expiry to be noticed on access.
    session_sweep_interval: int = 60
    # Durable sessions: workspaces and store records survive a shutdown and are
    # rehydrated on the next start instead of being wiped with the process.
    # ``None`` leaves it to the mode: the repl and the web server are durable.
    session_durable: bool | None = None
    keep_workspace: bool = False
    # Default git repository. When `repo_url` is set every new session clones it
    # into the workspace and checks out a session branch, and the agent can
    # commit, push and open a pull request from that checkout.
    repo_url: str = ""
    repo_branch: str = ""
    repo_token: str = ""
    repo_remote: str = "origin"
    repo_branch_prefix: str = "agent"
    repo_dir: str = ""
    repo_author_name: str = ""
    repo_author_email: str = ""
    repo_depth: int = 1
    repo_clone: bool = True
    repo_timeout: int = 300
    # Storage
    db_path: Path | None = None
    cache_size: int = 256
    # Tools
    shell_timeout: int = 900
    # While a command runs, a heartbeat is reported every this many seconds
    # (`tool.progress`); it keeps the stall guard of the leader from failing a
    # slow but live command. 0 disables it.
    shell_heartbeat: float = 5.0
    # A command's output is clipped to this many characters (head and tail
    # kept) before the model sees it; 0 disables the cap.
    shell_output_limit: int = 32_000
    # Characters one `read_text_file` call returns; a longer file comes back a
    # page of whole lines at a time, ending with the offset of the next page.
    # 0 sizes the page to a quarter of the usable context budget, -1 returns
    # files whole.
    read_output_limit: int = 0
    shell_enabled: bool = True
    # Reach beyond the session workspace. `outside_read` lets the read tools
    # (list, read, image) take outside paths, `outside_write` the write tools
    # (write, patch). The shell is held to the same pair: unless both are set
    # it runs inside an OS sandbox (`shell_sandbox`), and where none is
    # available it is withheld. ``None`` leaves each to the mode: the repl
    # allows both, the web server, a one shot run and an embedded engine
    # neither.
    outside_read: bool | None = None
    outside_write: bool | None = None
    # Which sandbox confines the shell: ``auto`` (Landlock, else bubblewrap,
    # on Linux; Seatbelt on macOS), one of those by name (``landlock``,
    # ``bwrap``, ``seatbelt``), or ``none`` to withhold a confined shell.
    shell_sandbox: str = "auto"
    # Give the leader the `ask_user` tool, which puts a question to the person
    # and waits for the answer. ``None`` leaves it to the mode: the repl and the
    # web server have someone to answer, an embedded engine and a one shot run
    # do not, and a question nobody can answer would hang the run.
    ask_enabled: bool | None = None
    # Web
    host: str = "127.0.0.1"
    port: int = 8765
    theme: Path | None = None
    # Console
    color: bool | None = None
    quiet: bool = False
    # Free form application values
    extras: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute lookup fails, which keeps template
        # access to application values (`config.foo`) working.
        if name.startswith("_"):
            raise AttributeError(name)
        extras = self.__dict__.get("extras") or {}
        if name in extras:
            return extras[name]
        raise AttributeError(name)

    def merge(self, **overrides: Any) -> "AgentConfig":
        """Return a copy with non-``None`` overrides applied."""
        known = {f.name for f in fields(self)}
        values = {k: v for k, v in overrides.items() if k in known and v is not None}
        extras = dict(self.extras)
        extras.update(
            {k: v for k, v in overrides.items() if k not in known and v is not None}
        )
        return replace(self, extras=extras, **values)

    def with_defaults(self, **defaults: Any) -> "AgentConfig":
        """Return a copy with `defaults` applied to the fields still ``None``.

        A mode (repl, web) uses it to settle what the layers left undecided
        without overriding a value the user set.
        """
        unset = {k: v for k, v in defaults.items() if getattr(self, k, None) is None}
        return self.merge(**unset)

    def with_env(self, environ: dict[str, str] | None = None) -> "AgentConfig":
        """Return a copy with ``AGENTIC_<FIELD>`` environment overrides applied."""
        environ = os.environ if environ is None else environ
        overrides: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "extras":
                continue
            raw = environ.get(f"{ENV_PREFIX}{f.name.upper()}")
            if raw is None or raw == "":
                continue
            with contextlib.suppress(ValueError):
                overrides[f.name] = _coerce(raw, _field_kind(str(f.type)))
        return self.merge(**overrides)

    def with_data(self, data: dict[str, Any]) -> "AgentConfig":
        """Return a copy with a parsed mapping applied, coerced to field types.

        Values arrive from a config file, so they are already typed; strings are
        coerced the way the environment layer coerces them, unknown keys go to
        `extras` and a ``null`` leaves the default in place.
        """
        known = {f.name: f for f in fields(self)}
        overrides: dict[str, Any] = {}
        for key, value in flatten_config(data, known).items():
            declared = known.get(key)
            if declared is None:
                overrides[key] = value
                continue
            with contextlib.suppress(ValueError, TypeError):
                overrides[key] = _coerce_value(value, _field_kind(str(declared.type)))
        return self.merge(**overrides)

    def with_file(self, path: Path | str | None = None) -> "AgentConfig":
        """Return a copy with the values of a JSON or YAML config file applied.

        Without a path the file is the one `find_config_file` locates:
        ``agentic.json``, ``agentic.yaml`` or ``settings.yml``, taken from the current
        directory first and from the directory of ``agentic.py`` as a fallback.
        Missing files are not an error when nothing was asked for by name.
        """
        file = Path(path).expanduser() if path else find_config_file()
        if file is None:
            return self
        return self.with_data(read_config_file(file))

    def model_spec(self, role: str = LEADER_ROLE) -> ModelSpec | None:
        """Resolve `role` to a `ModelSpec`, or ``None`` when it is not configured.

        Specialist roles inherit the leader's endpoint and credential unless
        they declare their own, so a second model usually costs one setting.
        """
        if role == LEADER_ROLE:
            return ModelSpec(
                LEADER_ROLE,
                self.inference_llm,
                self.inference_url,
                self.inference_key,
                self.inference_ctx,
            )
        name = getattr(self, ROLE_MODEL_FIELDS.get(role, f"{role}_model"), "") or ""
        if not name:
            return None
        return ModelSpec(
            role,
            name,
            getattr(self, f"{role}_api_url", "") or self.inference_url,
            getattr(self, f"{role}_api_key", "") or self.inference_key,
            self.inference_ctx,
        )

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Serialize the config, masking secrets by default."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Path):
                value = str(value)
            if redact and isinstance(value, str) and is_secret_key(f.name):
                value = mask_secret(value)
            out[f.name] = value
        return out

    def banner_items(self) -> list[tuple[str, str]]:
        """Key/value rows describing the config, used by `ConsoleRenderer`."""
        items = [
            ("Name", self.name),
            ("Model", self.inference_llm),
            ("API URL", self.inference_url),
            ("API Key", mask_secret(self.inference_key) or "(unset)"),
        ]
        vision = self.model_spec(VISION_ROLE)
        if vision is not None:
            items.append(("Vision Model", vision.name))
        if self.repo_url:
            items.append(("Repository", self.repo_url))
        items.append(("Input", self.input_source or "(inline)"))
        return items

    def state_paths(self) -> "AgentConfig":
        """Return a copy with `db_path` and `workspace_root` under `state_dir`.

        Only fields left unset are filled, and a relative `state_dir` is
        anchored at the current directory, so the paths survive a ``chdir``.
        """
        if self.state_dir is None:
            return self
        state = Path(self.state_dir).expanduser().resolve()
        return self.merge(
            state_dir=state,
            db_path=self.db_path or state / f"{STEM}.db",
            workspace_root=self.workspace_root or state / "workspaces",
        )


def resolve_input(raw: str | os.PathLike[str]) -> str:
    """Return input text, reading it from a file when `raw` is a file path."""
    if not raw:
        return ""
    raw = os.fspath(raw)
    candidate = raw.strip()
    if len(candidate) < 4096 and "\n" not in candidate:
        path = Path(candidate).expanduser()
        with contextlib.suppress(OSError, ValueError):
            if path.is_file():
                return path.read_text(encoding="utf-8")
    return raw


# ---------------------------------------------------------------------------
# Events
#
# Every observable side effect of a run (printing, streaming, persistence) is a
# subscriber of this bus, so consumers hook in instead of patching the harness.
# ---------------------------------------------------------------------------


class EventType:
    """Well known event names. Any string is a valid event name."""

    AGENT_START = "agent.start"
    AGENT_END = "agent.end"
    AGENT_ERROR = "agent.error"
    BLOCK_START = "block.start"
    BLOCK_DELTA = "block.delta"
    BLOCK_END = "block.end"
    TOOL_START = "tool.start"
    TOOL_PROGRESS = "tool.progress"
    TOOL_END = "tool.end"
    MODEL_RETRY = "model.retry"
    MODEL_WAIT = "model.wait"
    CONTEXT_USAGE = "context.usage"
    CONTEXT_COMPACT = "context.compact"
    PLAN_UPDATE = "plan.update"
    QUESTION_START = "question.start"
    QUESTION_END = "question.end"
    FEEDBACK = "feedback"
    SESSION_OPEN = "session.open"
    SESSION_CLOSE = "session.close"
    LOG = "log"

    ALL = "*"


@dataclass(slots=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    time: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "data": self.data,
            "session": self.session_id,
            "time": self.time,
        }


class EventBus:
    """A tiny publish/subscribe bus supporting sync and async subscribers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}

    def on(self, event_type: str, handler: Handler) -> Callable[[], None]:
        """Subscribe to an event type (or `EventType.ALL`). Returns an unsubscriber."""
        self._handlers.setdefault(event_type, []).append(handler)
        return lambda: self.off(event_type, handler)

    def off(self, event_type: str, handler: Handler) -> None:
        handlers = self._handlers.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def clear(self) -> None:
        self._handlers.clear()

    def subscribers(self, event_type: str) -> list[Handler]:
        return [*self._handlers.get(event_type, []), *self._handlers.get(EventType.ALL, [])]

    async def emit(self, event: Event) -> None:
        """Deliver an event; a failing subscriber never breaks the run."""
        for handler in self.subscribers(event.type):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[events] subscriber error: {exc!r}", file=sys.stderr)

    async def publish(self, event_type: str, /, session_id: str | None = None, **data: Any) -> Event:
        event = Event(type=event_type, data=data, session_id=session_id)
        await self.emit(event)
        return event


# ---------------------------------------------------------------------------
# Prompt
#
# The consumer owns the template; the harness renders it against the config it
# resolved for itself. `config.input` holds the resolved input text.
# ---------------------------------------------------------------------------


class PromptRenderer:
    """Renders Jinja templates against a config object."""

    def __init__(
        self,
        search_path: Path | str | None = None,
        *,
        strict: bool = True,
        sandboxed: bool = True,
    ) -> None:
        from jinja2 import StrictUndefined, Undefined
        from jinja2 import ChoiceLoader, FileSystemLoader
        from jinja2.sandbox import SandboxedEnvironment
        from jinja2 import Environment

        loaders = []
        if search_path is not None:
            loaders.append(FileSystemLoader(str(search_path)))
        env_cls = SandboxedEnvironment if sandboxed else Environment
        self.env = env_cls(
            loader=ChoiceLoader(loaders) if loaders else None,
            undefined=StrictUndefined if strict else Undefined,
            keep_trailing_newline=True,
            autoescape=False,
        )

    def render(self, template: str, config: Any = None, **context: Any) -> str:
        """Render a template string. `config` is exposed as ``config``."""
        return self.env.from_string(template).render(config=config, **context).strip()

    def render_file(self, path: Path | str, config: Any = None, **context: Any) -> str:
        """Render a template file from disk."""
        text = Path(path).expanduser().read_text(encoding="utf-8")
        return self.render(text, config, **context)


# ---------------------------------------------------------------------------
# Storage
#
# A namespaced key/value `Store` (SQLite by default) and a `Cache` (in-memory
# LRU by default). Both are protocols so other backends can be dropped in.
# ---------------------------------------------------------------------------


@runtime_checkable
class Store(Protocol):
    """Durable namespaced key/value storage."""

    def get(self, namespace: str, key: str) -> Any | None: ...
    def set(self, namespace: str, key: str, value: Any) -> None: ...
    def delete(self, namespace: str, key: str) -> None: ...
    def list(self, namespace: str) -> list[tuple[str, Any]]: ...
    def close(self) -> None: ...


@runtime_checkable
class Cache(Protocol):
    """Fast, volatile storage."""

    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any) -> None: ...
    def delete(self, key: str) -> None: ...
    def clear(self) -> None: ...


class MemoryStore:
    """In-process `Store`, used for tests and ephemeral runs."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def get(self, namespace: str, key: str) -> Any | None:
        return self._data.get(namespace, {}).get(key)

    def set(self, namespace: str, key: str, value: Any) -> None:
        self._data.setdefault(namespace, {})[key] = value

    def delete(self, namespace: str, key: str) -> None:
        self._data.get(namespace, {}).pop(key, None)

    def list(self, namespace: str) -> list[tuple[str, Any]]:
        return list(self._data.get(namespace, {}).items())

    def clear(self, namespace: str) -> None:
        """Delete every key of a namespace."""
        self._data.pop(namespace, None)

    def close(self) -> None:
        self._data.clear()


class SqliteStore:
    """SQLite backed `Store`. Values are JSON encoded."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = ":memory:" if path is None else str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            " ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,"
            " updated_at REAL NOT NULL, PRIMARY KEY (ns, key))"
        )
        self._db.commit()

    def get(self, namespace: str, key: str) -> Any | None:
        row = self._db.execute(
            "SELECT value FROM kv WHERE ns = ? AND key = ?", (namespace, key)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, namespace: str, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT INTO kv (ns, key, value, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(ns, key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (namespace, key, json.dumps(value), time.time()),
        )
        self._db.commit()

    def delete(self, namespace: str, key: str) -> None:
        self._db.execute("DELETE FROM kv WHERE ns = ? AND key = ?", (namespace, key))
        self._db.commit()

    def list(self, namespace: str) -> list[tuple[str, Any]]:
        rows = self._db.execute(
            "SELECT key, value FROM kv WHERE ns = ? ORDER BY updated_at", (namespace,)
        ).fetchall()
        return [(key, json.loads(value)) for key, value in rows]

    def clear(self, namespace: str) -> None:
        """Delete every key of a namespace in one statement."""
        self._db.execute("DELETE FROM kv WHERE ns = ?", (namespace,))
        self._db.commit()

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._db.close()


class LruCache:
    """In-memory LRU `Cache` backed by ``cachetools``."""

    def __init__(self, maxsize: int = 256, ttl: float | None = None) -> None:
        import cachetools

        self._cache: Any = (
            cachetools.TTLCache(maxsize=maxsize, ttl=ttl)
            if ttl
            else cachetools.LRUCache(maxsize=maxsize)
        )

    def get(self, key: str) -> Any | None:
        return self._cache.get(key)

    def set(self, key: str, value: Any) -> None:
        self._cache[key] = value

    def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


# ---------------------------------------------------------------------------
# Sessions
#
# A session owns an isolated workspace directory. When it is closed or expires
# the directory is removed, leaving no trace on the host.
#
# The record of a live session is written to the `Store`, so a manager built
# over the same store rehydrates what was live when the process stopped,
# reaps the workspaces that no longer have an owner, and expires the rest on a
# timer rather than only when someone asks for them.
# ---------------------------------------------------------------------------


@dataclass
class Session:
    id: str
    workspace: Path
    created_at: float
    expires_at: float
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def touch(self, ttl: float) -> None:
        self.expires_at = time.time() + ttl

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace": str(self.workspace),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        """Rebuild a session from a stored record."""
        return cls(
            id=str(data["id"]),
            workspace=Path(str(data["workspace"])),
            created_at=float(data.get("created_at") or 0.0),
            expires_at=float(data.get("expires_at") or 0.0),
            meta=dict(data.get("meta") or {}),
        )


class SessionManager:
    """Creates, tracks and destroys isolated session workspaces."""

    NAMESPACE = "sessions"

    def __init__(
        self,
        *,
        root: Path | None = None,
        ttl: float = 3600,
        store: Store | None = None,
        cache: Cache | None = None,
        prefix: str = "agent",
        keep_workspace: bool = False,
        seed: Path | None = None,
        durable: bool = False,
        sweep_interval: float = 0,
        restore: bool = True,
    ) -> None:
        self.root = Path(root).expanduser() if root else None
        self.seed = Path(seed).expanduser() if seed else None
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.store = store
        self.cache = cache
        self.prefix = prefix
        self.keep_workspace = keep_workspace
        self.durable = durable
        self.sweep_interval = sweep_interval
        #: Called with every session as it is destroyed (closed, expired or
        #: reconciled away), so what belongs to it can go with it.
        self.listeners: list[Callable[[Session], None]] = []
        self._sessions: dict[str, Session] = {}
        self._sweeper: "asyncio.Task[None] | None" = None
        if restore:
            self.restore()

    def create(self, **meta: Any) -> Session:
        session_id = uuid.uuid4().hex[:12]
        workspace = Path(
            tempfile.mkdtemp(prefix=f"{self.prefix}-{session_id}-", dir=self.root)
        ).resolve()
        if self.seed is not None and self.seed.is_dir():
            shutil.copytree(self.seed, workspace, dirs_exist_ok=True, symlinks=False)
        now = time.time()
        session = Session(
            id=session_id,
            workspace=workspace,
            created_at=now,
            expires_at=now + self.ttl,
            meta=meta,
        )
        self._sessions[session_id] = session
        self._persist(session)
        return session

    def get(self, session_id: str | None) -> Session | None:
        """Return a live session, renewing its lease, or ``None``.

        Access renews the lease so an active session is never expired under a
        client; an idle one is left to the sweeper or to the next access.
        """
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.expired:
            self.close(session_id)
            return None
        session.touch(self.ttl)
        self._persist(session)
        return session

    def ensure(self, session_id: str | None = None, **meta: Any) -> Session:
        """Return an existing live session or create a new one."""
        return self.get(session_id) or self.create(**meta)

    def update(self, session: Session) -> Session:
        """Persist changes made to the metadata of a live session."""
        self._persist(session)
        return session

    def list(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.created_at)

    def latest(self, **meta: Any) -> Session | None:
        """The most recently used live session whose meta matches `meta`.

        Every use renews a lease, so the latest expiry is the latest use.
        """
        live = [
            s for s in self._sessions.values()
            if not s.expired and all(s.meta.get(k) == v for k, v in meta.items())
        ]
        latest = max(live, key=lambda s: s.expires_at, default=None)
        return self.get(latest.id) if latest is not None else None

    def close(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        self._destroy(session)
        return True

    def purge_expired(self) -> list[str]:
        expired = [s.id for s in list(self._sessions.values()) if s.expired]
        for session_id in expired:
            self.close(session_id)
        return expired

    def close_all(self, destroy: bool | None = None) -> None:
        """Release every live session.

        Durable managers only forget them: the workspaces and the store records
        are what `restore()` needs on the next start. Pass ``destroy`` to decide
        explicitly.
        """
        if destroy is None:
            destroy = not self.durable
        if not destroy:
            self._sessions.clear()
            return
        for session_id in list(self._sessions):
            self.close(session_id)

    # -- durability ---------------------------------------------------------

    def restore(self) -> list[Session]:
        """Rehydrate the sessions the store still holds a record of.

        A record whose workspace is gone, whose lease has run out or which
        cannot be read is reconciled away instead of being adopted, so a
        restart never hands out a session that no longer has a directory.
        """
        if self.store is None:
            return []
        restored: list[Session] = []
        for session_id, record in self.store.list(self.NAMESPACE):
            try:
                session = Session.from_dict(record)
            except (AttributeError, KeyError, TypeError, ValueError):
                self.store.delete(self.NAMESPACE, session_id)
                if self.cache is not None:
                    self.cache.delete(session_id)
                continue
            if session.expired or not session.workspace.is_dir():
                self._destroy(session)
                continue
            self._sessions[session.id] = session
            if self.cache is not None:
                self.cache.set(session.id, session.to_dict())
            restored.append(session)
        return restored

    def orphans(self) -> list[Path]:
        """Workspace directories under the root that no live session owns.

        Only a configured root is scanned, and only the directories this
        manager could have created: a shared temporary directory is never
        touched.
        """
        if self.root is None or not self.root.is_dir():
            return []
        owned = {str(session.workspace) for session in self._sessions.values()}
        found: list[Path] = []
        with contextlib.suppress(OSError):
            for path in sorted(self.root.iterdir()):
                if not path.is_dir() or not path.name.startswith(f"{self.prefix}-"):
                    continue
                if str(path) in owned or str(path.resolve()) in owned:
                    continue
                found.append(path)
        return found

    def reap_orphans(self) -> list[Path]:
        """Remove the orphaned workspaces, unless workspaces are kept."""
        if self.keep_workspace:
            return []
        reaped: list[Path] = []
        for path in self.orphans():
            shutil.rmtree(path, ignore_errors=True)
            reaped.append(path)
        return reaped

    def sweep(self) -> tuple[list[str], list[Path]]:
        """Expire what is due and reap what has no owner."""
        return self.purge_expired(), self.reap_orphans()

    def start_sweeper(self, interval: float | None = None) -> "asyncio.Task[None] | None":
        """Sweep on a timer for as long as the loop runs. Idempotent."""
        period = self.sweep_interval if interval is None else interval
        if period <= 0:
            return None
        if self._sweeper is not None and not self._sweeper.done():
            return self._sweeper
        self._sweeper = asyncio.create_task(self._sweep_forever(period))
        return self._sweeper

    async def stop_sweeper(self) -> None:
        task, self._sweeper = self._sweeper, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _sweep_forever(self, period: float) -> None:
        while True:
            await asyncio.sleep(period)
            try:
                await asyncio.to_thread(self.sweep)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[sessions] sweep failed: {exc}")

    # -- overridable hooks --------------------------------------------------

    def _persist(self, session: Session) -> None:
        if self.store is not None:
            self.store.set(self.NAMESPACE, session.id, session.to_dict())
        if self.cache is not None:
            self.cache.set(session.id, session.to_dict())

    def _destroy(self, session: Session) -> None:
        for listener in list(self.listeners):
            try:
                listener(session)
            except Exception as exc:  # a listener never keeps a session alive
                print(f"[sessions] close listener failed: {exc!r}", file=sys.stderr)
        if not self.keep_workspace:
            shutil.rmtree(session.workspace, ignore_errors=True)
        if self.store is not None:
            self.store.delete(self.NAMESPACE, session.id)
        if self.cache is not None:
            self.cache.delete(session.id)


# ---------------------------------------------------------------------------
# Memory
#
# A session remembers its conversation: every run journals the exchange to a
# transcript that is persisted through the `Store`, one record per turn, and
# kept whole: a client that reconnects is shown all of it. The model is shown a
# window onto it instead, the newest summary and every turn after the point
# that summary covers (see Context).
# ---------------------------------------------------------------------------

#: Roles a remembered turn may carry, mapped onto the block kind of the UI.
TURN_KINDS = {
    "user": "prompt",
    "assistant": "output",
    "system": "log",
    "tool": "tool",
    "compact": "compact",
}

#: Run states a journaled prompt carries in `Turn.meta["status"]`.
RUN_RUNNING, RUN_OK, RUN_FAILED, RUN_CANCELLED = "running", "ok", "failed", "cancelled"


@dataclass
class Turn:
    """One remembered message of a conversation.

    A turn journaled from a run also carries the id of the block it came from
    (so a client that reconnects keeps the ids it shows), the id of the run,
    and the raw model input `items` it stands for in the context window — a
    tool call with its output, an assistant message — which are replayed to
    the model instead of the plain ``{role, content}`` message. `meta` holds
    the status of a run on its prompt, the vote on an answer, and on a tool
    call the stub its output was `cleared` to. `seq` orders the turns of a
    transcript and keys their records in the store.

    A ``compact`` turn marks a compaction: its text is shown to a client, and
    when it carries a summary (``meta["summary"]``) the newest one opens the
    context window in place of every turn before the point it keeps from
    (``meta["keep"]``).
    """

    role: str
    text: str
    created_at: float = field(default_factory=time.time)
    id: str = ""
    run: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    @property
    def kind(self) -> str:
        return TURN_KINDS.get(self.role, "output")

    @property
    def call_id(self) -> str:
        """The id of the call a tool turn journals, else empty."""
        if self.role != "tool" or not self.items:
            return ""
        return str(self.items[0].get("call_id") or "")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "text": self.text,
            "kind": self.kind,
            "created_at": self.created_at,
        }
        for key in ("id", "run", "items", "meta", "seq"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Turn":
        items = data.get("items")
        meta = data.get("meta")
        return cls(
            role=str(data.get("role") or "user"),
            text=str(data.get("text") or ""),
            created_at=float(data.get("created_at") or time.time()),
            id=str(data.get("id") or ""),
            run=str(data.get("run") or ""),
            items=[i for i in items if isinstance(i, dict)] if isinstance(items, list) else [],
            meta=dict(meta) if isinstance(meta, dict) else {},
            seq=int(data.get("seq") or 0),
        )

    def as_message(self) -> dict[str, str]:
        """The turn as a chat message the model understands."""
        return {"role": self.role, "content": self.text}

    def replay(self, tools: bool = True) -> list[dict[str, Any]]:
        """What this turn puts in the context window of a later run.

        Raw items when the run journaled them, else the plain message; a tool
        turn is left out when `tools` is false, and so is one without items,
        since a tool result means nothing without the call that asked for it.
        A cleared tool output is replayed as its stub, and a compaction never:
        its summary is placed by `Transcript.window`. A turn a branch folded
        away (`folded`) is not replayed: the `return` that closed it stands
        for it.
        """
        if self.role == "compact" or self.meta.get("folded"):
            return []
        if self.role == "tool":
            if not tools:
                return []
            stub = self.meta.get("cleared")
            if isinstance(stub, str) and len(self.items) == 2:
                return [self.items[0], {**self.items[1], "output": stub}]
            return list(self.items)
        if self.items:
            return list(self.items)
        return [self.as_message()] if self.role in ("user", "assistant", "system") else []


@dataclass
class Transcript:
    """The ordered turns of one session."""

    session_id: str
    turns: list[Turn] = field(default_factory=list)
    #: The `seq` the next appended turn gets.
    next_seq: int = 1

    def __post_init__(self) -> None:
        self.next_seq = max([self.next_seq, *(t.seq + 1 for t in self.turns)])

    def __len__(self) -> int:
        return len(self.turns)

    def append(self, *turns: Turn) -> list[Turn]:
        """Append the turns worth keeping, numbering them; returns those kept."""
        kept = [t for t in turns if t.text.strip() or t.items]
        for turn in kept:
            turn.seq = self.next_seq
            self.next_seq += 1
        self.turns.extend(kept)
        return kept

    def summary(self) -> Turn | None:
        """The newest compaction that carries a summary."""
        return next(
            (t for t in reversed(self.turns) if t.role == "compact" and t.meta.get("summary")),
            None,
        )

    def boundary(self, summary: Turn | None = None) -> int:
        """Index of the first turn the model sees verbatim after `summary`.

        A summary keeps from a turn (``{"seq": n}``) or, when it was made in
        the middle of a run before that run's turns were journaled, from the
        response that made the calls ``{"run": id, "calls": [...]}``: the
        first of their tool turns, and the assistant message that announced
        them. A reference that resolves to nothing keeps every turn after the
        summary itself.
        """
        summary = summary if summary is not None else self.summary()
        if summary is None:
            return 0
        keep = summary.meta.get("keep") or {}
        seq = keep.get("seq")
        if isinstance(seq, int) and seq > 0:
            return next((i for i, t in enumerate(self.turns) if t.seq >= seq), len(self.turns))
        calls = {str(c) for c in keep.get("calls") or ()}
        hits = [i for i, t in enumerate(self.turns) if calls and t.call_id in calls]
        if hits:
            index, run = min(hits), keep.get("run")
            while index and self.turns[index - 1].role == "assistant" and self.turns[index - 1].run == run:
                index -= 1
            return index
        return self.turns.index(summary) + 1 if summary in self.turns else len(self.turns)

    def window(self, tools: bool = True) -> tuple[list[dict[str, Any]], list[Turn]]:
        """The context window, and the turn each of its items stands for.

        The newest summary first, then every turn from its `boundary` on as
        the model input it stands for.
        """
        summary = self.summary()
        items: list[dict[str, Any]] = []
        owners: list[Turn] = []
        if summary is not None:
            items.append(summary_message(summary.text))
            owners.append(summary)
        for turn in self.turns[self.boundary(summary):]:
            for item in turn.replay(tools):
                items.append(item)
                owners.append(turn)
        return items, owners

    def messages(self, tools: bool = True) -> list[dict[str, Any]]:
        """The context window: what the model is shown of this conversation."""
        return self.window(tools)[0]

    def find(self, turn_id: str) -> Turn | None:
        """The turn journaled from block `turn_id`, if it is still remembered."""
        return next((t for t in self.turns if turn_id and t.id == turn_id), None)

    def run_of(self, run_id: str) -> list[Turn]:
        """The turns of one run, oldest first."""
        return [t for t in self.turns if run_id and t.run == run_id]

    def prompt_of(self, run_id: str) -> Turn | None:
        """The journaled prompt of a run, which carries its status."""
        return next((t for t in self.run_of(run_id) if t.role == "user"), None)

    def last_run(self) -> Turn | None:
        """The prompt of the newest journaled run."""
        return next((t for t in reversed(self.turns) if t.role == "user" and t.run), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session_id,
            "turns": [turn.to_dict() for turn in self.turns],
        }

    @classmethod
    def from_dict(cls, session_id: str, data: dict[str, Any] | None) -> "Transcript":
        turns = (data or {}).get("turns") or []
        return cls(
            session_id=session_id,
            turns=[Turn.from_dict(t) for t in turns if isinstance(t, dict)],
        )


class ConversationMemory:
    """Transcripts by session, persisted through a `Store`, one turn a record.

    A transcript is only ever appended to (and a turn's `meta` updated), so a
    write costs one record however long the conversation has grown. Nothing
    is trimmed here: what the model is shown is decided by the context window
    (`Transcript.window`) and its compaction. A consumer that wants another
    policy subclasses this, or decides what is remembered at all by overriding
    `Agent.remember`.
    """

    #: One header record per session; its turns live under ``<NAMESPACE>:<session>``.
    NAMESPACE = "transcripts"
    #: Longest tool output kept in the context window, head and tail.
    TOOL_LIMIT = 8000
    #: Version of the records; a header without it holds its turns inline.
    FORMAT = 2

    def __init__(
        self,
        store: Store | None = None,
        *,
        enabled: bool = True,
        tools: bool = True,
    ) -> None:
        self.store = store
        self.enabled = enabled
        #: Replay journaled tool calls to the model, not only the words.
        self.tools = tools
        self._transcripts: dict[str, Transcript] = {}

    @classmethod
    def turns_namespace(cls, session_id: str) -> str:
        return f"{cls.NAMESPACE}:{session_id}"

    @staticmethod
    def turn_key(turn: Turn) -> str:
        return f"{turn.seq:012d}"

    def transcript(self, session_id: str) -> Transcript:
        """Return the transcript of a session, loading it from the store once.

        A header written before turns had records of their own holds them
        inline; it is rewritten as one record per turn on load.
        """
        transcript = self._transcripts.get(session_id)
        if transcript is not None:
            return transcript
        header = self.store.get(self.NAMESPACE, session_id) if self.store else None
        if isinstance(header, dict) and "turns" in header:
            transcript = Transcript.from_dict(session_id, header)
            for index, turn in enumerate(transcript.turns, 1):
                turn.seq = index
            transcript.next_seq = len(transcript.turns) + 1
            self._transcripts[session_id] = transcript
            if self.enabled:
                self.persist(transcript, *transcript.turns, header=True)
            return transcript
        rows = self.store.list(self.turns_namespace(session_id)) if self.store else []
        turns = [Turn.from_dict(value) for _key, value in sorted(rows) if isinstance(value, dict)]
        transcript = Transcript(session_id=session_id, turns=turns)
        self._transcripts[session_id] = transcript
        return transcript

    def window(self, session_id: str) -> tuple[list[dict[str, Any]], list[Turn]]:
        """The context window of a session with the turn behind each item."""
        if not self.enabled:
            return [], []
        return self.transcript(session_id).window(self.tools)

    def history(self, session_id: str) -> list[dict[str, Any]]:
        """The context window as model input, oldest first."""
        return self.window(session_id)[0]

    def journal(self, session_id: str, *turns: Turn) -> Transcript:
        """Append `turns` and persist them at once.

        This is the write-ahead half of memory: a run records each step as it
        finishes, so a crash costs at most the step in flight.
        """
        transcript = self.transcript(session_id)
        if not self.enabled:
            return transcript
        fresh = not transcript.turns
        self.persist(transcript, *transcript.append(*turns), header=fresh)
        return transcript

    def replay(self, session_id: str) -> list[dict[str, Any]]:
        """Every remembered turn as JSON for a client that (re)connects."""
        if not self.enabled:
            return []
        return [turn.to_dict() for turn in self.transcript(session_id).turns]

    async def remember(self, session_id: str, turns: Iterable[Turn]) -> Transcript:
        """Append `turns` and persist them (`journal` for a coroutine caller)."""
        return self.journal(session_id, *turns)

    def persist(self, transcript: Transcript, *turns: Turn, header: bool = False) -> None:
        """Write the records of `turns` (all of them when none are named)."""
        if self.store is None or not self.enabled:
            return
        if header or not turns:
            self.store.set(
                self.NAMESPACE,
                transcript.session_id,
                {"session": transcript.session_id, "format": self.FORMAT},
            )
        namespace = self.turns_namespace(transcript.session_id)
        for turn in turns or transcript.turns:
            self.store.set(namespace, self.turn_key(turn), turn.to_dict())

    def forget(self, session_id: str) -> None:
        """Erase the transcript of a session, in memory and in the store."""
        self._transcripts.pop(session_id, None)
        if self.store is None:
            return
        namespace = self.turns_namespace(session_id)
        clear = getattr(self.store, "clear", None)
        if callable(clear):
            clear(namespace)
        else:
            for key, _value in self.store.list(namespace):
                self.store.delete(namespace, key)
        self.store.delete(self.NAMESPACE, session_id)


# ---------------------------------------------------------------------------
# Context
#
# The model sees a window onto the conversation, never necessarily all of it:
# the newest items verbatim and a summary of everything older. Budgets are
# tokens of the leader's context window, estimated locally and calibrated on
# the usage the provider reports. Compaction runs in tiers, cheapest first,
# and only past a high watermark, down to a low one, so the prefix a provider
# caches stays put between compactions and compactions are rare. It runs
# before every model call of a run (the SDK's `call_model_input_filter`), so a
# long run of tool calls is compacted as it goes, and over the journal between
# runs.
# ---------------------------------------------------------------------------

#: Window assumed when neither the config nor the provider says.
DEFAULT_CONTEXT_WINDOW = 128_000

#: Window assumed for an Ollama server that says nothing of its runtime
#: context: it truncates a longer prompt silently, from the front.
OLLAMA_CONTEXT_WINDOW = 8192

#: Opens the message that stands in for the compacted part of a conversation.
SUMMARY_HEADER = (
    "[Context summary] The earlier part of this conversation was compacted to "
    "save context. This summary replaces it; everything after it is verbatim."
)

#: Heads the copy of the request in flight that a summary keeps word for word.
PINNED_HEADER = "## Active request (verbatim)"

#: Stands in for the reasoning of an earlier step once it is cleared.
THOUGHT_STUB = "[earlier reasoning cleared to save context]"
#: Heads a tool output compaction clipped: the same call returns the same size.
CLIP_NOTE = (
    "[output clipped to fit the context window. Calling the tool again returns "
    "the same size; ask for a narrower part (a line range, a filter) instead.]"
)


def summary_message(text: str) -> dict[str, str]:
    """The message a summary is shown to the model as.

    A user message rather than a system one: several chat templates refuse a
    system message anywhere but first.
    """
    return {"role": "user", "content": f"{SUMMARY_HEADER}\n\n{text.strip()}"}


def item_kind(item: Any) -> str:
    """What a model input item is: the role of a message, else its type."""
    if not isinstance(item, dict):
        return "other"
    kind = str(item.get("type") or "")
    if kind in ("", "message"):
        return str(item.get("role") or "other")
    return kind


def is_call(kind: str) -> bool:
    """A tool call item (``function_call``, ``custom_tool_call``, …)."""
    return kind.endswith("_call")


def is_output(kind: str) -> bool:
    """The output item of a tool call."""
    return kind.endswith("_call_output")


def safe_cuts(items: list[Any]) -> list[int]:
    """The indices a list of input items may be cut at, ascending.

    Everything before a cut can be summarised away and everything from it on
    kept without leaving half of a pair behind: a call stays with its output,
    reasoning with the message or call it led to, and the calls of a response
    with the message that announced them. Index 0 is not a cut.
    """
    answered = {item.get("call_id") for item in items if is_output(item_kind(item))}
    pending: set[Any] = set()
    cuts: list[int] = []
    previous = ""
    for index, item in enumerate(items):
        kind = item_kind(item)
        if (
            index
            and not pending
            and previous != "reasoning"
            and not is_output(kind)
            and not (is_call(kind) and (previous == "assistant" or is_call(previous)))
        ):
            cuts.append(index)
        if is_call(kind) and item.get("call_id") in answered:
            pending.add(item.get("call_id"))
        elif is_output(kind):
            pending.discard(item.get("call_id"))
        previous = kind
    return cuts


def part_text(part: Any) -> str:
    """The text of one content part; media as a short placeholder."""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part)
    if "text" in part:
        return str(part.get("text") or "")
    kind = str(part.get("type") or "")
    if "image" in kind:
        return "[image]"
    if "file" in kind:
        return f"[file {part.get('filename') or ''}]".replace(" ]", "]")
    return json_dump(part)


def content_text(value: Any) -> str:
    """The text of a message content, a tool output or reasoning parts."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text for text in (part_text(p) for p in value) if text)
    return part_text(value)


def item_text(item: Any) -> str:
    """The words an input item carries, whatever its kind."""
    if not isinstance(item, dict):
        return str(item)
    kind = item_kind(item)
    if is_call(kind):
        args = item.get("arguments") if "arguments" in item else item.get("input")
        return f"{item.get('name') or kind}({content_text(args)})"
    if is_output(kind):
        return content_text(item.get("output"))
    if kind == "reasoning":
        return content_text([*(item.get("summary") or []), *(item.get("content") or [])])
    return content_text(item.get("content"))


def reasoning_replayed(model: str) -> bool:
    """Whether the SDK sends earlier reasoning back to `model` within a run.

    It mirrors the SDK's default for Chat Completions: only a DeepSeek model
    gets its ``reasoning_content`` back (it needs it during a tool loop); any
    other drops it, so it costs nothing and is not counted.
    """
    return "deepseek" in model.lower()


class TokenCounter:
    """Estimates what model input costs in tokens, calibrated by real usage.

    Text is counted with a tiktoken encoding when it loads (its vocabulary is
    downloaded once, then cached by tiktoken), else estimated from its UTF-8
    size. Neither is the tokenizer of an arbitrary provider, so `ratio` scales
    the estimate by what the provider reported for recent calls: the estimate
    only has to be proportional, the calibration makes it right.
    """

    BYTES_PER_TOKEN = 4
    #: Framing every message or item costs on top of its content.
    MESSAGE_TOKENS = 4
    #: What an image costs, whatever its size (providers differ widely).
    IMAGE_TOKENS = 1024
    #: Item counts remembered, by a digest of the item.
    CACHE_SIZE = 8192
    #: An input smaller than this is too noisy to calibrate on.
    CALIBRATE_MIN = 256
    #: Bounds of `ratio`: past them it is a mismatch, not a tokenizer.
    RATIO_BOUNDS = (0.25, 4.0)
    #: Encodings tried so far, shared by every counter (``None``: unavailable).
    encodings: dict[str, Any] = {}

    def __init__(self, encoding: str | None = None) -> None:
        self.encoding = encoding or None
        #: Provider tokens per estimated token.
        self.ratio = 1.0
        self.samples = 0
        self._cache: dict[tuple[int, int], int] = {}

    def load(self) -> Any | None:
        """The tiktoken encoding, loaded once per process; ``None`` estimates."""
        if self.encoding is None:
            return None
        if self.encoding not in self.encodings:
            try:
                import tiktoken

                self.encodings[self.encoding] = tiktoken.get_encoding(self.encoding)
            except Exception as exc:  # offline, or tiktoken missing: estimate
                print(
                    f"[context] tokenizer {self.encoding} unavailable, estimating: {exc!r}",
                    file=sys.stderr,
                )
                self.encodings[self.encoding] = None
        return self.encodings[self.encoding]

    def text(self, text: str) -> int:
        """Tokens of a text, uncalibrated."""
        if not text:
            return 0
        encoding = self.load()
        if encoding is not None:
            with contextlib.suppress(Exception):
                return len(encoding.encode(text, disallowed_special=()))
        return -(-len(text.encode("utf-8", "replace")) // self.BYTES_PER_TOKEN)

    @staticmethod
    def key(item: Any) -> tuple[int, int]:
        """A digest of an input item, stable for one process."""
        try:
            text = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(item)
        return len(text), hash(text)

    def item(self, item: Any) -> int:
        """Tokens of one model input item, uncalibrated and cached."""
        key = self.key(item)
        count = self._cache.get(key)
        if count is None:
            count = self.MESSAGE_TOKENS + self.measure(item)
            if len(self._cache) >= self.CACHE_SIZE:
                self._cache.clear()
            self._cache[key] = count
        return count

    def measure(self, item: Any) -> int:
        if not isinstance(item, dict):
            return self.text(str(item))
        kind = item_kind(item)
        if is_call(kind):
            args = item.get("arguments") if "arguments" in item else item.get("input")
            return self.text(str(item.get("name") or "")) + self.content(args)
        if is_output(kind):
            return self.content(item.get("output"))
        if kind == "reasoning":
            return self.content([*(item.get("summary") or []), *(item.get("content") or [])])
        return self.content(item.get("content"))

    def content(self, value: Any) -> int:
        """Tokens of a content value: text, or a list of parts with media."""
        if value is None:
            return 0
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return sum(self.part(part) for part in value)
        return self.part(value)

    def part(self, part: Any) -> int:
        if isinstance(part, dict) and "text" not in part and "image" in str(part.get("type")):
            return self.IMAGE_TOKENS
        return self.text(part_text(part))

    def items(self, items: Iterable[Any]) -> int:
        return sum(self.item(item) for item in items)

    def scaled(self, raw: int | float) -> int:
        """An estimate in provider tokens."""
        return int(raw * self.ratio + 0.5)

    def raw(self, tokens: int | float) -> int:
        """Provider tokens in estimate units (the inverse of `scaled`)."""
        return int(tokens / self.ratio)

    def calibrate(self, estimated: int, reported: int) -> float:
        """Fold a provider's count for an input we estimated into `ratio`."""
        if estimated < self.CALIBRATE_MIN or reported <= 0:
            return self.ratio
        low, high = self.RATIO_BOUNDS
        sample = min(max(reported / estimated, low), high)
        self.ratio = sample if not self.samples else (self.ratio + sample) / 2
        self.samples += 1
        return self.ratio


@dataclass(frozen=True)
class ContextBudget:
    """The token budget of a context window and its two watermarks.

    `usable` is what the input may take (the window minus the room reserved
    for the reply, reasoning included); compaction starts past `high` and
    stops at `low`, so the input grows by the gap between them before the
    next compaction.
    """

    window: int
    reserve: int
    trigger: float = 0.85
    target: float = 0.5

    @classmethod
    def of(
        cls, window: int, reserve: int = 0, trigger: float = 0.85, target: float = 0.5
    ) -> "ContextBudget":
        """A budget with the defaults filled in and the values made sane.

        A reserve of 0 is a fifth of the window, between 4k and 32k tokens
        (a model that thinks a lot spends that much on one reply), and never
        more than half of it.
        """
        window = max(int(window or DEFAULT_CONTEXT_WINDOW), 1024)
        reserve = int(reserve) if reserve and reserve > 0 else min(max(window // 5, 4096), 32768)
        reserve = min(reserve, window // 2)
        trigger = min(max(float(trigger), 0.05), 1.0)
        target = min(max(float(target), 0.01), trigger)
        return cls(window, reserve, trigger, target)

    @property
    def usable(self) -> int:
        return self.window - self.reserve

    @property
    def high(self) -> int:
        return int(self.usable * self.trigger)

    @property
    def low(self) -> int:
        return int(self.usable * self.target)

    def usage(self, tokens: int, *, estimated: bool = True) -> dict[str, Any]:
        """The `context.usage` payload for an input of `tokens`."""
        return {
            "tokens": int(tokens),
            "window": self.window,
            "usable": self.usable,
            "high": self.high,
            "low": self.low,
            "fill": round(min(tokens / self.usable, 1.0), 4) if self.usable else 0.0,
            "estimated": estimated,
        }

    def capped(self, usable: int) -> "ContextBudget":
        """This budget with at most `usable` tokens of input (0: unchanged).

        The reserve for the reply stays whole: only the input gives way.
        """
        if usable <= 0 or usable >= self.usable:
            return self
        return ContextBudget(usable + self.reserve, self.reserve, self.trigger, self.target)


@dataclass
class Throughput:
    """How fast one model reads its input and writes its reply, as measured.

    `prefill` is input tokens read per second before the first token arrives,
    `decode` output tokens written per second after it; each is the median of
    the newest `WINDOW` calls worth timing, so one odd call (a prompt cache
    the server dropped, another session queued ahead) cannot move it. Too few
    tokens and what is timed is the round trip, not the model, so such calls
    are left out.

    `replies` are the output tokens of the newest calls, timed or not: a reply
    a server sends whole (a tool call on Ollama) keeps the stream silent while
    it is written, so the time cap leaves room to write one as long as the
    longest of them (`readable`).
    """

    prefill: float = 0.0
    decode: float = 0.0
    samples: int = 0
    reads: list[float] = field(default_factory=list, repr=False)
    writes: list[float] = field(default_factory=list, repr=False)
    replies: list[int] = field(default_factory=list, repr=False)
    #: When the newest call of the model ended, on the clock that timed it.
    last: float | None = field(default=None, repr=False)

    #: Fewer tokens read (written) than this time the connection, not the model.
    MIN_READ = 512
    MIN_WRITE = 64
    #: Newest samples the median is taken over.
    WINDOW = 9
    #: Share of a time budget always left to reading, however long a reply.
    READ_SHARE = 0.5

    def observe(self, read: int, first: float, wrote: int = 0, writing: float = 0.0) -> None:
        """Fold in one call: `read` tokens before a first token after `first`
        seconds, then `wrote` tokens in `writing` seconds."""
        if read >= self.MIN_READ and first > 0:
            self.prefill = self.median(self.reads, read / first)
            self.samples += 1
        if wrote >= self.MIN_WRITE and writing > 0:
            self.decode = self.median(self.writes, wrote / writing)

    def median(self, samples: list[float], sample: float) -> float:
        samples.append(sample)
        del samples[: -self.WINDOW]
        return statistics.median(samples)

    def wrote(self, tokens: int) -> None:
        """Keep the length of one reply, timed or not (0: nothing written)."""
        if tokens > 0:
            self.replies.append(int(tokens))
            del self.replies[: -self.WINDOW]

    @property
    def reply(self) -> int:
        """Output tokens of the longest of the newest replies."""
        return max(self.replies, default=0)

    def tokens(self, seconds: float) -> int:
        """Input tokens the model reads in `seconds` (0: not measured yet)."""
        return int(self.prefill * seconds) if self.prefill > 0 and seconds > 0 else 0

    def seconds(self, tokens: int) -> float:
        """Seconds the model takes to read `tokens` (0: not measured yet)."""
        return tokens / self.prefill if self.prefill > 0 else 0.0

    def readable(self, seconds: float, reserve: int = 0) -> int:
        """Input tokens the model reads in `seconds` and still writes a reply
        as long as its longest recent one (at most `reserve` tokens, 0: no
        bound) before they are up; 0 while unmeasured.

        Writing is reckoned at the measured `decode` (nothing while it is
        unknown) and never takes more than `1 - READ_SHARE` of the time.
        """
        if self.prefill <= 0 or seconds <= 0:
            return 0
        reply = min(self.reply, reserve) if reserve > 0 else self.reply
        writing = reply / self.decode if self.decode > 0 else 0.0
        return self.tokens(max(seconds - writing, seconds * self.READ_SHARE))

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefill": round(self.prefill, 1),
            "decode": round(self.decode, 1),
            "samples": self.samples,
            "reply": self.reply,
        }


@dataclass(frozen=True)
class SliceBudget:
    """What one step of work may bring into the context.

    `usable` is the window's budget for input; `timed` the input the model can
    read again from scratch within its share of the stall guard (0: not yet
    measured), which is what bounds a slow local model long before its window
    does; `share` the part of the smaller of the two one step may take; `used`
    the context in use now.
    """

    usable: int
    timed: int = 0
    share: float = 0.25
    used: int = 0

    @property
    def limit(self) -> int:
        return min(self.usable, self.timed) if self.timed > 0 else self.usable

    @property
    def slice(self) -> int:
        return max(int(self.limit * self.share), 1)


def compact_tokens(count: int) -> str:
    """A token count for people: ``950``, ``12k``, ``1.2M``."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 10_000:
        return f"{count / 1000:.0f}k"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


@dataclass
class Compaction:
    """What one compaction did, for its events and its journal record."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    #: ``trigger`` (past the high watermark), ``forced`` (asked) or ``overflow``.
    reason: str = "trigger"
    #: Provider tokens of the input before and after.
    before: int = 0
    after: int = 0
    #: Reasoning items cleared, tool outputs cleared and clipped (by call id).
    thoughts: int = 0
    cleared: list[str] = field(default_factory=list)
    clipped: list[str] = field(default_factory=list)
    #: Items folded into the summary, and the summary now standing for them.
    summarized: int = 0
    summary: str = ""
    #: Index of the first item kept verbatim.
    cut: int = 0

    def describe(self) -> str:
        """One line for people: what went and what it saved."""
        parts = []
        if self.summarized:
            parts.append(f"summarised {self.summarized} items")
        if self.cleared:
            parts.append(f"cleared {len(self.cleared)} tool outputs")
        if self.thoughts:
            parts.append(f"cleared {self.thoughts} reasoning steps")
        if self.clipped:
            parts.append(f"clipped {len(self.clipped)} tool outputs")
        done = ", ".join(parts) or "nothing to compact"
        return f"Compacted {compact_tokens(self.before)} → {compact_tokens(self.after)} tokens: {done}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reason": self.reason,
            "tokens_before": self.before,
            "tokens_after": self.after,
            "thoughts": self.thoughts,
            "cleared": len(self.cleared),
            "clipped": len(self.clipped),
            "summarized": self.summarized,
        }


@dataclass
class ContextState:
    """The decisions compaction made over one growing list of input items.

    They are sticky: every later call applies them before deciding anything
    new, so the model input only ever changes at a compaction and the prefix
    a provider caches holds between compactions.
    """

    #: Stands for ``items[:cut]``; ``items[cut:]`` are sent (masked).
    summary: str = ""
    cut: int = 0
    #: Digest of ``items[cut]``, to find the cut again in a list that moved.
    anchor: tuple[int, int] | None = None
    #: The request in flight, kept verbatim once the cut has passed it.
    pinned: str = ""
    #: Tool outputs replaced: by call id, the stub or the clipped text.
    cleared: dict[str, str] = field(default_factory=dict)
    clipped: dict[str, str] = field(default_factory=dict)
    #: Digests of the reasoning items cleared.
    thoughts: set[tuple[int, int]] = field(default_factory=set)
    #: Branches folded (`branch` / `return`): the call id of each `return`,
    #: to the call id of the `branch` it closed. The steps between the two
    #: are hidden (see `Compactor.hidden`).
    folds: dict[str, str] = field(default_factory=dict)

    def text(self) -> str:
        """The summary as shown to the model, the pinned request included."""
        if not self.pinned:
            return self.summary
        return f"{self.summary.strip()}\n\n{PINNED_HEADER}\n\n{self.pinned.strip()}".strip()


class Compactor:
    """Fits a list of model input items into a `ContextBudget`, in tiers.

    Past the high watermark (or when forced), cheapest first and only as far
    as the low watermark needs:

    1. reasoning before the newest response is cleared (no model call)
    2. tool outputs are cleared, oldest first, sparing the newest `keep`
    3. the oldest prefix is folded into the summary by `summarize`, which is
       handed the previous summary and the segment (anchored and incremental:
       a summary is never rebuilt from scratch); the cut is the smallest safe
       one that fits, and the newest response is always kept
    4. what still does not fit the usable budget — a giant output in the
       newest response — is clipped, head and tail

    A failing summarizer costs the quality of the summary, never the run: the
    segment is then kept as an extract of its requests and calls.
    """

    #: Smallest tool output, in tokens, worth clearing (a stub costs ~40).
    CLEAR_MIN = 200
    #: Smallest a clipped output is cut to, in tokens, and what the note of
    #: the cut and the framing of the item cost on top.
    CLIP_MIN = 500
    CLIP_MARGIN = 16
    #: How much of an output, of call arguments and of reasoning a summarizer
    #: is shown, in characters, head and tail.
    RENDER_OUTPUT = 2000
    RENDER_ARGS = 600
    RENDER_THOUGHT = 600

    def __init__(
        self,
        counter: TokenCounter,
        budget: ContextBudget,
        *,
        summarize: Callable[[str, str, str], Awaitable[str]] | None = None,
        keep: int = 4,
        summary_tokens: int = 4096,
        segment_limit: int = 0,
        reasoning: bool = True,
    ) -> None:
        self.counter = counter
        self.budget = budget
        #: ``(previous summary, segment, focus) -> summary``; ``None`` drops.
        self.summarize = summarize
        self.keep = max(int(keep), 0)
        self.summary_tokens = max(int(summary_tokens), 256)
        #: Largest segment one summarizer call is handed, in tokens (0: any).
        self.segment_limit = max(int(segment_limit), 0)
        #: Whether reasoning items reach the model (see `reasoning_replayed`).
        self.reasoning = reasoning

    # -- applying the decisions --------------------------------------------

    def locate(self, items: list[Any], state: ContextState) -> int:
        """Where the cut of `state` falls in `items` (they only ever grow)."""
        cut = min(state.cut, len(items))
        if state.anchor is None or (cut < len(items) and self.counter.key(items[cut]) == state.anchor):
            return cut
        found = next(
            (i for i, item in enumerate(items) if self.counter.key(item) == state.anchor), None
        )
        return cut if found is None else found

    def masked(self, item: Any, state: ContextState) -> Any:
        """An item as it is sent, the decisions of `state` applied."""
        kind = item_kind(item)
        if is_output(kind):
            call_id = str(item.get("call_id") or "")
            replaced = state.cleared.get(call_id) or state.clipped.get(call_id)
            return {**item, "output": replaced} if replaced is not None else item
        if kind == "reasoning" and state.thoughts and self.counter.key(item) in state.thoughts:
            masked = {**item, "summary": [{"type": "summary_text", "text": THOUGHT_STUB}], "content": []}
            masked.pop("encrypted_content", None)
            return masked
        return item

    def cost(self, item: Any, state: ContextState) -> int:
        """Tokens an item costs as sent (uncalibrated)."""
        if item_kind(item) == "reasoning" and not self.reasoning:
            return 0
        return self.counter.item(self.masked(item, state))

    def head_cost(self, state: ContextState) -> int:
        text = state.text()
        return self.counter.item(summary_message(text)) if text else 0

    def hidden(self, items: list[Any], state: ContextState) -> set[int]:
        """Indices of the items folded away by the branches of `state`.

        From the first safe cut after the output of a `branch` call up to the
        response that called `return` on it: the prefix up to the branch stays
        as the provider cached it, and the `return` call, which carries the
        summary, stands for what is hidden. A fold whose calls are not in
        `items` hides nothing.
        """
        if not state.folds:
            return set()
        outputs: dict[str, int] = {}
        calls: dict[str, int] = {}
        for index, item in enumerate(items):
            kind = item_kind(item)
            if is_output(kind):
                outputs[str(item.get("call_id") or "")] = index
            elif is_call(kind):
                calls[str(item.get("call_id") or "")] = index
        cuts = safe_cuts(items)
        hidden: set[int] = set()
        for back, branch in state.folds.items():
            opened, closing = outputs.get(branch), calls.get(back)
            if opened is None or closing is None or closing <= opened:
                continue
            start = next((cut for cut in cuts if cut > opened), None)
            end = max((cut for cut in cuts if cut <= closing), default=None)
            if start is not None and end is not None and end > start:
                hidden.update(range(start, end))
        return hidden

    def view(self, items: list[Any], state: ContextState) -> list[Any]:
        """The input sent: the summary, then the kept items, masked, the
        folded ones left out."""
        cut = self.locate(items, state)
        hidden = self.hidden(items, state)
        head = [summary_message(state.text())] if state.text() else []
        return head + [
            self.masked(item, state)
            for index, item in enumerate(items[cut:], cut)
            if index not in hidden
        ]

    def tokens(self, items: list[Any], state: ContextState, fixed: int = 0) -> int:
        """Uncalibrated tokens of the input `view` would send."""
        cut = self.locate(items, state)
        hidden = self.hidden(items, state)
        return fixed + self.head_cost(state) + sum(
            self.cost(item, state)
            for index, item in enumerate(items[cut:], cut)
            if index not in hidden
        )

    # -- deciding ------------------------------------------------------------

    async def compact(
        self,
        items: list[Any],
        state: ContextState,
        *,
        fixed: int = 0,
        force: bool = False,
        reason: str = "",
        focus: str = "",
        pinned_at: int = -1,
        pinned: str = "",
        on_summary: Callable[[Compaction], Awaitable[None]] | None = None,
    ) -> Compaction | None:
        """Compact `items` into `state` when past the high watermark.

        `fixed` is what the input costs besides the items (instructions, tool
        schemas). `force` compacts whatever the size: every tier, and the
        summary reaches the newest response. `pinned` is the request in
        flight, at `pinned_at`, kept verbatim once the cut passes it.
        `on_summary` is awaited before the summarizer is called, which is the
        only slow tier. Returns what was done, or ``None`` when nothing was
        needed.
        """
        state.cut = self.locate(items, state)
        hidden = self.hidden(items, state)  # folded: never sent, never compacted
        costs = [0 if index in hidden else self.cost(item, state) for index, item in enumerate(items)]
        current = fixed + self.head_cost(state) + sum(costs[state.cut:])
        high = self.counter.raw(self.budget.high)
        goal = self.counter.raw(self.budget.low)
        if not force and current <= high:
            return None
        report = Compaction(
            reason=reason or ("forced" if force else "trigger"),
            before=self.counter.scaled(current),
        )
        cuts = safe_cuts(items)
        latest = max([state.cut, *cuts])

        def settle(index: int) -> None:
            nonlocal current
            cost = 0 if index in hidden else self.cost(items[index], state)
            current -= costs[index] - cost
            costs[index] = cost

        # 1. reasoning before the newest response
        if self.reasoning:
            for index in range(state.cut, latest):
                if index not in hidden and item_kind(items[index]) == "reasoning":
                    key = self.counter.key(items[index])
                    if key not in state.thoughts:
                        state.thoughts.add(key)
                        report.thoughts += 1
                        settle(index)
        # 2. tool outputs, oldest first, the newest `keep` spared
        outputs = [
            i for i in range(state.cut, len(items))
            if i not in hidden and is_output(item_kind(items[i]))
        ]
        spared = set(outputs[-self.keep:]) if self.keep else set()
        for index in outputs:
            if current <= goal and not force:
                break
            call_id = str(items[index].get("call_id") or "")
            if index in spared or not call_id or call_id in state.cleared:
                continue
            if costs[index] < self.CLEAR_MIN:
                continue
            state.cleared[call_id] = self.stub(items, index, costs[index])
            report.cleared.append(call_id)
            settle(index)
        # 3. the oldest prefix into the summary, when it is worth a model call
        if current > goal or force:
            room = goal - fixed - self.summary_tokens
            cut = latest if force else self.choose(cuts, costs, state.cut, latest, room)
            worth = min(self.summary_tokens, goal // 4)
            if cut > state.cut and (force or sum(costs[state.cut:cut]) >= worth):
                if on_summary is not None:
                    await on_summary(report)
                segment = [items[i] for i in range(state.cut, cut) if i not in hidden]
                state.summary = await self.fold(state.summary, segment, focus, items)
                if pinned and 0 <= pinned_at < cut:
                    state.pinned = self.bounded(pinned)
                report.summarized = cut - state.cut
                report.summary = state.text()
                report.cut = state.cut = cut
                state.anchor = self.counter.key(items[cut]) if cut < len(items) else None
                current = fixed + self.head_cost(state) + sum(costs[cut:])
        report.cut = state.cut
        # 4. clip what still cannot be sent at all
        usable = self.counter.raw(self.budget.usable)
        if current > usable:
            tail = [
                i
                for i in range(state.cut, len(items))
                if i not in hidden and is_output(item_kind(items[i])) and items[i].get("call_id")
            ]
            for index in sorted(tail, key=lambda i: -costs[i]):
                if current <= usable:
                    break
                keep = max(costs[index] - (current - usable) - self.CLIP_MARGIN, self.CLIP_MIN)
                if keep >= costs[index]:
                    continue
                text = content_text(self.masked(items[index], state).get("output"))
                keep -= self.counter.text(CLIP_NOTE)
                chars = max(int(len(text) * keep / max(costs[index], 1)), 200)
                call_id = str(items[index]["call_id"])
                state.cleared.pop(call_id, None)
                state.clipped[call_id] = f"{CLIP_NOTE}\n{clip_output(text, chars)}"
                report.clipped.append(call_id)
                settle(index)
        report.after = self.counter.scaled(current)
        if not (report.thoughts or report.cleared or report.clipped or report.summarized):
            return None
        return report

    def choose(self, cuts: list[int], costs: list[int], start: int, latest: int, room: int) -> int:
        """The smallest safe cut after `start` whose tail fits in `room`.

        Keeping as much as fits makes compactions rare; when nothing fits the
        newest response alone is kept.
        """
        candidates = [c for c in cuts if start < c <= latest]
        if not candidates:
            return start
        tail = sum(costs[candidates[0]:])
        previous = candidates[0]
        for cut in candidates:
            tail -= sum(costs[previous:cut])
            previous = cut
            if tail <= room:
                return cut
        return candidates[-1]

    def stub(self, items: list[Any], index: int, tokens: int) -> str:
        """What a cleared output is replaced with: what it was, and how to get it back."""
        call_id = items[index].get("call_id")
        name = next(
            (
                str(item.get("name") or "tool")
                for item in reversed(items[:index])
                if is_call(item_kind(item)) and item.get("call_id") == call_id
            ),
            "tool",
        )
        size = self.counter.scaled(tokens)
        if size > self.budget.usable // 4:
            # Called again whole it would only be cleared or clipped again.
            return (
                f"[{name} output cleared to save context: {size:,} tokens, too large "
                "to read again whole. Ask for only the part you need (a line range, "
                "a filter).]"
            )
        return (
            f"[{name} output cleared to save context: {size:,} "
            "tokens. Call the tool again if you need it.]"
        )

    # -- summarising ---------------------------------------------------------

    async def fold(
        self, previous: str, segment: list[Any], focus: str = "", items: list[Any] | None = None
    ) -> str:
        """Fold `segment` into `previous`, a chunk per summarizer call."""
        names = {
            item.get("call_id"): str(item.get("name") or "tool")
            for item in items or segment
            if is_call(item_kind(item))
        }
        lines = [line for line in (self.render(item, names) for item in segment) if line]
        if self.summarize is None:
            note = f"_{len(segment)} earlier items were dropped without a summary._"
            return self.bounded(f"{previous.strip()}\n\n{note}".strip())
        summary = previous
        for chunk in self.chunks(lines):
            text = ""
            try:
                text = (await self.summarize(summary, "\n\n".join(chunk), focus)).strip()
            except Exception as exc:  # the summary is lost, never the run
                print(f"[context] summarizer failed: {exc!r}", file=sys.stderr)
            summary = text or self.extract(summary, chunk)
        return summary

    def render(self, item: Any, names: dict[Any, str] | None = None) -> str:
        """One item as a line of the transcript a summarizer reads."""
        if not isinstance(item, dict):
            return ""
        kind = item_kind(item)
        if kind == "reasoning":
            text = clip_output(item_text(item), self.RENDER_THOUGHT).strip()
            return f"ASSISTANT (thinking): {text}" if text and text != THOUGHT_STUB else ""
        if is_call(kind):
            args = item.get("arguments") if "arguments" in item else item.get("input")
            return f"TOOL CALL {item.get('name') or kind}: {clip_output(content_text(args), self.RENDER_ARGS)}"
        if is_output(kind):
            name = (names or {}).get(item.get("call_id"), "tool")
            output = clip_output(content_text(item.get("output")), self.RENDER_OUTPUT)
            return f"TOOL RESULT ({name}): {output}"
        text = item_text(item).strip()
        if not text or text.startswith(SUMMARY_HEADER):
            return ""  # the previous summary is handed over on its own
        return f"{kind.upper()}: {text}"

    def chunks(self, lines: list[str]) -> list[list[str]]:
        """`lines` grouped so no group exceeds `segment_limit` tokens."""
        if not self.segment_limit:
            return [lines] if lines else []
        groups: list[list[str]] = []
        group: list[str] = []
        size = 0
        for line in lines:
            tokens = self.counter.text(line)
            if tokens > self.segment_limit:
                line = clip_output(line, self.segment_limit * TokenCounter.BYTES_PER_TOKEN)
                tokens = self.segment_limit
            if group and size + tokens > self.segment_limit:
                groups.append(group)
                group, size = [], 0
            group.append(line)
            size += tokens
        if group:
            groups.append(group)
        return groups

    def extract(self, previous: str, lines: list[str]) -> str:
        """A summary without a summarizer: the requests and calls of a segment."""
        kept = [line for line in lines if line.startswith(("USER:", "TOOL CALL"))]
        answers = [line for line in lines if line.startswith("ASSISTANT:")]
        kept += answers[-1:]
        note = "\n".join(f"- {clip_output(line, 300)}" for line in kept)
        merged = f"{previous.strip()}\n\n## Earlier segment (no summary)\n\n{note}".strip()
        return self.bounded(merged)

    def bounded(self, text: str) -> str:
        """A summary held to `summary_tokens`, head and tail."""
        return clip_output(text, self.summary_tokens * TokenCounter.BYTES_PER_TOKEN)


#: What a provider says when the input does not fit its window.
OVERFLOW_PATTERN = re.compile(
    r"context[_ ](?:length|window)|maximum context|prompt is too long|too many tokens"
    r"|reduce the length|input is too long|exceeds? the (?:model'?s? )?(?:maximum|max)",
    re.IGNORECASE,
)

#: Where a provider says how large its window is.
WINDOW_PATTERNS = (
    re.compile(r"maximum context length is (\d[\d,]*)", re.IGNORECASE),
    re.compile(r"(\d[\d,]*) tokens? > (\d[\d,]*)", re.IGNORECASE),
    re.compile(r"context (?:length|window|size)\D{0,40}?(\d[\d,]{3,})", re.IGNORECASE),
)


def is_overflow(exc: BaseException) -> bool:
    """A failure that says the input does not fit the model's window."""
    status = error_status(exc)
    if status is not None and status not in (400, 413, 422):
        return False
    text = f"{getattr(exc, 'code', '') or ''} {exc}"
    return bool(OVERFLOW_PATTERN.search(text))


def overflow_window(text: str) -> int:
    """The window a context length error names, or 0."""
    for pattern in WINDOW_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.groups()[-1].replace(",", ""))
    return 0


class ContextRun:
    """Compaction inside one run, as the SDK's `call_model_input_filter`.

    Before every model call the SDK hands the filter the whole input — what
    the run started with and every item generated since — and sends whatever
    it returns. The decisions of earlier calls are applied first (they are
    sticky), and a new compaction happens only past the high watermark, so a
    long run of tool calls is compacted as it goes, not only between runs.

    `publish` posts an event for the run's session; `on_compact` is awaited
    with every compaction and the items it was made over (the agent journals
    it). After a call `observe` folds the tokens the provider reported into
    the calibration.

    Each call is timed as well, against the model's `throughput`: from the
    input handed over to the first token (`first_token`), over the tokens the
    provider had to read again (all of them after a compaction, else what was
    added since the previous call). Once the model's read speed is known, the
    budget is capped to what it reads from scratch within `slice_stall` of the
    `stall` guard, so a slow model is compacted before a cold read of its
    context could stall. With `note`, every call ends with a short budget note
    for the model: the context in use, the slice one step may read and the
    open `plan`.
    """

    #: Recoveries from a context length error allowed per run.
    OVERFLOWS = 2
    #: The smallest context the time cap leaves (instructions and tools alone
    #: may take most of it).
    TIMED_FLOOR = 8192
    #: A cap that moves less than this share of the budget is left alone, so
    #: the watermarks do not wander with every timed call.
    RETIME_STEP = 0.1
    #: Tokens per second no model writes at: a reply that came faster arrived
    #: in one piece; so did one of `BURST_TOKENS` or more within `BURST` seconds.
    WRITE_LIMIT = 5000
    BURST = 0.05
    BURST_TOKENS = 8
    #: Seconds of quiet after which a local server may have unloaded the model.
    IDLE = 240.0
    #: Opens the note, so the model does not take it for the user.
    NOTE_HEADER = "[harness status, not from the user]"

    def __init__(
        self,
        compactor: Compactor,
        *,
        state: ContextState | None = None,
        publish: Callable[..., Awaitable[Any]] | None = None,
        block: Callable[[int], str] | None = None,
        pinned_at: int = -1,
        pinned: str = "",
        on_compact: Callable[[Compaction, list[Any]], Awaitable[None]] | None = None,
        on_window: Callable[[int], None] | None = None,
        throughput: Throughput | None = None,
        stall: float = 0.0,
        slice_share: float = 0.25,
        slice_stall: float = 0.0,
        plan: Callable[[], Any] | None = None,
        note: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.compactor = compactor
        self.state = state or ContextState()
        self.publish = publish
        #: The id of the marker block of the n-th compaction.
        self.block = block or (lambda n: f"compact-{uuid.uuid4().hex[:8]}")
        self.pinned_at = pinned_at
        self.pinned = pinned
        self.on_compact = on_compact
        self.on_window = on_window
        #: The window's own budget; `compactor.budget` is it under the time cap.
        self.base = compactor.budget
        self.throughput = throughput
        self.stall = max(float(stall), 0.0)
        self.slice_share = min(max(float(slice_share), 0.01), 1.0)
        self.slice_stall = max(float(slice_stall), 0.0)
        self.plan = plan
        self.note = note
        self.clock = clock
        #: Tokens of the instructions and tool schemas, counted once.
        self.fixed: int | None = None
        #: The items of the newest call, before and after compaction.
        self.items: list[Any] = []
        self.sent: list[Any] = []
        #: Uncalibrated tokens of the newest input sent.
        self.estimate = 0
        self.compactions: list[Compaction] = []
        self.overflows = 0
        #: Timing of the newest call: when its input was handed over, when its
        #: first token arrived, whether its prefix was rebuilt (a compaction:
        #: the provider reads it all again), the input the provider reported
        #: for the call before and the note tokens that call ended with.
        self.called_at: float | None = None
        self.first_at: float | None = None
        self.rebuilt = False
        self.reported = 0
        self.noted = 0
        self.noted_before = 0
        self._lock = asyncio.Lock()
        # a model already timed by an earlier run is capped from the first call
        self.compactor.budget = self.base.capped(self.timed())

    @property
    def counter(self) -> TokenCounter:
        return self.compactor.counter

    @property
    def budget(self) -> ContextBudget:
        return self.compactor.budget

    async def __call__(self, data: Any) -> Any:
        from agents.run_config import ModelInputData

        model_data = data.model_data
        if self.fixed is None:
            self.fixed = self.fixed_cost(
                model_data.instructions, getattr(data.agent, "tools", None) or []
            )
        sent = await self.fit(list(model_data.input))
        self.noted = 0
        if self.note:
            text = self.note_text()
            self.noted = self.counter.text(text) + TokenCounter.MESSAGE_TOKENS
            sent = self.with_note(sent, text)
        self.called_at, self.first_at = self.clock(), None
        return ModelInputData(input=sent, instructions=model_data.instructions)

    def fixed_cost(self, instructions: str | None, tools: list[Any]) -> int:
        """Tokens of the instructions and of every tool schema."""
        count = self.counter.text(instructions or "") + TokenCounter.MESSAGE_TOKENS
        for tool in tools:
            schema = getattr(tool, "params_json_schema", None) or {}
            count += TokenCounter.MESSAGE_TOKENS + self.counter.text(
                f"{getattr(tool, 'name', '')} {getattr(tool, 'description', '') or ''} "
                f"{json_dump(schema)}"
            )
        return count

    async def fit(
        self, items: list[Any], *, force: bool = False, reason: str = "", focus: str = ""
    ) -> list[Any]:
        """The input to send for `items`: compacted when it must be."""
        async with self._lock:
            n = len(self.compactions)
            started = False

            async def on_summary(report: Compaction) -> None:
                nonlocal started
                started = True
                await self.post(
                    EventType.BLOCK_START, id=self.block(n), kind="compact", role="system", text=""
                )

            report = await self.compactor.compact(
                items,
                self.state,
                fixed=self.fixed or 0,
                force=force,
                reason=reason,
                focus=focus,
                pinned_at=self.pinned_at,
                pinned=self.pinned,
                on_summary=on_summary,
            )
            self.items = items
            self.sent = self.compactor.view(items, self.state)
            self.estimate = self.compactor.tokens(items, self.state, self.fixed or 0)
            if report is not None:
                self.rebuilt = True
                report.id = self.block(n)
                self.compactions.append(report)
                await self.announce(report, started)
                if self.on_compact is not None:
                    await self.on_compact(report, items)
            await self.post(EventType.CONTEXT_USAGE, **self.usage(self.counter.scaled(self.estimate)))
            return self.sent

    async def announce(self, report: Compaction, started: bool) -> None:
        """Publish a compaction: its marker block and the `context.compact` event."""
        data = report.to_dict()
        if not started:
            await self.post(
                EventType.BLOCK_START, id=report.id, kind="compact", role="system", text=""
            )
        await self.post(
            EventType.BLOCK_END,
            id=report.id,
            kind="compact",
            text=report.summary or report.describe(),
            message=report.describe(),
            **{k: v for k, v in data.items() if k != "id"},
        )
        await self.post(EventType.CONTEXT_COMPACT, message=report.describe(), **data)

    async def post(self, event_type: str, **data: Any) -> None:
        if self.publish is not None:
            await self.publish(event_type, **data)

    def observe(
        self, input_tokens: int, output_tokens: int = 0, cached_tokens: int = 0
    ) -> dict[str, Any] | None:
        """The provider counted the input of the newest call: calibrate on it.

        The call is timed too (see `time_call`). Returns the `context.usage`
        payload to publish, or ``None`` when nothing was reported.
        """
        if input_tokens <= 0:
            return None
        self.counter.calibrate(self.estimate, input_tokens)
        self.time_call(input_tokens, output_tokens, cached_tokens)
        return self.usage(input_tokens, estimated=False)

    def first_token(self) -> None:
        """The first token of the newest call arrived."""
        if self.called_at is not None and self.first_at is None:
            self.first_at = self.clock()

    def time_call(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> None:
        """Fold the timing of the newest call into the model's throughput.

        What the provider read is the input less what it reports it had cached;
        where it reports nothing, the whole input after a compaction, else what
        the input grew by since the call before (whose trailing note it read
        again). The first call of a run, over a prefix the provider may or may
        not still hold, is not timed, nor is a call after `IDLE` seconds of
        quiet (a local server unloads an idle model and reloads it first). A
        reply whose tokens all came within `BURST` of the first (or faster than
        `WRITE_LIMIT`) arrived in one piece, as some servers send a tool call:
        its wait timed the writing too, so it is left out.
        """
        if cached_tokens > 0:
            read = input_tokens - cached_tokens
        elif self.rebuilt:
            read = input_tokens
        elif self.reported:
            read = input_tokens - (self.reported - self.counter.scaled(self.noted_before))
        else:
            read = 0
        self.reported, self.noted_before, self.rebuilt = input_tokens, self.noted, False
        speed = self.throughput
        if speed is None:
            return
        speed.wrote(output_tokens)  # a reply sent whole is not timed, but sizes the cap
        if self.called_at is not None and self.first_at is not None:
            now = self.clock()
            idle = speed.last is not None and self.called_at - speed.last > self.IDLE
            speed.last = now
            first = self.first_at - self.called_at
            writing = now - self.first_at
            if not idle and not (
                output_tokens >= self.BURST_TOKENS
                and writing < max(self.BURST, output_tokens / self.WRITE_LIMIT)
            ):
                speed.observe(read, first, output_tokens, writing)
        self.retime()

    def timed(self) -> int:
        """Input tokens the model reads from scratch within its share of the
        stall guard, leaving time to write its longest recent reply (at most
        the reserve), or 0 while unmeasured (or the cap is off)."""
        if self.throughput is None or self.stall <= 0 or self.slice_stall <= 0:
            return 0
        tokens = self.throughput.readable(self.stall * self.slice_stall, self.base.reserve)
        return max(tokens, self.TIMED_FLOOR) if tokens else 0

    def retime(self) -> None:
        """Cap the budget to what the model reads in time, when that moved."""
        capped = self.base.capped(self.timed())
        current = self.compactor.budget
        if capped == current:
            return
        if abs(capped.usable - current.usable) >= current.usable * self.RETIME_STEP:
            self.compactor.budget = capped

    def usage(self, tokens: int, *, estimated: bool = True) -> dict[str, Any]:
        """The `context.usage` payload: the budget's, the slice one step may
        read and, once measured, the read and write speeds of the model."""
        payload = self.budget.usage(tokens, estimated=estimated)
        payload["slice"] = self.limits().slice
        speed = self.throughput
        if speed is not None and speed.prefill > 0:
            payload["prefill"] = round(speed.prefill, 1)
        if speed is not None and speed.decode > 0:
            payload["decode"] = round(speed.decode, 1)
        return payload

    def limits(self) -> SliceBudget:
        """The slice budget of the run now."""
        return SliceBudget(
            self.base.usable,
            self.timed(),
            self.slice_share,
            self.counter.scaled(self.estimate),
        )

    def note_text(self) -> str:
        """The budget note the newest call ends with."""
        limits = self.limits()
        line = (
            f"[budget] context {compact_tokens(limits.used)} of "
            f"{compact_tokens(limits.limit)} tokens; one step may read up to "
            f"{compact_tokens(limits.slice)}"
        )
        speed = self.throughput
        if speed is not None and speed.prefill > 0:
            line += f"; the model reads ~{speed.prefill:.0f}"
            line += f" and writes ~{speed.decode:.0f} tokens/s" if speed.decode > 0 else " tokens/s"
        lines = [self.NOTE_HEADER, line]
        plan = self.plan() if self.plan is not None else None
        if plan is not None and plan.steps:
            lines.append(plan.brief())
        return "\n".join(lines)

    @staticmethod
    def with_note(items: list[Any], note: str) -> list[Any]:
        """`items` ending with `note`: folded into a closing user message (no
        two user messages in a row), else as a message of its own."""
        last = items[-1] if items else None
        if (
            isinstance(last, dict)
            and last.get("role") == "user"
            and isinstance(last.get("content"), str)
        ):
            return [*items[:-1], {**last, "content": f"{last['content']}\n\n{note}"}]
        return [*items, {"role": "user", "content": note}]

    async def overflowed(self, exc: BaseException) -> list[Any] | None:
        """The input did not fit after all: the input to retry with, or ``None``.

        The window the error names (else three quarters of the assumed one)
        replaces the budget, and the context is compacted as hard as it goes.
        """
        if self.overflows >= self.OVERFLOWS or not self.items or not is_overflow(exc):
            return None
        self.overflows += 1
        budget = self.budget
        window = overflow_window(str(exc)) or int(budget.window * 0.75)
        self.base = ContextBudget.of(
            min(window, self.base.window), budget.reserve, budget.trigger, budget.target
        )
        self.compactor.budget = self.base.capped(self.timed())
        if self.on_window is not None:
            self.on_window(self.base.window)
        retry = await self.fit(self.items, force=True, reason="overflow")
        self.noted = 0
        self.called_at, self.first_at = self.clock(), None
        return retry


@functools.cache
def context_guard_class() -> type:
    """The SDK model wrapper that recovers from a context length error.

    Built on first use, since the SDK is imported lazily.
    """
    from agents.models.interface import Model

    class ContextGuard(Model):
        """Wraps the leader's model: a context length error compacts and retries."""

        def __init__(self, model: Any, context: ContextRun) -> None:
            self.model = model
            self.context = context

        def __getattr__(self, name: str) -> Any:
            return getattr(self.model, name)

        async def get_response(self, system_instructions: Any, input: Any, *args: Any, **kwargs: Any) -> Any:
            while True:
                try:
                    return await self.model.get_response(system_instructions, input, *args, **kwargs)
                except Exception as exc:
                    smaller = await self.context.overflowed(exc)
                    if smaller is None:
                        raise
                    input = smaller

        async def stream_response(
            self, system_instructions: Any, input: Any, *args: Any, **kwargs: Any
        ) -> AsyncIterator[Any]:
            while True:
                started = False
                try:
                    async for event in self.model.stream_response(
                        system_instructions, input, *args, **kwargs
                    ):
                        started = True
                        yield event
                    return
                except Exception as exc:
                    smaller = None if started else await self.context.overflowed(exc)
                    if smaller is None:
                        raise
                    input = smaller

        def get_retry_advice(self, request: Any) -> Any:
            return self.model.get_retry_advice(request)

        async def _cleanup_on_run_end(self, owner: object) -> None:
            await self.model._cleanup_on_run_end(owner)

    return ContextGuard


# Learned rules. A vote on an answer becomes a rule: the trace behind the
# answer goes to a model together with the rules that already exist, and it
# either adds a rule or edits the one covering the same behaviour. The rules
# are appended to the instructions of every run, like a standing system
# message, and a warning is raised once they outgrow their budget.

#: What a rule asks for: keep doing it, or stop.
RULE_POLARITIES = ("do", "avoid")
#: Votes and the polarity each one defaults to.
RULE_VOTES = {"up": "do", "down": "avoid"}


@dataclass
class Rule:
    """One learned rule."""

    id: str
    polarity: str
    text: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: How many votes shaped it: an edit is one more.
    votes: int = 1

    def line(self) -> str:
        return f"{'Do' if self.polarity == 'do' else 'Avoid'}: {self.text}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "polarity": self.polarity,
            "text": self.text,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "votes": self.votes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        polarity = str(data.get("polarity") or "do")
        return cls(
            id=str(data["id"]),
            polarity=polarity if polarity in RULE_POLARITIES else "do",
            text=str(data.get("text") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            votes=int(data.get("votes") or 1),
        )


class RuleBook:
    """Rules learned from feedback, persisted through a `Store`.

    `learn` is the whole loop: the trace and the existing rules go to a
    `distill` delegate (a model), its JSON answer adds or edits one rule, and
    learning is serialized so two votes in a row cannot both add what one of
    them should have edited. `decorate` appends the rules to instructions.
    """

    NAMESPACE = "rules"
    HEADING = "## Learned rules"
    #: Longest trace a distillation is shown: the prompt's head and the tail.
    TRACE_LIMIT = 12000

    def __init__(
        self, store: Store | None = None, *, enabled: bool = True, max_chars: int = 4000
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.max_chars = max_chars
        self._rules: dict[str, Rule] | None = None
        self._lock: asyncio.Lock | None = None

    # -- the book -----------------------------------------------------------

    def rules(self) -> list[Rule]:
        """Every rule, oldest first."""
        if self._rules is None:
            self._rules = {}
            for key, record in self.store.list(self.NAMESPACE) if self.store else []:
                with contextlib.suppress(AttributeError, KeyError, TypeError, ValueError):
                    self._rules[key] = Rule.from_dict(record)
        return sorted(self._rules.values(), key=lambda rule: rule.created_at)

    def get(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules() if r.id == str(rule_id).strip()), None)

    @property
    def size(self) -> int:
        """Characters the rules add to every run's instructions."""
        rendered = self.render()
        return len(rendered)

    def warning(self) -> str | None:
        """A warning once the rules outgrow `max_chars`, else ``None``."""
        size = self.size
        if self.max_chars <= 0 or size <= self.max_chars:
            return None
        return (
            f"Learned rules are {size:,} chars, over the {self.max_chars:,} budget: "
            f"every run pays for them. Merge or drop some with /rules."
        )

    def render(self) -> str:
        """The rules as a section of the instructions, or empty."""
        rules = self.rules() if self.enabled else []
        if not rules:
            return ""
        lines = "\n".join(f"- {rule.line()}" for rule in rules)
        return f"{self.HEADING}\n\nThe user taught you these through feedback:\n\n{lines}"

    def decorate(self, instructions: str) -> str:
        """`instructions` with the rules appended."""
        rendered = self.render()
        return f"{instructions.rstrip()}\n\n{rendered}" if rendered else instructions

    def save(self, rule: Rule) -> Rule:
        self.rules()
        assert self._rules is not None
        self._rules[rule.id] = rule
        if self.store is not None:
            self.store.set(self.NAMESPACE, rule.id, rule.to_dict())
        return rule

    def add(self, polarity: str, text: str) -> Rule:
        return self.save(Rule(f"r{uuid.uuid4().hex[:6]}", polarity, text.strip()))

    def edit(self, rule_id: str, polarity: str, text: str) -> Rule:
        rule = self.get(rule_id)
        if rule is None:
            raise KeyError(rule_id)
        rule.polarity, rule.text = polarity, text.strip()
        rule.votes += 1
        rule.updated_at = time.time()
        return self.save(rule)

    def drop(self, rule_id: str) -> bool:
        rule = self.get(rule_id)
        if rule is None:
            return False
        assert self._rules is not None
        self._rules.pop(rule.id, None)
        if self.store is not None:
            self.store.delete(self.NAMESPACE, rule.id)
        return True

    def clear(self) -> int:
        rules = self.rules()
        for rule in rules:
            self.drop(rule.id)
        return len(rules)

    # -- learning -----------------------------------------------------------

    def trace(self, turns: Iterable[Turn]) -> str:
        """The turns behind an answer as text, cut to `TRACE_LIMIT`."""
        labels = {
            "user": "USER",
            "assistant": "ASSISTANT",
            "tool": "TOOL",
            "system": "SUMMARY",
            "compact": "SUMMARY",
        }
        text = "\n\n".join(
            f"{labels.get(turn.role, turn.role.upper())}: {turn.text.strip()}" for turn in turns
        )
        if len(text) <= self.TRACE_LIMIT:
            return text
        head = self.TRACE_LIMIT // 4
        tail = self.TRACE_LIMIT - head
        return f"{text[:head]}\n[... trace cut ...]\n{text[-tail:]}"

    def messages(
        self, vote: str, turns: Iterable[Turn], note: str = "", instructions: str = ""
    ) -> list[dict[str, str]]:
        """The distillation request: the rules, the trace, the vote."""
        existing = "\n".join(f"- {r.id} ({r.polarity}): {r.text}" for r in self.rules())
        verdict = "LIKED" if vote == "up" else "DISLIKED"
        parts = [
            f"Existing rules:\n{existing or '(none)'}",
            f"Trace:\n{self.trace(turns)}",
            f"The user {verdict} the final ASSISTANT answer.",
        ]
        if note.strip():
            parts.append(f"The user said: {note.strip()}")
        return [
            {"role": "system", "content": instructions or DEFAULT_RULES_INSTRUCTIONS},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

    def parse(self, text: str, vote: str) -> dict[str, str]:
        """Read the model's decision: ``{action, id, polarity, rule}``.

        JSON is expected; a reply that is not is taken as the rule itself, so a
        model that ignores the format still teaches something. An edit of a
        rule that does not exist becomes an add.
        """
        default = RULE_VOTES.get(vote, "do")
        decision: dict[str, Any] | None = None
        match = re.search(r"\{.*\}", text or "", re.DOTALL)
        if match:
            with contextlib.suppress(ValueError):
                loaded = json.loads(match.group(0))
                decision = loaded if isinstance(loaded, dict) else None
        if decision is None:  # not JSON: the reply is the rule
            decision = {"rule": (text or "").strip().splitlines()[0][:300] if (text or "").strip() else ""}
        rule = str(decision.get("rule") or decision.get("text") or "").strip()
        if not rule:
            raise ValueError("the model proposed no rule")
        polarity = str(decision.get("polarity") or default).lower()
        rule_id = str(decision.get("id") or "").strip()
        action = "edit" if decision.get("action") == "edit" and self.get(rule_id) else "add"
        return {
            "action": action,
            "id": rule_id if action == "edit" else "",
            "polarity": polarity if polarity in RULE_POLARITIES else default,
            "rule": rule,
        }

    async def learn(
        self,
        vote: str,
        turns: Iterable[Turn],
        distill: Callable[[list[dict[str, str]]], Awaitable[str]],
        *,
        note: str = "",
        instructions: str = "",
    ) -> tuple[str, Rule]:
        """Distil one rule from a vote on the answer that ends `turns`.

        Returns ``("add" | "edit", rule)``; raises `ValueError` when the model
        proposed nothing and lets a failed model call through.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            reply = await distill(self.messages(vote, list(turns), note, instructions))
            decision = self.parse(reply, vote)
            if decision["action"] == "edit":
                rule = self.edit(decision["id"], decision["polarity"], decision["rule"])
            else:
                rule = self.add(decision["polarity"], decision["rule"])
            print(f"[rules] {decision['action']} {rule.id}: {rule.line()}", file=sys.stderr)
            return decision["action"], rule


# ---------------------------------------------------------------------------
# Repository
#
# A session may be backed by a git checkout of a default repository: it is
# cloned into the session workspace on a branch of its own, and the work done
# there is published by committing, pushing and opening a pull request.
# ---------------------------------------------------------------------------

#: ``git@host:owner/name(.git)`` and ``https://host/owner/name(.git)``.
SSH_URL_RE = re.compile(r"^(?:ssh://)?git@(?P<host>[^:/]+)[:/](?P<path>.+?)(?:\.git)?/?$")
HTTP_URL_RE = re.compile(r"^https?://(?:[^@/]+@)?(?P<host>[^/]+)/(?P<path>.+?)(?:\.git)?/?$")
SHORTHAND_RE = re.compile(r"^(?P<path>[\w.-]+/[\w.-]+?)(?:\.git)?$")

DEFAULT_GIT_HOST = "github.com"
GIT_TOKEN_USER = "x-access-token"


class RepoError(ValueError):
    """Raised when a git or forge operation fails."""


@dataclass(frozen=True)
class RepoSpec:
    """An addressable repository: where it lives and how to write to it."""

    url: str = ""
    branch: str = ""
    token: str = ""
    remote: str = "origin"
    branch_prefix: str = "agent"
    directory: str = ""
    author_name: str = "agent"
    author_email: str = f"agent@users.noreply.{DEFAULT_GIT_HOST}"
    depth: int = 1

    @classmethod
    def from_config(cls, config: "AgentConfig") -> "RepoSpec":
        return cls(
            url=(config.repo_url or "").strip(),
            branch=(config.repo_branch or "").strip(),
            token=config.repo_token or "",
            remote=config.repo_remote or "origin",
            branch_prefix=config.repo_branch_prefix or "agent",
            directory=(config.repo_dir or "").strip(),
            author_name=(config.repo_author_name or config.name or "agent").strip(),
            author_email=(
                config.repo_author_email
                or f"{safe_name(config.name or 'agent')}@users.noreply.{DEFAULT_GIT_HOST}"
            ).strip(),
            depth=max(0, int(config.repo_depth or 0)),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def local(self) -> bool:
        """True for a filesystem repository (a path or a ``file://`` URL)."""
        raw = self.url.strip()
        return raw.startswith(("file://", "/", "./", "../", "~"))

    @property
    def parts(self) -> tuple[str, str]:
        """Return ``(host, owner/name)`` for the configured URL.

        A filesystem repository has no host and its slug is its directory name.
        """
        raw = self.url.strip()
        if not raw:
            raise RepoError("no repository is configured")
        if self.local:
            path = raw[len("file://"):] if raw.startswith("file://") else raw
            return ("", path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
        for pattern, host in ((SSH_URL_RE, ""), (HTTP_URL_RE, ""), (SHORTHAND_RE, DEFAULT_GIT_HOST)):
            match = pattern.match(raw)
            if match:
                return (host or match.group("host"), match.group("path").strip("/"))
        raise RepoError(f"unrecognized repository URL: {raw}")

    @property
    def slug(self) -> str:
        """``owner/name``."""
        return self.parts[1]

    @property
    def name(self) -> str:
        return self.slug.rsplit("/", 1)[-1]

    @property
    def clone_url(self) -> str:
        """The URL git clones from, without any credential in it."""
        raw = self.url.strip()
        if self.local or raw.startswith(("http://", "https://", "git@", "ssh://")):
            return raw
        host, slug = self.parts
        return f"https://{host}/{slug}.git"

    @property
    def api_url(self) -> str:
        """Base URL of the GitHub REST API for the host of this repository."""
        host = self.parts[0]
        if not host:
            raise RepoError("a filesystem repository has no pull request API")
        if host in (DEFAULT_GIT_HOST, "www.github.com"):
            return "https://api.github.com"
        return f"https://{host}/api/v3"

    def authenticated_url(self) -> str:
        """The clone URL with the token embedded, for one-off git invocations.

        The token is never written to ``.git/config``: it is only ever passed on
        an argument list, and command output is masked before it is surfaced.
        """
        url = self.clone_url
        if not self.token or not url.startswith("https://"):
            return url
        return f"https://{GIT_TOKEN_USER}:{self.token}@{url[len('https://'):]}"

    #: Tokens shorter than this are not masked: a one or two character string
    #: would shred unrelated output without protecting anything real.
    MIN_MASKED = 8

    def mask(self, text: str) -> str:
        if not self.token or len(self.token) < self.MIN_MASKED:
            return text
        return text.replace(self.token, "***")

    def identity(self) -> list[str]:
        """``git -c`` arguments giving the commits an author of their own."""
        args = []
        if self.author_name:
            args += ["-c", f"user.name={self.author_name}"]
        if self.author_email:
            args += ["-c", f"user.email={self.author_email}"]
        return args


@dataclass
class Checkout:
    """A cloned repository inside a session workspace."""

    manager: "RepoManager"
    path: Path
    branch: str
    base: str
    url: str = ""

    @classmethod
    def from_meta(cls, manager: "RepoManager", meta: dict[str, Any]) -> "Checkout":
        return cls(
            manager=manager,
            path=Path(meta["path"]),
            branch=meta.get("branch", ""),
            base=meta.get("base", ""),
            url=meta.get("url", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "branch": self.branch,
            "base": self.base,
            "url": self.url,
        }

    async def status(self) -> str:
        return await self.manager.status(self.path)

    async def commit(self, message: str) -> str:
        return await self.manager.commit(self.path, message)

    async def push(self) -> str:
        return await self.manager.push(self.path, self.branch)

    async def pull_request(self, title: str, body: str = "") -> dict[str, Any]:
        return await self.manager.open_pull_request(
            title=title, body=body, head=self.branch, base=self.base
        )

    async def publish(self, message: str, title: str = "", body: str = "") -> dict[str, Any]:
        """Commit, push and open a pull request in one step."""
        commit = await self.commit(message)
        push = await self.push()
        pull = await self.pull_request(title or message, body)
        return {"commit": commit, "push": push, "pull_request": pull}


class RepoManager:
    """Clones the configured repository and publishes the work done in it.

    `spec` is replaceable at runtime (the ``/repo`` command does exactly that),
    so a manager is never bound to one repository for its lifetime.
    """

    def __init__(self, spec: RepoSpec | None = None, *, timeout: int = 300) -> None:
        self.spec = spec or RepoSpec()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return self.spec.configured

    def branch_name(self, session_id: str) -> str:
        prefix = safe_name(self.spec.branch_prefix or "agent").strip("-") or "agent"
        return f"{prefix}/{safe_name(session_id)}"

    # -- git ----------------------------------------------------------------

    async def git(self, *args: str, cwd: Path | None = None) -> str:
        """Run a git command, returning its output with secrets masked."""
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "GCM_INTERACTIVE": "never",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(cwd) if cwd else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except OSError as exc:
            raise RepoError(f"git is not available: {exc}") from exc
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise RepoError(f"git timed out after {self.timeout}s") from None
        output = self.spec.mask(stdout.decode("utf-8", errors="replace")).strip()
        if process.returncode:
            raise RepoError(output or f"git failed with exit code {process.returncode}")
        return output

    async def clone(self, workspace: Path | str, *, session_id: str) -> Checkout:
        """Clone the repository into `workspace` and branch off for the session."""
        if not self.configured:
            raise RepoError("no repository is configured")
        root = Path(workspace).expanduser().resolve()
        target = root / (self.spec.directory or self.spec.name)
        if target.exists() and any(target.iterdir()):
            raise RepoError(f"clone target is not empty: {target.name}")
        args = ["clone"]
        if self.spec.depth:
            args += ["--depth", str(self.spec.depth)]
        if self.spec.branch:
            args += ["--branch", self.spec.branch]
        args += ["--origin", self.spec.remote, self.spec.authenticated_url(), str(target)]
        await self.git(*args, cwd=root)
        # Keep the credential out of the stored remote; it is supplied per push.
        await self.git(
            "remote", "set-url", self.spec.remote, self.spec.clone_url, cwd=target
        )
        base = self.spec.branch or await self.git(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=target
        )
        branch = self.branch_name(session_id)
        await self.git("checkout", "-b", branch, cwd=target)
        return Checkout(
            manager=self, path=target, branch=branch, base=base, url=self.spec.clone_url
        )

    async def status(self, path: Path) -> str:
        return await self.git("status", "--short", "--branch", cwd=path) or "(clean)"

    async def commit(self, path: Path, message: str) -> str:
        message = (message or "").strip()
        if not message:
            raise RepoError("a commit message is required")
        await self.git("add", "--all", cwd=path)
        staged = await self.git("diff", "--cached", "--name-only", cwd=path)
        if not staged:
            raise RepoError("there is nothing to commit")
        await self.git(*self.spec.identity(), "commit", "--message", message, cwd=path)
        return await self.git("log", "--oneline", "--max-count", "1", cwd=path)

    async def push(self, path: Path, branch: str) -> str:
        """Push `branch`, passing the credential on the command line only."""
        return (
            await self.git(
                "push",
                "--set-upstream",
                self.spec.authenticated_url(),
                f"HEAD:refs/heads/{branch}",
                cwd=path,
            )
            or f"Pushed {branch}"
        )

    async def open_pull_request(
        self, *, title: str, body: str = "", head: str, base: str = ""
    ) -> dict[str, Any]:
        """Open a pull request for `head` against `base` on the forge."""
        title = (title or "").strip()
        if not title:
            raise RepoError("a pull request title is required")
        if not self.spec.token:
            raise RepoError("a repository token is required to open a pull request")
        base = (base or self.spec.branch or "").strip()
        if not base:
            raise RepoError("a base branch is required to open a pull request")
        payload = {"title": title, "body": body or "", "head": head, "base": base}
        url = f"{self.spec.api_url}/repos/{self.spec.slug}/pulls"
        data = await asyncio.to_thread(self._post, url, payload)
        return {
            "number": data.get("number"),
            "url": data.get("html_url", ""),
            "title": data.get("title", title),
            "state": data.get("state", ""),
        }

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to the forge API. Overridable; keeps IO in one place."""
        import urllib.error
        import urllib.request

        if not url.startswith("https://"):
            raise RepoError(f"refusing to call a non HTTPS API: {url}")
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + self.spec.token,
                "Content-Type": "application/json",
                "User-Agent": "agent-harness",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:  # forge said no: surface why
            detail = exc.read().decode("utf-8", errors="replace")
            raise RepoError(self.spec.mask(f"{exc.code} {exc.reason}: {detail}")) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RepoError(self.spec.mask(str(exc))) from None


# ---------------------------------------------------------------------------
# Resilience
#
# Every call to a provider is bounded: one timeout per attempt, a few retries
# with exponential backoff and jitter on the failures another attempt can get
# past, and a taxonomy (`ErrorKind`) that tells a consumer which kind of failure
# it got instead of a string it has to parse. Token accounting rides the same
# path, because what a run spent is only knowable where its calls are made.
# ---------------------------------------------------------------------------


class ErrorKind:
    """Why a run failed, as reported on `RunResult.error_kind`."""

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    CONNECTION = "connection"
    SERVER = "server"
    AUTH = "auth"
    INVALID = "invalid_request"
    MAX_TURNS = "max_turns"
    #: The input did not fit the model's window, even compacted.
    CONTEXT = "context_overflow"
    CANCELLED = "cancelled"
    INTERNAL = "internal"

    #: The kinds an identical attempt may get past. Everything else is a
    #: decision of the provider (or a defect here) and is reported at once.
    TRANSIENT = (TIMEOUT, RATE_LIMIT, CONNECTION, SERVER)


#: Exception class names mapped onto the taxonomy. Matching by name keeps the
#: harness from importing the exception hierarchy of any provider SDK; a class
#: that is not listed still classifies by the HTTP status it carries.
ERROR_KINDS = {
    "APITimeoutError": ErrorKind.TIMEOUT,
    "TimeoutError": ErrorKind.TIMEOUT,
    "ReadTimeout": ErrorKind.TIMEOUT,
    "ConnectTimeout": ErrorKind.TIMEOUT,
    "APIConnectionError": ErrorKind.CONNECTION,
    "ConnectionError": ErrorKind.CONNECTION,
    "ConnectError": ErrorKind.CONNECTION,
    "RateLimitError": ErrorKind.RATE_LIMIT,
    "InternalServerError": ErrorKind.SERVER,
    "AuthenticationError": ErrorKind.AUTH,
    "PermissionDeniedError": ErrorKind.AUTH,
    "BadRequestError": ErrorKind.INVALID,
    "NotFoundError": ErrorKind.INVALID,
    "UnprocessableEntityError": ErrorKind.INVALID,
    "MaxTurnsExceeded": ErrorKind.MAX_TURNS,
}


class ModelError(RuntimeError):
    """A model call that failed, carrying its kind, role and attempt count."""

    def __init__(
        self,
        message: str,
        kind: str = ErrorKind.INTERNAL,
        *,
        role: str = LEADER_ROLE,
        attempts: int = 1,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.role = role
        self.attempts = attempts
        #: What the provider said, without the framing of the message above.
        self.detail = detail or message


def status_kind(status: int) -> str | None:
    """The taxonomy entry of an HTTP status, or ``None`` when it says nothing."""
    if status in (408, 504):
        return ErrorKind.TIMEOUT
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if status in (401, 403):
        return ErrorKind.AUTH
    if 500 <= status < 600:
        return ErrorKind.SERVER
    if 400 <= status < 500:
        return ErrorKind.INVALID
    return None


def error_status(exc: BaseException) -> int | None:
    """The HTTP status of a provider exception, wherever its SDK keeps it."""
    for holder in (exc, getattr(exc, "response", None)):
        for name in ("status_code", "status", "http_status"):
            value = getattr(holder, name, None)
            if isinstance(value, int):
                return value
    return None


def classify_error(exc: BaseException) -> str:
    """Classify a failure as one of the `ErrorKind` values."""
    if isinstance(exc, ModelError):
        return exc.kind
    if isinstance(exc, asyncio.CancelledError):
        return ErrorKind.CANCELLED
    kind = classify_failure(exc)
    if kind in (ErrorKind.INVALID, ErrorKind.INTERNAL) and is_overflow(exc):
        return ErrorKind.CONTEXT
    return kind


def classify_failure(exc: BaseException) -> str:
    """The kind of a failure by its class, its HTTP status or its nature."""
    for cls in type(exc).__mro__:
        kind = ERROR_KINDS.get(cls.__name__)
        if kind is not None:
            return kind
    status = error_status(exc)
    if status is not None:
        kind = status_kind(status)
        if kind is not None:
            return kind
    if isinstance(exc, OSError):
        return ErrorKind.CONNECTION
    return ErrorKind.INTERNAL


def model_error(
    exc: BaseException, kind: str, *, role: str = LEADER_ROLE, attempts: int = 1
) -> ModelError:
    """Wrap a failed model call, keeping the message the provider gave."""
    detail = (
        exc.detail
        if isinstance(exc, ModelError)
        else (str(exc).strip() or type(exc).__name__)
    )
    tries = f" after {attempts} attempts" if attempts > 1 else ""
    return ModelError(
        f"{role} model {kind}{tries}: {detail}",
        kind,
        role=role,
        attempts=attempts,
        detail=detail,
    )


def usage_value(raw: Any, *names: str) -> int:
    """The first of `names` present on a usage payload, as attribute or key."""
    for name in names:
        value = raw.get(name) if isinstance(raw, dict) else getattr(raw, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return 0


@dataclass
class Usage:
    """What was spent: per run on `RunResult`, in total on a `ModelPool`."""

    requests: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(
        self, *, requests: int = 1, input_tokens: int = 0, output_tokens: int = 0
    ) -> "Usage":
        self.requests += int(requests)
        self.input_tokens += int(input_tokens)
        self.output_tokens += int(output_tokens)
        return self

    def record(self, raw: Any) -> "Usage":
        """Accumulate one provider usage payload, under either naming."""
        if raw is None:
            return self
        return self.add(
            requests=usage_value(raw, "requests") or 1,
            input_tokens=usage_value(raw, "input_tokens", "prompt_tokens"),
            output_tokens=usage_value(raw, "output_tokens", "completion_tokens"),
        )

    def price(self, input_rate: float = 0.0, output_rate: float = 0.0) -> float:
        """Cost the tokens at rates quoted per million, and keep the result."""
        spent = self.input_tokens * input_rate + self.output_tokens * output_rate
        self.cost = spent / 1_000_000
        return self.cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost": round(self.cost, 6),
        }


#: The accounting of the run in progress on this task. A model call made
#: anywhere below `Engine.running` — a tool delegating to the vision model, the
#: summariser of the memory — finds its sink here instead of being handed one,
#: and two concurrent runs never share it because each run is its own task.
_USAGE: "contextvars.ContextVar[Usage | None]" = contextvars.ContextVar(
    "agent_usage", default=None
)


def current_usage() -> Usage | None:
    """The `Usage` of the run in progress on this task, when there is one."""
    return _USAGE.get()


def record_usage(raw: Any, *sinks: "Usage | None") -> None:
    """Accumulate one provider usage payload onto each distinct sink given."""
    accounted: list[Usage] = []
    for sink in (*sinks, current_usage()):
        if isinstance(sink, Usage) and not any(sink is seen for seen in accounted):
            accounted.append(sink)
            sink.record(raw)


def usage_of(streamed: Any) -> Any:
    """The usage of an SDK run, wherever that SDK happens to keep it.

    The running totals of the SDK are preferred; a build that does not keep them
    is summed from the raw responses instead, and one that keeps neither simply
    reports nothing rather than failing the run it was accounting.
    """
    running = getattr(getattr(streamed, "context_wrapper", None), "usage", None)
    if running is not None:
        return running
    totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    for response in getattr(streamed, "raw_responses", None) or []:
        raw = getattr(response, "usage", None)
        if raw is None:
            continue
        totals["requests"] += usage_value(raw, "requests") or 1
        totals["input_tokens"] += usage_value(raw, "input_tokens", "prompt_tokens")
        totals["output_tokens"] += usage_value(raw, "output_tokens", "completion_tokens")
    return totals if totals["requests"] else None


@dataclass(frozen=True)
class RetryPolicy:
    """How a model call is bounded in time and how often it is tried again.

    `attempts` counts attempts in total (``1`` never retries) and `timeout`
    bounds a single attempt (``0`` leaves it unbounded); `stall` bounds the
    silence between two events of a stream instead (``0`` never stalls), which
    the transport is given as its read timeout. The wait after failure
    *n* is ``backoff * 2 ** (n - 1)`` seconds, capped at `max_backoff` and
    multiplied by a random factor in ``[1 - jitter, 1]``, so that the clients a
    provider failed together do not all come back at the same moment.
    """

    attempts: int = 3
    timeout: float = 120.0
    stall: float = 600.0
    backoff: float = 0.5
    max_backoff: float = 30.0
    jitter: float = 0.5
    transient: tuple[str, ...] = ErrorKind.TRANSIENT

    @classmethod
    def from_config(cls, config: "AgentConfig") -> "RetryPolicy":
        """The policy a configuration describes (`*_timeout`, `retry_*`)."""
        return cls(
            attempts=max(1, int(config.retry_attempts)),
            timeout=max(0.0, float(config.request_timeout)),
            stall=max(0.0, float(config.stream_timeout)),
            backoff=max(0.0, float(config.retry_backoff)),
            max_backoff=max(0.0, float(config.retry_max_backoff)),
            jitter=min(max(float(config.retry_jitter), 0.0), 1.0),
        )

    def retryable(self, kind: str, attempt: int) -> bool:
        """True when `kind` is worth another attempt after attempt `attempt`."""
        return attempt < self.attempts and kind in self.transient

    def delay(self, attempt: int) -> float:
        """Seconds to wait after failure `attempt` before trying again."""
        capped = min(self.backoff * 2 ** max(attempt - 1, 0), self.max_backoff)
        return capped * (1.0 - self.jitter * random.random())

    async def call(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        role: str = LEADER_ROLE,
        bounded: bool = True,
        resumable: Callable[[], bool] | None = None,
        on_retry: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        """Await ``operation()`` under this policy and return what it returns.

        A failure is classified, tried again while it is transient and the
        caller still considers the call `resumable`, and otherwise raised: as a
        `ModelError` carrying the kind, or unchanged when nothing is known about
        it, so a defect in the harness is never disguised as a provider fault.
        Cancellation is never retried. Set `bounded` to ``False`` for a call
        that enforces its own timeout, such as a stream read event by event.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                if bounded and self.timeout:
                    async with asyncio.timeout(self.timeout):
                        return await operation()
                return await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                kind = classify_error(exc)
                again = self.retryable(kind, attempt) and (
                    resumable is None or resumable()
                )
                if not again:
                    if kind == ErrorKind.INTERNAL:
                        raise
                    raise model_error(exc, kind, role=role, attempts=attempt) from exc
                pause = self.delay(attempt)
                usage = current_usage()
                if usage is not None:
                    usage.retries += 1
                if on_retry is not None:
                    outcome = on_retry(
                        {
                            "role": role,
                            "error_kind": kind,
                            "attempt": attempt,
                            "attempts": self.attempts,
                            "delay": pause,
                            "error": str(exc),
                        }
                    )
                    if inspect.isawaitable(outcome):
                        await outcome
                await asyncio.sleep(pause)


# ---------------------------------------------------------------------------
# Models
#
# A run may need more than one model: a leader that drives the agentic loop and
# specialists (vision, ...) that tools delegate to. `ModelPool` keeps one client
# per endpoint and one SDK model per model name so extra roles are cheap, and
# every request it makes itself goes through a `RetryPolicy` and is accounted.
# ---------------------------------------------------------------------------


class ModelPool:
    """Lazily builds and caches API clients and SDK models by `ModelSpec`."""

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self._clients: dict[tuple[str, str], Any] = {}
        self._models: dict[tuple[str, str, str], Any] = {}
        #: Applied to the calls the pool makes when one is not passed in.
        self.policy = policy or RetryPolicy()
        #: Everything this pool has spent, across runs and roles.
        self.usage = Usage()
        #: Context windows by endpoint and model, as found or learned (0: unknown).
        self._windows: dict[tuple[str, str], int] = {}
        #: `num_ctx` to send by endpoint and model, as found (0: send none).
        self._contexts: dict[tuple[str, str], int] = {}

    def client(self, spec: ModelSpec) -> Any:
        """Return the shared async client for the endpoint of `spec`.

        The client is told not to retry: a retry inside the SDK is invisible to
        the event bus, unjittered, uncounted and nested inside whatever the
        harness is already doing, so retrying belongs to the `RetryPolicy` and
        to it alone. The transport timeout of the pool's policy is kept, since
        it is what stops a socket that has gone quiet mid-response; its read
        timeout is the longer of `timeout` and `stall`, because the leader's
        stream shares the client and may be legitimately silent for longer
        than a request may take (a specialist call stays bound by `timeout`).
        """
        client = self._clients.get(spec.endpoint)
        if client is None:
            import httpx
            from openai import AsyncOpenAI

            options: dict[str, Any] = {"max_retries": 0}
            read = max(self.policy.timeout, self.policy.stall)
            if read:
                options["timeout"] = httpx.Timeout(self.policy.timeout or read, read=read)
            # An empty key means "no key here": pass nothing so the SDK falls
            # back to its own environment lookups (OPENAI_API_KEY, ...). When
            # that lookup would find nothing either, hand the SDK a placeholder
            # instead — it refuses to construct a client without some key, and
            # endpoints that need no auth (a local Ollama, a proxy) would fail
            # to start over a credential they never ask for.
            if spec.api_key:
                options["api_key"] = spec.api_key
            elif not os.environ.get("OPENAI_API_KEY"):
                options["api_key"] = "no-key"
            client = AsyncOpenAI(base_url=spec.api_url, **options)
            self._clients[spec.endpoint] = client
        return client

    def model(self, spec: ModelSpec) -> Any:
        """Return the shared SDK model for `spec`."""
        key = (spec.name, spec.api_url, spec.api_key)
        model = self._models.get(key)
        if model is None:
            from agents import OpenAIChatCompletionsModel

            model = OpenAIChatCompletionsModel(
                model=spec.name, openai_client=self.client(spec)
            )
            self._models[key] = model
        return model

    #: Keys a model listing names the context window under: OpenRouter,
    #: Together (`context_length`), vLLM (`max_model_len`), Groq
    #: (`context_window`), Mistral, LM Studio (`max_context_length`), LiteLLM.
    WINDOW_KEYS = (
        "context_length",
        "max_model_len",
        "context_window",
        "max_context_length",
        "max_input_tokens",
    )
    #: Seconds a window lookup may take; a run never waits longer on it.
    PROBE_TIMEOUT = 8.0

    async def window(self, spec: ModelSpec) -> int:
        """The context window of the model of `spec`, as its provider says, or 0.

        Asked once per endpoint and model; a lookup that fails or stalls is
        remembered as unknown and never fails a run.
        """
        key = (spec.api_url, spec.name)
        if key not in self._windows:
            found = 0
            try:
                found = int(
                    await asyncio.wait_for(self.probe_window(spec), self.PROBE_TIMEOUT) or 0
                )
            except Exception as exc:
                print(f"[context] no window found for {spec.name}: {exc!r}", file=sys.stderr)
            self._windows[key] = found
        return self._windows[key]

    def known_window(self, spec: ModelSpec) -> int:
        """The window of `spec` when it was found or learned already, else 0."""
        return self._windows.get((spec.api_url, spec.name), 0)

    def learn_window(self, spec: ModelSpec, window: int) -> None:
        """Remember a window the provider revealed (in a context length error)."""
        self._windows[(spec.api_url, spec.name)] = int(window)

    async def probe_window(self, spec: ModelSpec) -> int:
        """Ask the endpoint: its model listing, then Ollama's own API."""
        with contextlib.suppress(Exception):
            page = await self.client(spec).models.list()
            for model in getattr(page, "data", None) or []:
                data = model.model_dump() if hasattr(model, "model_dump") else dict(model)
                if data.get("id") == spec.name:
                    found = find_key(data, self.WINDOW_KEYS)
                    if found:
                        return found
        return await self.probe_ollama(spec)

    async def probe_ollama(self, spec: ModelSpec) -> int:
        """The runtime window of an Ollama server, which its listing omits.

        A loaded model reports it (`/api/ps`), a Modelfile may set it
        (`num_ctx`); otherwise Ollama runs a small default and truncates a
        longer prompt from the front without saying so, so a conservative
        window is assumed. Not an Ollama server: 0.
        """
        import httpx

        root = spec.api_url.rstrip("/").removesuffix("/v1")
        names = {spec.name, f"{spec.name}:latest"}
        async with httpx.AsyncClient(timeout=self.PROBE_TIMEOUT) as http:
            listed = await http.get(f"{root}/api/ps")
            body = listed.json() if listed.status_code == 200 else None
            if not isinstance(body, dict) or "models" not in body:
                return 0
            for model in body.get("models") or []:
                if names & {model.get("name"), model.get("model")} and model.get("context_length"):
                    return int(model["context_length"])
            shown = await http.post(f"{root}/api/show", json={"model": spec.name})
            if shown.status_code == 200:
                match = re.search(r"num_ctx\s+(\d+)", str(shown.json().get("parameters") or ""))
                if match:
                    return int(match.group(1))
        return OLLAMA_CONTEXT_WINDOW

    async def num_ctx(self, spec: ModelSpec) -> int:
        """The `num_ctx` to send for `spec`: configured, else looked up, else 0.

        Looked up once per endpoint and model; a lookup that fails or stalls is
        remembered as 0 (send none) and never fails a run.
        """
        if spec.num_ctx > 0:
            return int(spec.num_ctx)
        key = (spec.api_url, spec.name)
        if key not in self._contexts:
            found = 0
            try:
                found = int(
                    await asyncio.wait_for(self.probe_ctx(spec), self.PROBE_TIMEOUT) or 0
                )
            except Exception as exc:
                print(f"[context] no num_ctx found for {spec.name}: {exc!r}", file=sys.stderr)
            self._contexts[key] = found
        return self._contexts[key]

    def known_num_ctx(self, spec: ModelSpec) -> int:
        """The `num_ctx` of `spec` when configured or looked up already, else 0."""
        return int(spec.num_ctx) if spec.num_ctx > 0 else self._contexts.get((spec.api_url, spec.name), 0)

    def extra_body(self, spec: ModelSpec | None) -> dict[str, Any] | None:
        """Request body extras for `spec`: ``{"options": {"num_ctx": n}}`` or ``None``."""
        size = self.known_num_ctx(spec) if spec is not None else 0
        return {"options": {"num_ctx": size}} if size > 0 else None

    async def probe_ctx(self, spec: ModelSpec) -> int:
        """The context length an Ollama model was trained for (`/api/show`), else 0."""
        import httpx

        root = spec.api_url.rstrip("/").removesuffix("/v1")
        async with httpx.AsyncClient(timeout=self.PROBE_TIMEOUT) as http:
            shown = await http.post(f"{root}/api/show", json={"model": spec.name})
        if shown.status_code != 200:
            return 0
        try:
            info = shown.json().get("model_info")
        except (ValueError, AttributeError):
            return 0
        if not isinstance(info, dict):
            return 0
        lengths = [
            int(value)
            for name, value in info.items()
            if name.endswith(".context_length")
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ]
        return max(lengths, default=0)

    async def complete(
        self,
        spec: ModelSpec,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        policy: RetryPolicy | None = None,
        usage: "Usage | None" = None,
        on_retry: Callable[[dict[str, Any]], Any] | None = None,
    ) -> str:
        """Single chat completion against `spec`; no tools, no agentic loop.

        The request is bounded and retried by `policy` (the pool's by default)
        and what it spends is recorded on `usage`, on the run in progress and on
        the totals of the pool. A failure that survives the policy is raised as
        a `ModelError`, so the caller sees why it failed and not only that it
        did.
        """
        resolved = policy or self.policy
        await self.num_ctx(spec)
        extra_body = self.extra_body(spec)

        async def request() -> Any:
            return await self.client(spec).chat.completions.create(
                model=spec.name, messages=messages, max_tokens=max_tokens, extra_body=extra_body
            )

        response = await resolved.call(request, role=spec.role, on_retry=on_retry)
        self.record(getattr(response, "usage", None), usage)
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        return (choices[0].message.content or "").strip()

    def record(self, raw: Any, usage: "Usage | None" = None) -> None:
        """Account one provider response on the pool, the run and `usage`."""
        record_usage(raw, self.usage, usage)

    async def describe_image(
        self,
        spec: ModelSpec,
        data_url: str,
        question: str = "",
        *,
        instructions: str = DEFAULT_VISION_INSTRUCTIONS,
        max_tokens: int | None = None,
        policy: RetryPolicy | None = None,
        usage: "Usage | None" = None,
        on_retry: Callable[[dict[str, Any]], Any] | None = None,
    ) -> str:
        """Ask a vision model about one image and return its answer as text."""
        messages = [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question.strip() or "Describe this image."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        return await self.complete(
            spec,
            messages,
            max_tokens=max_tokens,
            policy=policy,
            usage=usage,
            on_retry=on_retry,
        )

    def clear(self) -> None:
        """Drop every cached client and model. The totals are kept."""
        self._clients.clear()
        self._models.clear()


# ---------------------------------------------------------------------------
# Tools
#
# `Workspace` holds the sandboxed filesystem/shell primitives (unit testable on
# their own) and `ToolRegistry` exposes them to the model as function tools.
# ---------------------------------------------------------------------------

IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

MAX_READ_BYTES = 2 * 1024 * 1024


def find_key(data: Any, keys: Iterable[str]) -> int:
    """The first positive integer under one of `keys`, shallowest first."""
    wanted = tuple(keys)
    level = [data]
    while level:
        for node in level:
            if isinstance(node, dict):
                for key in wanted:
                    value = node.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                        return int(value)
        level = [
            child
            for node in level
            for child in (node.values() if isinstance(node, dict) else node if isinstance(node, list) else ())
            if isinstance(child, (dict, list))
        ]
    return 0


def clip_output(text: str, limit: int) -> str:
    """Clip text to about `limit` characters, keeping its head and tail.

    A command's useful lines are usually its first (what ran) and its last
    (the error, the summary, the exit code); the middle is what gets dropped.
    A `limit` of 0 or less keeps the text whole.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[... {omitted} chars omitted ...]\n{text[-tail:]}"


@dataclass
class TextPage:
    """A run of whole lines of a text file (`Workspace.read_page`)."""

    text: str
    #: First and last line returned (1-based, inclusive) and the line count.
    start: int
    end: int
    total: int
    path: str = ""
    #: The one line returned was longer than the page and is clipped.
    clipped: bool = False

    @property
    def complete(self) -> bool:
        """True when the page is the whole file, nothing cut."""
        return not self.clipped and self.start <= 1 and self.end >= self.total

    @property
    def footer(self) -> str:
        """Where the page sits in the file and how to read on; empty when whole."""
        if self.complete:
            return ""
        note = f"lines {self.start}-{self.end} of {self.total} of {self.path}"
        if self.clipped:
            note += f", line {self.end} clipped (too long to return whole)"
        if self.end < self.total:
            note += f"; continue with offset={self.end + 1}"
        return f"[{note}]"

    def render(self) -> str:
        """The page as a tool returns it: the lines, then the footer."""
        if self.complete:
            return self.text
        return f"{self.text.rstrip(chr(10))}\n{self.footer}"


class ReadRepeats:
    """Refuses a read repeated back to back when its last answer was cut short.

    The same arguments return the same cut, so a model that answers a cut
    output by reading it again loops (more so while compaction trims the
    context around it); it is pointed at the next range instead. A write in
    between forgets the last read, as the file may have changed.
    """

    def __init__(self) -> None:
        self.key: tuple[Any, ...] | None = None
        self.footer = ""

    def check(self, key: tuple[Any, ...]) -> str | None:
        """An error to return instead of running `key`, or ``None`` to run it."""
        if not self.footer or key != self.key:
            return None
        return (
            f"Error: this exact read just returned {self.footer}. Repeating it "
            "returns the same text; read the next range (offset) or a narrower "
            "one (offset and limit) instead."
        )

    def record(self, key: tuple[Any, ...], footer: str) -> None:
        self.key, self.footer = key, footer

    def forget(self) -> None:
        self.key, self.footer = None, ""


def guess_mime(path: Path | str) -> str:
    """Guess a MIME type from a file extension."""
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_MIME:
        return IMAGE_MIME[suffix]
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


# Sandboxes. A confined shell runs under an OS mechanism that holds it to the
# reach of the file tools: full access to the workspace, read and execute
# access to the system directories (everywhere with `outside_read`) and writes
# beyond the workspace only with `outside_write`. A backend turns a command
# into the argv that runs it confined; where the platform offers no boundary
# none is picked, and the shell is withheld rather than run unconfined.

#: Directories a confined shell may always read and execute from: what any
#: command needs to start at all. Missing ones are skipped.
SYSTEM_PATHS = {
    "linux": (
        "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc",
        "/opt", "/nix", "/snap", "/run", "/proc", "/sys", "/dev",
    ),
    "darwin": (
        "/usr", "/bin", "/sbin", "/System", "/Library", "/Applications", "/opt",
        "/private/etc", "/private/var/db", "/dev",
    ),
}

#: Device files a confined shell may also write to (Linux; Seatbelt has its own).
DEVICE_PATHS = (
    "/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom",
    "/dev/tty", "/dev/ptmx", "/dev/pts", "/dev/shm",
)

#: Applies Landlock to itself, then becomes the shell; the restriction is
#: inherited by everything the shell starts. Run with ``python -I -S -c`` and
#: the policy as JSON in ``argv[1]``. Any failure exits 126 before the command
#: runs: a sandbox that cannot be applied never degrades to no sandbox.
LANDLOCK_LAUNCHER = r"""
import ctypes, json, os, sys
policy = json.loads(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
L = ctypes.c_long
def fail(what):
    sys.stderr.write("sandbox: %s failed: %s\n" % (what, os.strerror(ctypes.get_errno())))
    sys.exit(126)
abi = libc.syscall(L(444), ctypes.c_void_p(None), L(0), L(1))
if abi < 1:
    fail("landlock")
handled = (1 << 13) - 1
files = (1 << 0) | (1 << 1) | (1 << 2)
if abi >= 2:
    handled |= 1 << 13
if abi >= 3:
    handled |= 1 << 14
    files |= 1 << 14
if abi >= 5:
    handled |= 1 << 15
    files |= 1 << 15
read = (1 << 0) | (1 << 2) | (1 << 3)
class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]
class Beneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
attr = Ruleset(handled)
ruleset = libc.syscall(L(444), ctypes.c_void_p(ctypes.addressof(attr)), L(ctypes.sizeof(attr)), L(0))
if ruleset < 0:
    fail("landlock_create_ruleset")
def allow(path, access):
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return
    if not os.path.isdir(path):
        access &= files
    rule = Beneath(access & handled, fd)
    if libc.syscall(L(445), L(ruleset), L(1), ctypes.c_void_p(ctypes.addressof(rule)), L(0)):
        fail("landlock_add_rule %s" % path)
    os.close(fd)
for path in policy["read"]:
    allow(path, read)
for path in policy["write"]:
    allow(path, handled & ~read)
for path in policy["full"]:
    allow(path, handled)
if libc.prctl(ctypes.c_int(38), ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)):
    fail("prctl")
if libc.syscall(L(446), L(ruleset), L(0)):
    fail("landlock_restrict_self")
os.close(ruleset)
os.execv(policy["shell"], [policy["shell"], "-c", policy["command"]])
"""


def system_paths() -> list[str]:
    """What a confined shell may read and execute: the system, the running
    interpreter and every absolute ``PATH`` entry but the home directory itself."""
    base = SYSTEM_PATHS.get(sys.platform, SYSTEM_PATHS["linux"])
    extra = {sys.prefix, sys.base_prefix, sys.exec_prefix}
    home = str(Path.home())
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and os.path.isabs(entry) and entry not in (os.sep, home):
            extra.add(entry)
    return [path for path in (*base, *sorted(extra)) if os.path.exists(path)]


class Sandbox:
    """Confines a shell command to the reach of a `Workspace`.

    A backend says whether this machine supports it (`available`) and turns a
    command into the argv that runs it confined (`argv`); `environ` points the
    scratch directories of the command (temp, cache, home) into the workspace,
    since what it cannot write it cannot use.
    """

    name = "none"
    #: Where a confined command keeps its scratch files, under the workspace.
    SCRATCH = ".sandbox"

    @classmethod
    def available(cls) -> bool:
        return False

    def argv(self, command: str, ws: "Workspace") -> list[str]:  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def paths(ws: "Workspace") -> tuple[list[str], list[str], list[str]]:
        """``(read, write, full)``: paths readable, writable only, and both."""
        read = [os.sep] if ws.outside_read else system_paths()
        write = [os.sep] if ws.outside_write else []
        return read, write, [str(ws.root)]

    def environ(self, env: dict[str, str], ws: "Workspace") -> dict[str, str]:
        """Point temp (and, when confined, home and cache) into the workspace."""
        scratch = ws.root / self.SCRATCH
        tmp = scratch / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env.update(TMPDIR=str(tmp), TMP=str(tmp), TEMP=str(tmp))
        if not ws.outside_read:
            home = scratch / "home"
            home.mkdir(parents=True, exist_ok=True)
            env["HOME"] = str(home)  # dotfiles out of reach would fail tools (git)
        elif not ws.outside_write:
            env["XDG_CACHE_HOME"] = str(scratch / "cache")
        return env

    def describe(self, ws: "Workspace") -> str:
        """The boundary in words, for the description the model reads."""
        reads = "anywhere" if ws.outside_read else "the workspace and system directories"
        writes = "anywhere" if ws.outside_write else "inside the workspace only"
        return f" It runs sandboxed: it can read {reads} and write {writes}."


class LandlockSandbox(Sandbox):
    """Linux 5.13+: the shell restricts itself with Landlock before it starts."""

    name = "landlock"

    @classmethod
    def available(cls) -> bool:
        return _landlock_abi() > 0

    def argv(self, command: str, ws: "Workspace") -> list[str]:
        read, write, full = self.paths(ws)
        policy = {
            "read": read,
            "write": write,
            "full": [*full, *(p for p in DEVICE_PATHS if os.path.exists(p))],
            "shell": "/bin/sh",
            "command": command,
        }
        return [sys.executable, "-I", "-S", "-c", LANDLOCK_LAUNCHER, json.dumps(policy)]


class BubblewrapSandbox(Sandbox):
    """Linux without Landlock: a bubblewrap mount namespace. Coarser — a
    writable root is readable too, so `outside_write` implies reading."""

    name = "bwrap"

    @classmethod
    def available(cls) -> bool:
        return _bwrap_works()

    def argv(self, command: str, ws: "Workspace") -> list[str]:
        read, write, full = self.paths(ws)
        args = [shutil.which("bwrap") or "bwrap", "--die-with-parent"]
        for path in read:
            args += ["--ro-bind-try", path, path]
        for path in [*write, *full]:
            args += ["--bind-try", path, path]
        args += ["--dev", "/dev", "--chdir", str(ws.root), "/bin/sh", "-c", command]
        return args


class SeatbeltSandbox(Sandbox):
    """macOS: ``sandbox-exec`` with a generated profile that denies what the
    workspace flags do not allow on top of the default policy."""

    name = "seatbelt"
    EXE = "/usr/bin/sandbox-exec"
    DEVICES = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/stdout", "/dev/stderr", "/dev/dtracehelper")

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "darwin" and os.path.exists(cls.EXE)

    @staticmethod
    def quote(path: str) -> str:
        return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def profile(self, ws: "Workspace") -> str:
        read, _write, full = self.paths(ws)
        lines = ["(version 1)", "(allow default)"]
        if not ws.outside_write:
            allowed = [f"(subpath {self.quote(p)})" for p in full]
            allowed += [f"(literal {self.quote(p)})" for p in self.DEVICES]
            allowed.append('(subpath "/dev/fd")')
            lines.append(f"(deny file-write* (require-not (require-any {' '.join(allowed)})))")
        if not ws.outside_read:
            allowed = [f"(subpath {self.quote(p)})" for p in [*read, *full]]
            lines.append(f"(deny file-read-data (require-not (require-any {' '.join(allowed)})))")
        return "\n".join(lines)

    def argv(self, command: str, ws: "Workspace") -> list[str]:
        return [self.EXE, "-p", self.profile(ws), "/bin/sh", "-c", command]


#: Sandbox backends by name, in the order ``auto`` tries them.
SANDBOXES: dict[str, type[Sandbox]] = {
    "landlock": LandlockSandbox,
    "bwrap": BubblewrapSandbox,
    "seatbelt": SeatbeltSandbox,
}


def pick_sandbox(name: str = "auto") -> Sandbox | None:
    """The sandbox `name` asks for when this machine has it, else ``None``.

    ``auto`` takes the first available backend, ``none`` never takes one.
    """
    name = (name or "auto").strip().lower()
    order = tuple(SANDBOXES) if name == "auto" else (name,)
    for key in order:
        kind = SANDBOXES.get(key)
        if kind is not None and kind.available():
            return kind()
    return None


@functools.cache
def _landlock_abi() -> int:
    """The Landlock ABI of the running kernel, ``0`` when there is none."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        abi = libc.syscall(ctypes.c_long(444), ctypes.c_void_p(None), ctypes.c_long(0), ctypes.c_long(1))
    except (OSError, AttributeError):
        return 0
    return max(int(abi), 0)


@functools.cache
def _bwrap_works() -> bool:
    """Whether bubblewrap is installed and allowed to make a namespace here."""
    exe = shutil.which("bwrap")
    if not exe or not sys.platform.startswith("linux"):
        return False
    import subprocess

    try:
        probe = subprocess.run(
            [exe, "--ro-bind", "/", "/", "true"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


class WorkspaceError(ValueError):
    """Raised when an operation would escape the workspace root."""


class Workspace:
    """Filesystem and shell primitives scoped to a single directory."""

    #: Seconds a killed command may take to release its pipes before they are
    #: abandoned, so a process that survives the kill cannot hang the caller.
    KILL_GRACE = 5.0
    #: Bytes read from a pipe of a running command at a time.
    READ_CHUNK = 4096

    def __init__(
        self,
        root: Path | str,
        *,
        shell_timeout: int = 900,
        shell_output_limit: int = 32_000,
        shell_heartbeat: float = 5.0,
        outside_read: bool = False,
        outside_write: bool = False,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.shell_timeout = shell_timeout
        self.shell_output_limit = shell_output_limit
        self.shell_heartbeat = shell_heartbeat
        #: Reads (writes) may resolve outside the root.
        self.outside_read = outside_read
        self.outside_write = outside_write
        #: Confines `run_shell` while the reach above is not total.
        self.sandbox = sandbox

    @property
    def confined(self) -> bool:
        """True when some path outside the root is off limits."""
        return not (self.outside_read and self.outside_write)

    @property
    def shell_allowed(self) -> bool:
        """A shell reaches anything the process can, so it is offered only
        when nothing is off limits or a sandbox holds it to the same reach.

        `run_shell` itself does not check: the primitive runs what it is given,
        and `ToolRegistry` decides whether the model gets it.
        """
        return not self.confined or self.sandbox is not None

    def resolve(self, raw: str, *, read: bool = False) -> Path:
        """Resolve a model supplied path inside the workspace root.

        A `read` may leave the root when the workspace allows `outside_read`,
        a write when it allows `outside_write`.
        """
        cleaned = (raw or "").strip().strip("\"'")
        if not cleaned:
            raise WorkspaceError("path must not be empty")
        path = Path(cleaned).expanduser()
        if not path.is_absolute():
            path = self.root / path
        try:
            # resolve() also follows symlinks, so links cannot escape the root
            resolved = path.resolve()
        except OSError as exc:  # pragma: no cover - platform specific
            raise WorkspaceError(str(exc)) from exc
        if self.outside_read if read else self.outside_write:
            return resolved
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise WorkspaceError(f"path escapes the workspace root: {raw}")
        return resolved

    def relative(self, path: Path) -> str:
        with contextlib.suppress(ValueError):
            return str(path.relative_to(self.root))
        return str(path)

    # -- sync primitives ----------------------------------------------------

    def read_text(self, raw: str) -> str:
        path = self.resolve(raw, read=True)
        if not path.is_file():
            raise WorkspaceError(f"file not found: {self.relative(path)}")
        if path.stat().st_size > MAX_READ_BYTES:
            raise WorkspaceError(f"file too large: {self.relative(path)}")
        return path.read_text(encoding="utf-8", errors="replace")

    def read_page(self, raw: str, offset: int = 1, limit: int = 0, max_chars: int = 0) -> "TextPage":
        """Whole lines of a text file from line `offset` (1-based).

        At most `limit` lines (0: any) and `max_chars` characters (0: any); a
        first line longer than `max_chars` on its own is clipped.
        """
        text = self.read_text(raw)
        name = self.relative(self.resolve(raw, read=True))
        lines = text.splitlines(keepends=True)
        total = len(lines)
        offset = max(int(offset or 1), 1)
        if total == 0:
            return TextPage("", 1, 0, 0, name)
        if offset > total:
            raise WorkspaceError(f"offset {offset} is past the end: {name} has {total} lines")
        stop = total if limit <= 0 else min(total, offset - 1 + int(limit))
        chosen: list[str] = []
        size = 0
        clipped = False
        for line in lines[offset - 1 : stop]:
            if max_chars > 0 and size + len(line) > max_chars:
                if not chosen:
                    chosen.append(clip_output(line, max_chars))
                    clipped = True
                break
            chosen.append(line)
            size += len(line)
        return TextPage("".join(chosen), offset, offset - 1 + len(chosen), total, name, clipped)

    def write_text(self, raw: str, content: str) -> str:
        path = self.resolve(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self.relative(path)

    def list_dir(self, raw: str = ".") -> list[str]:
        path = self.resolve(raw, read=True)
        if not path.is_dir():
            raise WorkspaceError(f"directory not found: {self.relative(path)}")
        return sorted(e.name + ("/" if e.is_dir() else "") for e in path.iterdir())

    def read_data_url(self, raw: str) -> str:
        path = self.resolve(raw, read=True)
        if not path.is_file():
            raise WorkspaceError(f"file not found: {self.relative(path)}")
        data = path.read_bytes()
        if not data:
            raise WorkspaceError(f"file is empty: {self.relative(path)}")
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{guess_mime(path)};base64,{encoded}"

    def apply_patch(self, raw: str, diff: str, operation: str = "update_file") -> str:
        """Apply a unified diff to a workspace file."""
        from agents.apply_diff import apply_diff

        path = self.resolve(raw)
        if operation == "delete_file":
            path.unlink(missing_ok=True)
            return f"Deleted {self.relative(path)}"
        if operation == "create_file":
            text = apply_diff("", diff, mode="create")
        elif operation == "update_file":
            if not path.is_file():
                raise WorkspaceError(f"file not found: {self.relative(path)}")
            text = apply_diff(path.read_text(encoding="utf-8"), diff, mode="default")
        else:
            raise WorkspaceError(f"unknown operation: {operation}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return f"Wrote {self.relative(path)}"

    # -- async primitives ---------------------------------------------------

    async def run_shell(self, command: str, progress: Progress | None = None) -> str:
        """Run a shell command in the workspace without blocking the loop.

        Output is read as it is produced and handed to `progress`, which also
        gets an empty string every `shell_heartbeat` seconds while the command
        runs, so a caller can tell a slow command from a dead one. A command that
        times out or is cancelled is killed with every process it started: a
        child holding the pipes would otherwise outlive the shell and keep the
        call waiting for it.
        """
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        options: dict[str, Any] = dict(
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.root,
            # its own process group, so a kill reaches its children (POSIX)
            start_new_session=True,
        )
        try:
            if self.sandbox is not None and self.confined:
                argv = self.sandbox.argv(command, self)
                env = self.sandbox.environ(env, self)
                process = await asyncio.create_subprocess_exec(*argv, env=env, **options)
            else:
                process = await asyncio.create_subprocess_shell(command, env=env, **options)
        except OSError as exc:
            return f"(command failed: {exc})"
        stdout: list[str] = []
        stderr: list[str] = []

        async def drain() -> None:
            await asyncio.gather(
                self.pump(process.stdout, stdout, progress),
                self.pump(process.stderr, stderr, progress),
            )
            await process.wait()

        task = asyncio.ensure_future(drain())
        try:
            async with asyncio.timeout(self.shell_timeout):
                while not task.done():
                    await asyncio.wait({task}, timeout=self.shell_heartbeat or None)
                    if not task.done():
                        await self.notify(progress, "")
            task.result()
        except TimeoutError:
            return f"(command timed out after {self.shell_timeout}s)"
        finally:
            if not task.done():
                await self.kill_tree(process)
                # The pipes close once the last process holding them is gone;
                # the wait is bounded so a survivor cannot hang the caller.
                done, _ = await asyncio.wait({task}, timeout=self.KILL_GRACE)
                if not done:
                    task.cancel()
                    print(
                        f"[shell] pid {process.pid} kept its pipes open "
                        f"{self.KILL_GRACE:g}s after the kill; abandoned",
                        file=sys.stderr,
                    )
        output = "".join(stdout)
        error = "".join(stderr)
        if error:
            output += ("\n" if output else "") + error
        if process.returncode:
            output += f"\n[exit code {process.returncode}]"
        output = output.strip() or "(no output)"
        clipped = clip_output(output, self.shell_output_limit)
        if clipped is not output:
            print(
                f"[shell] output clipped: {len(output)} -> {len(clipped)} chars",
                file=sys.stderr,
            )
        return clipped

    async def pump(
        self, stream: asyncio.StreamReader | None, sink: list[str], progress: Progress | None
    ) -> None:
        """Copy a pipe into `sink` until it closes, reporting each chunk."""
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := await stream.read(self.READ_CHUNK):
            text = decoder.decode(chunk)
            if text:
                sink.append(text)
                await self.notify(progress, text)
        if tail := decoder.decode(b"", final=True):
            sink.append(tail)
            await self.notify(progress, tail)

    @staticmethod
    async def notify(progress: Progress | None, text: str) -> None:
        """Hand `text` to `progress`; a failing observer never breaks the command."""
        if progress is None:
            return
        try:
            await progress(text)
        except Exception as exc:
            print(f"[shell] progress callback failed: {exc!r}", file=sys.stderr)

    async def kill_tree(self, process: asyncio.subprocess.Process) -> None:
        """Kill a shell and every process it started."""
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(process.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), self.KILL_GRACE)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone
        except (OSError, TimeoutError) as exc:
            print(f"[shell] process tree kill failed: {exc!r}", file=sys.stderr)
        with contextlib.suppress(OSError):  # the shell itself, if the tree kill missed it
            process.kill()


# Work slicing. A task too large for one pass is planned as steps, each sized
# against the slice budget of the run (`SliceBudget`): the harness measures what
# a step reads instead of trusting the model's guess, refuses a step larger than
# one slice, and the budget note of every call recites the open plan, so no
# compaction can fold it away. A plan whose estimate passes a threshold asks the
# user once before it is kept, an `Agent` writes every change of a plan through
# to its store (`PlanBook`), so a resumed session or a restarted process
# continues it, and `delegate` runs one step in a fresh context (a worker).

PLAN_STATUSES = ("pending", "active", "done")
#: The choices of the sizing question when the model offers none of its own.
ASK_GO, ASK_STOP = "Go ahead", "Stop"


class PlanItem(TypedDict):
    """One step of a plan as the model writes it (the `plan` tool's schema)."""

    title: str
    inputs: list[str]
    status: Literal["pending", "active", "done"]


@dataclass
class PlanStep:
    """One step of a plan, with what the harness measured it reads."""

    title: str
    inputs: list[str] = field(default_factory=list)
    status: str = "pending"
    #: Tokens each input reads (provider units), in the order given.
    sizes: list[tuple[str, int]] = field(default_factory=list)
    #: Inputs that do not exist yet: a step may read what an earlier one writes.
    missing: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(size for _, size in self.sizes)

    @property
    def done(self) -> bool:
        return self.status == "done"

    def key(self) -> tuple[str, tuple[str, ...]]:
        return self.title, tuple(self.inputs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "inputs": list(self.inputs),
            "status": self.status,
            "sizes": [[name, size] for name, size in self.sizes],
            "missing": list(self.missing),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PlanStep | None":
        """A step as `to_dict` wrote it, or ``None`` when it is not one."""
        if not isinstance(data, dict) or not str(data.get("title") or "").strip():
            return None
        status = str(data.get("status") or "pending")
        sizes = [
            (str(pair[0]), int(pair[1]))
            for pair in data.get("sizes") or []
            if isinstance(pair, (list, tuple)) and len(pair) == 2
            and isinstance(pair[1], (int, float))
        ]
        return cls(
            str(data["title"]).strip(),
            [str(raw) for raw in data.get("inputs") or []],
            status if status in PLAN_STATUSES else "pending",
            sizes,
            [str(raw) for raw in data.get("missing") or []],
        )


@dataclass(frozen=True)
class Estimate:
    """What the steps of a plan not done yet still take to read.

    `slices` is how many slices that is, `seconds` how long at the model's
    measured read speed (`speed`, tokens a second; 0: not measured), `cost`
    what reading it once costs at the configured price of input tokens.
    """

    tokens: int = 0
    slices: int = 0
    seconds: float = 0.0
    cost: float = 0.0
    speed: float = 0.0

    def describe(self) -> str:
        """The estimate in one line: ``~1.2M tokens to read in 48 slices, ...``."""
        line = f"~{compact_tokens(self.tokens)} tokens to read"
        if self.slices:
            line += f" in {self.slices} slice{'s' if self.slices != 1 else ''}"
        if self.seconds >= 60:
            line += f", ~{self.seconds / 60:.0f} min at ~{self.speed:.0f} tokens/s"
        if self.cost > 0:
            line += f", ~{self.cost:.2f} to read once"
        return line


@dataclass(frozen=True)
class AskLimits:
    """Past which estimate a plan asks the user before it is kept (0: never).

    `price` is what a million input tokens cost (`cost_input`), which the cost
    of an `Estimate` is reckoned at.
    """

    slices: int = 0
    minutes: float = 0.0
    cost: float = 0.0
    price: float = 0.0

    @classmethod
    def of(cls, config: "AgentConfig") -> "AskLimits":
        return cls(
            max(int(config.plan_ask_slices), 0),
            max(float(config.plan_ask_minutes), 0.0),
            max(float(config.plan_ask_cost), 0.0),
            max(float(config.cost_input), 0.0),
        )

    def passed(self, estimate: Estimate) -> list[str]:
        """The thresholds `estimate` passes, by name."""
        passed: list[str] = []
        if self.slices and estimate.slices > self.slices:
            passed.append("slices")
        if self.minutes and estimate.seconds > self.minutes * 60:
            passed.append("minutes")
        if self.cost and estimate.cost > self.cost:
            passed.append("cost")
        return passed


@dataclass
class Plan:
    """The steps of a task, in order.

    `asked` says the sizing question of this plan was put to the user, which
    happens once: a later update carries it over, unless every step of the
    plan it replaces was done.
    """

    steps: list[PlanStep] = field(default_factory=list)
    asked: bool = False

    @property
    def done(self) -> int:
        return sum(1 for step in self.steps if step.done)

    def current(self) -> PlanStep | None:
        """The active step, else the first one still pending."""
        for status in ("active", "pending"):
            for step in self.steps:
                if step.status == status:
                    return step
        return None

    def left(self) -> int:
        """Tokens the steps not done yet still read."""
        return sum(step.tokens for step in self.steps if not step.done)

    def estimate(
        self,
        limits: SliceBudget | None = None,
        throughput: Throughput | None = None,
        price: float = 0.0,
    ) -> Estimate:
        """What the steps not done yet still take: tokens, slices, time, cost."""
        left = self.left()
        slices = -(-left // limits.slice) if limits is not None and left else 0
        speed = throughput.prefill if throughput is not None else 0.0
        seconds = throughput.seconds(left) if throughput is not None else 0.0
        return Estimate(left, slices, seconds, left * price / 1_000_000, speed)

    def to_dict(self) -> dict[str, Any]:
        return {"steps": [step.to_dict() for step in self.steps], "asked": self.asked}

    def number(self, step: PlanStep | None) -> int:
        """The 1-based number of `step` in this plan (0: none)."""
        return next((n for n, own in enumerate(self.steps, 1) if own is step), 0)

    def view(
        self, limits: SliceBudget | None = None, throughput: Throughput | None = None
    ) -> dict[str, Any]:
        """The plan as a client shows it: steps, progress and what is left."""
        estimate = self.estimate(limits, throughput)
        return {
            "steps": [
                {
                    "title": step.title,
                    "status": step.status,
                    "tokens": step.tokens,
                    "missing": list(step.missing),
                }
                for step in self.steps
            ],
            "done": self.done,
            "total": len(self.steps),
            "current": self.number(self.current()),
            "left": estimate.tokens,
            "slices": estimate.slices,
            "seconds": round(estimate.seconds, 1),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Plan | None":
        """A plan as `to_dict` wrote it, or ``None`` when it holds no step."""
        raw = data.get("steps") if isinstance(data, dict) else None
        steps = [step for step in map(PlanStep.from_dict, raw or []) if step is not None]
        return cls(steps, bool(data.get("asked"))) if steps else None

    def brief(self) -> str:
        """The plan in one line, for the budget note."""
        return plan_line(self.view())

    def render(
        self, limits: SliceBudget | None = None, throughput: Throughput | None = None
    ) -> str:
        """The plan as the `plan` tool answers it."""
        marks = {"done": "x", "active": ">"}
        head = f"Plan: {self.done} of {len(self.steps)} done"
        if limits is not None:
            head += f"; one step may read up to {compact_tokens(limits.slice)} tokens"
        lines = [head]
        for number, step in enumerate(self.steps, 1):
            line = f"{number}. [{marks.get(step.status, ' ')}] {step.title}"
            if step.sizes:
                names = ", ".join(name for name, _ in step.sizes)
                line += f" ({compact_tokens(step.tokens)}: {names})"
            if step.missing:
                line += f" (not found yet: {', '.join(step.missing)})"
            lines.append(line)
        left = self.left()
        if left:
            tail = f"Left to read: {compact_tokens(left)} tokens"
            seconds = throughput.seconds(left) if throughput is not None else 0.0
            if seconds >= 60:
                tail += f", ~{seconds / 60:.0f} min at ~{throughput.prefill:.0f} tokens/s"
            lines.append(f"{tail}.")
        lines.append(
            "Work one step at a time; call plan again with the whole list to mark progress."
        )
        return "\n".join(lines)


def plan_line(view: dict[str, Any] | None) -> str:
    """A plan `view` in one line: ``[plan] 1/3 done; now 2. Write``."""
    if not view:
        return "[plan] cleared"
    total, current = int(view.get("total") or 0), int(view.get("current") or 0)
    if not current:
        return f"[plan] all {total} steps done"
    title = view["steps"][current - 1]["title"]
    return f"[plan] {int(view.get('done') or 0)}/{total} done; now {current}. {title}"


def speed_line(prefill: float, decode: float = 0.0) -> str:
    """Measured speeds for people: ``reads ~100 · writes ~20 tokens/s``."""
    parts = [f"reads ~{prefill:.0f}"] if prefill > 0 else []
    parts += [f"writes ~{decode:.0f}"] if decode > 0 else []
    return f"{' · '.join(parts)} tokens/s" if parts else ""


class PlanBook:
    """Plans by session: kept in memory, written through to a `Store`.

    The `Cache` an `Agent` keeps its plans in (`Engine.plans`): every change
    of a plan is written ahead (namespace `plans`, keyed by session), so a
    resumed session, or a process restarted over the same store, continues
    the plan where it stood. `clear` empties the memory only; `delete`
    forgets a plan in both.
    """

    NAMESPACE = "plans"

    def __init__(self, store: Store | None = None, maxsize: int = 256) -> None:
        self.store = store
        self.memory = LruCache(maxsize)

    def get(self, key: str) -> Plan | None:
        plan = self.memory.get(key)
        if plan is None and self.store is not None:
            plan = Plan.from_dict(self.store.get(self.NAMESPACE, key))
            if plan is not None:
                self.memory.set(key, plan)
        return plan

    def set(self, key: str, value: Plan) -> None:
        self.memory.set(key, value)
        if self.store is not None:
            self.store.set(self.NAMESPACE, key, value.to_dict())

    def delete(self, key: str) -> None:
        self.memory.delete(key)
        if self.store is not None:
            self.store.delete(self.NAMESPACE, key)

    def clear(self) -> None:
        self.memory.clear()


class Planner:
    """Sizes the plan of one session against the slice budget, and keeps it.

    `limits` returns the run's `SliceBudget` (``None``: unknown, nothing is
    refused); `store` keeps plans by session (`key`), so the budget note of a
    later call, or run, recites the same plan. An input is a path, a glob or a
    line range (``notes.md:1-400``), resolved by the workspace as a read.
    `ask` holds the thresholds past which a plan asks the user once before it
    is kept (the sizing question, see `update`).
    """

    #: Files a glob or a directory is measured over, at most.
    FILE_LIMIT = 500
    #: Files up to this size are counted; larger ones estimated from their size.
    COUNT_BYTES = 256 * 1024
    #: Bytes counted per plan; past it every input is estimated from its size.
    COUNT_TOTAL = 4 * 1024 * 1024
    #: Bytes read at a time from a line range (a single line may be huge).
    CHUNK = 64 * 1024
    RANGE = re.compile(r"(.+?):(\d+)-(\d+)")

    def __init__(
        self,
        workspace: Workspace,
        counter: TokenCounter,
        limits: Callable[[], SliceBudget | None] | None,
        store: Cache,
        key: str,
        throughput: Throughput | None = None,
        ask: AskLimits | None = None,
        notify: Callable[[dict[str, Any] | None], Awaitable[None]] | None = None,
    ) -> None:
        self.workspace = workspace
        self.counter = counter
        self.limits = limits
        self.store = store
        self.key = key
        self.throughput = throughput
        self.ask = ask or AskLimits()
        #: Awaited with the `view` of the plan after every change (``None``:
        #: cleared), so a front end can show it.
        self.notify = notify
        #: The oversized steps refused last: the same steps sent back are kept
        #: with a warning, not refused for ever (a line cannot be split).
        self.refused: tuple[Any, ...] = ()
        #: Bytes still counted exactly by the plan being measured.
        self.spare = self.COUNT_TOTAL

    @property
    def plan(self) -> Plan | None:
        return self.store.get(self.key)

    def view(self) -> dict[str, Any] | None:
        """The kept plan as a client shows it, or ``None`` without one."""
        plan = self.plan
        if plan is None:
            return None
        return plan.view(self.limits() if self.limits is not None else None, self.throughput)

    async def changed(self) -> None:
        """Tell `notify` the plan changed."""
        if self.notify is not None:
            await self.notify(self.view())

    async def update(
        self,
        items: Iterable[dict[str, Any]],
        *,
        question: str = "",
        options: Iterable[str] = (),
        ask: Callable[[str, list[str]], Awaitable[str]] | None = None,
    ) -> str:
        """Measure, check and keep a plan; the answer for the model.

        A plan that passes the refusal is put to the user once (`ask`, a
        ``(question, options) -> answer`` delegate) when its estimate passes a
        threshold of `self.ask` or the model has a `question` about the split;
        the answer comes back with the plan, and the default ``Stop`` drops it.
        """
        steps = [step for step in map(self.step_of, items) if step is not None]
        if not steps:
            self.store.delete(self.key)
            self.refused = ()
            await self.changed()
            return "Plan cleared."
        await asyncio.to_thread(self.size, steps)
        previous = self.plan  # a plan with every step done is finished: the next is new
        asked = previous is not None and previous.asked and previous.current() is not None
        plan = Plan(steps, asked=asked)
        limits = self.limits() if self.limits is not None else None
        over = [
            step for step in steps
            if not step.done and limits is not None and step.tokens > limits.slice
        ]
        keys = tuple(step.key() for step in over)
        if over and keys != self.refused:
            self.refused = keys
            return self.refusal(plan, over, limits)
        self.refused = ()
        choices = [str(o).strip() for o in options if str(o).strip()]
        answer = await self.consult(plan, limits, question.strip(), choices, ask)
        if answer is not None:
            plan.asked = True
            if not choices and answer.lower() == ASK_STOP.lower():
                self.store.delete(self.key)
                await self.changed()
                return "Plan dropped: the user said stop. Ask them what they want instead."
        self.store.set(self.key, plan)
        await self.changed()
        text = plan.render(limits, self.throughput)
        if over:
            numbers = ", ".join(str(steps.index(step) + 1) for step in over)
            text += (
                f"\nKept as sent, though step {numbers} reads more than one slice: "
                "read it a page at a time."
            )
        if answer is not None:
            text += f"\nThe user answered: {answer}"
        elif question:
            text += (
                "\nThe user was not asked (asked once already, or nobody can answer): "
                "decide the split yourself."
            )
        return text

    async def consult(
        self,
        plan: Plan,
        limits: SliceBudget | None,
        question: str,
        options: list[str],
        ask: Callable[[str, list[str]], Awaitable[str]] | None,
    ) -> str | None:
        """The sizing question: put once per plan, when its estimate passes a
        threshold or the model asks about the split. The answer, or ``None``
        when nobody was asked."""
        if ask is None or plan.asked:
            return None
        estimate = plan.estimate(limits, self.throughput, self.ask.price)
        passed = self.ask.passed(estimate)
        if not passed and not question:
            return None
        head = "This plan is large" if passed else "Plan"
        count = len(plan.steps)
        text = f"{head}: {count} step{'s' if count != 1 else ''}, {estimate.describe()}."
        text += f"\n\n{question}" if question else " Go ahead?"
        answer = (await ask(text, options or [ASK_GO, ASK_STOP])).strip()
        return answer or "(the user did not answer)"

    def mark(self, number: int, status: str, title: str = "") -> PlanStep | None:
        """Set the status of step `number` (1-based) of the kept plan; the
        change is written through like any other. ``None``: no such step, or
        (with `title`) the plan was replaced and step `number` is another."""
        plan = self.plan
        if plan is None or not 1 <= number <= len(plan.steps) or status not in PLAN_STATUSES:
            return None
        step = plan.steps[number - 1]
        if title and step.title != title:
            return None
        step.status = status
        self.store.set(self.key, plan)
        return step

    @staticmethod
    def step_of(item: Any) -> PlanStep | None:
        data = dict(item) if isinstance(item, dict) else {}
        title = str(data.get("title") or "").strip()
        if not title:
            return None
        status = str(data.get("status") or "pending").strip().lower()
        inputs = [str(raw).strip() for raw in data.get("inputs") or [] if str(raw).strip()]
        return PlanStep(title, inputs, status if status in PLAN_STATUSES else "pending")

    def size(self, steps: list[PlanStep]) -> None:
        """Measure the inputs of every step not done yet."""
        self.spare = self.COUNT_TOTAL
        for step in steps:
            if not step.done:
                step.sizes, step.missing = self.measure_all(step.inputs)

    def sizes(self, inputs: Iterable[str]) -> tuple[list[tuple[str, int]], list[str]]:
        """Measure `inputs` afresh: the tokens of each found, and those not found."""
        self.spare = self.COUNT_TOTAL
        return self.measure_all(inputs)

    def measure_all(self, inputs: Iterable[str]) -> tuple[list[tuple[str, int]], list[str]]:
        found: list[tuple[str, int]] = []
        missing: list[str] = []
        for spec in inputs:
            tokens = self.measure(spec)
            if tokens is None:
                missing.append(spec)
            else:
                found.append((spec, tokens))
        return found, missing

    def chunks(
        self, inputs: Iterable[str], limit: int, most: int = 0
    ) -> tuple[list[tuple[str, int]], list[str]]:
        """Cut `inputs` at line boundaries into parts of at most `limit`
        tokens (provider units): ``path:A-B`` with the tokens of each, and the
        inputs not found.

        A file that fits whole is named alone, a line longer than `limit` is a
        part of its own and an image is one part. Cutting stops once there are
        more than `most` parts (0: no bound), so a giant input is not read to
        the end only to be refused.
        """
        self.spare = self.COUNT_TOTAL
        bound = max(self.counter.raw(limit), 1)
        parts: list[tuple[str, int]] = []
        missing: list[str] = []
        for spec in inputs:
            text = spec.strip().strip("\"'")
            start = end = 0
            match = self.RANGE.fullmatch(text)
            if match:
                text, start, end = match.group(1), int(match.group(2)), int(match.group(3))
            files = self.expand(text)
            if not files:
                missing.append(spec)
                continue
            for path in files:
                parts.extend(self.cut(path, bound, start, end, most - len(parts) if most else None))
                if most and len(parts) > most:
                    return parts, missing
        return parts, missing

    def cut(
        self, path: Path, bound: int, start: int = 0, end: int = 0, most: int | None = None
    ) -> list[tuple[str, int]]:
        """The parts of one file (or of its lines `start`-`end`) of at most
        `bound` uncalibrated tokens each; past `most` parts (``None``: no
        bound) it stops reading."""
        name = self.display(path)
        if guess_mime(path).startswith("image/"):
            return [(name, self.counter.scaled(TokenCounter.IMAGE_TOKENS))]
        spans: list[tuple[int, int, int]] = []
        first = tokens = line = last = 0
        number, open_line = 1, False

        def close(number: int, line: int) -> None:
            nonlocal first, tokens, last
            if first and tokens + line > bound:
                spans.append((first, last, tokens))
                first, tokens = 0, 0
            first = first or number
            tokens += line
            last = number

        with path.open("rb") as handle:
            while (not end or number <= end) and (most is None or len(spans) <= most):
                piece = handle.readline(self.CHUNK)
                if not piece:
                    break
                inside = number >= max(start, 1)
                if inside:
                    line += self.piece_tokens(piece)
                    open_line = True
                if piece.endswith(b"\n"):
                    if inside:
                        close(number, line)
                    number, line, open_line = number + 1, 0, False
        if open_line:
            close(number, line)
        if first and (most is None or len(spans) <= most):
            spans.append((first, last, tokens))
        if len(spans) == 1 and not start:
            return [(name, self.counter.scaled(spans[0][2]))]
        return [(f"{name}:{a}-{b}", self.counter.scaled(size)) for a, b, size in spans]

    def piece_tokens(self, piece: bytes) -> int:
        """Tokens of a piece of a file: counted while the spare bytes last,
        else estimated from its size."""
        if len(piece) <= self.spare:
            self.spare -= len(piece)
            return self.counter.text(piece.decode("utf-8", "replace"))
        return -(-len(piece) // TokenCounter.BYTES_PER_TOKEN)

    def display(self, path: Path) -> str:
        """`path` as a worker names it: relative to the workspace when inside it."""
        try:
            return path.relative_to(self.workspace.root).as_posix()
        except ValueError:
            return str(path)

    def refusal(self, plan: Plan, over: list[PlanStep], limits: SliceBudget) -> str:
        lines = [
            f"Plan not kept: {len(over)} step(s) read more than one slice "
            f"({compact_tokens(limits.slice)} tokens):"
        ]
        for step in over:
            parts = ", ".join(f"{name} {compact_tokens(size)}" for name, size in step.sizes)
            number = plan.steps.index(step) + 1
            lines.append(f"- {number}. {step.title}: ~{compact_tokens(step.tokens)} ({parts})")
        lines.append(
            "Split them by file or by line range ('path:1-400') and call plan again."
        )
        return "\n".join(lines)

    def measure(self, spec: str) -> int | None:
        """Tokens one input reads (provider units), ``None`` when it is not there."""
        text = spec.strip().strip("\"'")
        start = end = 0
        match = self.RANGE.fullmatch(text)
        if match:
            text, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        files = self.expand(text)
        if not files:
            return None
        return self.counter.scaled(sum(self.file_tokens(path, start, end) for path in files))

    def expand(self, text: str) -> list[Path]:
        """The files an input names: a file, the files of a directory or a glob.

        A glob is walked lazily from the root (never read as a pattern itself)
        and stops at `FILE_LIMIT`; unless the workspace reads outside, it may
        not be absolute or climb with ``..``, and a match or a directory entry
        that resolves outside is skipped, not reported, so nothing outside is
        revealed. A plain path is resolved like any read.
        """
        ws = self.workspace
        if any(char in text for char in "*?["):
            pattern = Path(text).expanduser()
            if not ws.outside_read and (pattern.is_absolute() or ".." in pattern.parts):
                raise WorkspaceError(f"a glob must stay inside the workspace: {text}")
            found = (
                glob.iglob(str(pattern), recursive=True)
                if pattern.is_absolute()
                else glob.iglob(text, root_dir=ws.root, recursive=True)
            )
            paths = [
                path
                for path in (self.inside(raw) for raw in itertools.islice(found, self.FILE_LIMIT))
                if path is not None
            ]
        else:
            paths = [ws.resolve(text, read=True)]
        files: list[Path] = []
        for path in paths:
            if path.is_file():
                files.append(path)
            elif path.is_dir():
                for child in path.rglob("*"):
                    inner = self.inside(str(child))
                    if inner is not None and inner.is_file():
                        files.append(inner)
                    if len(files) >= self.FILE_LIMIT:
                        break
            if len(files) >= self.FILE_LIMIT:
                break
        return files

    def inside(self, raw: str) -> Path | None:
        """`raw` resolved as a read, or ``None`` where the workspace may not read."""
        try:
            return self.workspace.resolve(raw, read=True)
        except WorkspaceError:
            return None

    def file_tokens(self, path: Path, start: int = 0, end: int = 0) -> int:
        """Tokens of a file, or of its lines `start`-`end`, uncalibrated.

        Text is counted exactly while the plan's `spare` bytes last (and a
        whole file only up to `COUNT_BYTES`); the rest is estimated from its
        size, so a giant file or a glob over many costs a read, not a tokenizer.
        """
        if guess_mime(path).startswith("image/"):
            return TokenCounter.IMAGE_TOKENS
        if start:
            return self.range_tokens(path, start, end)
        size = path.stat().st_size
        if size > min(self.COUNT_BYTES, self.spare):
            return -(-size // TokenCounter.BYTES_PER_TOKEN)
        self.spare -= size
        return self.counter.text(path.read_text(encoding="utf-8", errors="replace"))

    def range_tokens(self, path: Path, start: int, end: int) -> int:
        """Tokens of lines `start`-`end`, read a bounded chunk at a time."""
        kept: list[bytes] = []
        counted = skipped = 0
        number = 1
        with path.open("rb") as handle:
            while number <= end:
                chunk = handle.readline(self.CHUNK)
                if not chunk:
                    break
                if number >= start:
                    size = len(chunk)
                    if counted + size <= self.COUNT_BYTES and size <= self.spare:
                        kept.append(chunk)
                        counted += size
                        self.spare -= size
                    else:
                        skipped += size
                if chunk.endswith(b"\n"):
                    number += 1
        text = b"".join(kept).decode("utf-8", "replace")
        return self.counter.text(text) + -(-skipped // TokenCounter.BYTES_PER_TOKEN)


class Folder:
    """Context folding for one run: `branch` opens a sub-task, `return` folds it.

    The steps between a `branch` and its `return` are hidden from the next
    model call on (`ContextState.folds`, applied by `Compactor.hidden`); the
    `return` call, which carries the model's own summary of the sub-task,
    stands for them. Branches nest: `return` closes the innermost one open in
    this run. `context` returns the run's `ContextRun` (built after the tools).
    """

    def __init__(self, context: Callable[[], "ContextRun | None"]) -> None:
        self.context = context
        #: The branches open in this run, innermost last: (call id, goal).
        self.open: list[tuple[str, str]] = []

    def branch(self, call_id: str | None, goal: str) -> str:
        goal = " ".join(goal.split())
        if not goal:
            return "Error: branch needs a goal"
        self.open.append((str(call_id or ""), goal))
        return (
            f"Branch opened: {goal}. Work on it, then call return with what it found: "
            "every step from here to that call is folded out of your context and the "
            "summary stands for them."
        )

    def back(self, call_id: str | None, summary: str) -> str:
        if not self.open:
            return "Error: no branch is open in this run"
        if not summary.strip():
            return "Error: return needs a summary of what the branch found"
        context = self.context()
        if context is None:
            return "Error: folding needs context compaction (context_trigger > 0)"
        branch, goal = self.open.pop()
        context.state.folds[str(call_id or "")] = branch
        tokens = self.size(context, branch)
        return (
            f"Folded the branch '{goal}': ~{compact_tokens(tokens)} tokens of its steps are "
            "hidden from the next call on; your summary in this call stands for them."
        )

    @staticmethod
    def size(context: "ContextRun", branch: str) -> int:
        """Tokens the steps after `branch` take in the newest input (provider units)."""
        items = context.items
        opened = next(
            (
                index for index, item in enumerate(items)
                if is_output(item_kind(item)) and str(item.get("call_id") or "") == branch
            ),
            None,
        )
        if opened is None:
            return 0
        start = next((cut for cut in safe_cuts(items) if cut > opened), len(items))
        compactor = context.compactor
        return context.counter.scaled(
            sum(compactor.cost(item, context.state) for item in items[start:])
        )


class ToolRegistry:
    """Builds the model facing tool list for a workspace.

    Consumers add or replace tools with `register`; each factory receives the
    `Workspace` and returns an ``openai-agents`` tool. Tools may delegate to a
    specialist model (see the `vision` argument of `build`).
    """

    def __init__(self, *, defaults: bool = True, shell: bool = True) -> None:
        self._factories: dict[str, Callable[[Workspace], Any]] = {}
        self._defaults = defaults
        self._shell = shell

    def register(self, name: str, factory: Callable[[Workspace], Any]) -> None:
        self._factories[name] = factory

    def unregister(self, name: str) -> None:
        self._factories.pop(name, None)

    def build(
        self,
        workspace: Workspace,
        *,
        vision: Callable[[str, str], Awaitable[str]] | None = None,
        checkout: "Checkout | None" = None,
        progress: ToolReporter | None = None,
        ask: Asker | None = None,
        read_limit: int | Callable[[], int] = 0,
        planner: "Planner | None" = None,
        delegate: Delegator | None = None,
        mapper: Mapper | None = None,
        folder: Folder | None = None,
    ) -> list[Any]:
        """Build the tools for `workspace`.

        `vision` is an optional ``(data_url, question) -> text`` delegate. When
        it is given, images are inspected by the vision model and only its text
        answer reaches the leader, so the leader needs no vision of its own.
        `checkout` is the session's git checkout; when it is given the leader
        also gets the tools that publish its work. `progress` receives the
        ``(call_id, text)`` a long running tool reports while it works. `ask`
        puts a question to the person behind the run; when it is given the
        leader gets `ask_user`. `read_limit` is the characters one
        `read_text_file` call returns (0: whole files), or a callable asked
        on every read when the budget moves during a run. `planner` sizes the
        plan of the session; when it is given the leader gets `plan`, which
        puts its sizing question through `ask`. `delegate` runs one step in a
        fresh context; when it is given the leader gets `delegate`. `mapper`
        runs one task over the parts of some inputs; the leader gets `map`.
        `folder` folds sub-tasks out of the context; the leader gets `branch`
        and `return`.
        """
        tools = (
            list(self._build_defaults(workspace, vision, progress, read_limit))
            if self._defaults
            else []
        )
        if checkout is not None:
            tools.extend(self._build_repo_tools(checkout))
        if ask is not None:
            tools.append(self._build_ask_tool(ask))
        if planner is not None:
            tools.append(self._build_plan_tool(planner, ask))
        if delegate is not None:
            tools.append(self._build_delegate_tool(delegate))
        if mapper is not None:
            tools.append(self._build_map_tool(mapper))
        if folder is not None:
            tools.extend(self._build_fold_tools(folder))
        tools.extend(factory(workspace) for factory in self._factories.values())
        return tools

    @staticmethod
    def guard(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        """Turn workspace and repository errors into model readable text."""

        @functools.wraps(fn)  # keeps the signature the tool schema needs
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return await fn(*args, **kwargs)
            except (WorkspaceError, RepoError, OSError, ValueError) as exc:
                return f"Error: {exc}"

        return wrapper

    def _build_repo_tools(self, checkout: "Checkout") -> Iterable[Any]:
        """Tools that let the leader publish the work in its git checkout."""
        from agents import function_tool

        guard = self.guard

        @function_tool(
            name_override="git_status",
            description_override=(
                "Show the state of the git checkout of this session: its branch "
                "and the files that changed."
            ),
        )
        @guard
        async def git_status() -> str:
            return (
                f"Repository {checkout.url}\nBranch {checkout.branch} "
                f"(base {checkout.base})\n{await checkout.status()}"
            )

        @function_tool(
            name_override="git_commit",
            description_override=(
                "Stage every change in the git checkout and commit it with the "
                "given message."
            ),
        )
        @guard
        async def git_commit(message: str) -> str:
            return await checkout.commit(message)

        @function_tool(
            name_override="git_push",
            description_override=(
                "Push the session branch of the git checkout to the remote."
            ),
        )
        @guard
        async def git_push() -> str:
            return await checkout.push()

        @function_tool(
            name_override="open_pull_request",
            description_override=(
                "Open a pull request from the session branch onto the base "
                "branch and return its URL. Push the branch first."
            ),
        )
        @guard
        async def open_pull_request(title: str, body: str = "") -> str:
            pull = await checkout.pull_request(title, body)
            return f"Opened pull request #{pull['number']}: {pull['url']}"

        @function_tool(
            name_override="publish_work",
            description_override=(
                "Finish the task: commit every change, push the session branch "
                "and open a pull request. Returns the pull request URL."
            ),
        )
        @guard
        async def publish_work(message: str, title: str = "", body: str = "") -> str:
            result = await checkout.publish(message, title, body)
            pull = result["pull_request"]
            return f"Published {result['commit']} as pull request #{pull['number']}: {pull['url']}"

        return [git_status, git_commit, git_push, open_pull_request, publish_work]

    def _build_plan_tool(self, planner: "Planner", ask: Asker | None = None) -> Any:
        """The tool that lets the leader plan work too large for one pass.

        With `ask` the plan may put its sizing question to the user, on the
        block of its own call.
        """
        from agents import function_tool
        from agents.tool_context import ToolContext

        @self.guard
        async def plan(
            ctx: Any, steps: list[PlanItem], question: str = "", options: list[str] | None = None
        ) -> str:
            asking = functools.partial(ask, ctx.tool_call_id) if ask is not None else None
            return await planner.update(steps, question=question, options=options or [], ask=asking)

        plan.__annotations__["ctx"] = ToolContext  # as for the shell: context, not schema
        tool = function_tool(
            plan,
            name_override="plan",
            description_override=(
                "Plan work too large for one pass: reading many files or a long "
                "one, or writing a long document. List every step with a short "
                "title, the inputs it reads (paths, globs or line ranges such as "
                "'notes.md:1-400') and its status (pending, active or done). The "
                "harness measures each step and refuses one that reads more than "
                "one slice (the [budget] note says how much); split it by file or "
                "by line range. Call it again with the whole list to mark progress; "
                "an empty list clears the plan. When the split depends on what the "
                "user wants, put one 'question' (and 'options') on the call instead "
                "of calling ask_user; a large plan asks the user on its own, once."
            ),
        )
        # inlined: some local servers and small models do not follow `$ref`
        schema = tool.params_json_schema
        item = schema.pop("$defs", {}).get("PlanItem")
        if item is not None:
            schema["properties"]["steps"]["items"] = item
        return tool

    def _build_delegate_tool(self, delegate: Delegator) -> Any:
        """The tool that runs one step of the work in a fresh context."""
        from agents import function_tool
        from agents.tool_context import ToolContext

        @self.guard
        async def delegate_step(
            ctx: Any,
            task: str,
            inputs: list[str] | None = None,
            step: int = 0,
            tools: list[str] | None = None,
        ) -> str:
            return await delegate(
                ctx.tool_call_id, task, list(inputs or []), int(step or 0), list(tools or [])
            )

        delegate_step.__annotations__["ctx"] = ToolContext
        return function_tool(
            delegate_step,
            name_override="delegate",
            description_override=(
                "Hand one step of the work to a worker that runs it in a fresh "
                "context with the workspace tools and returns only its answer, "
                "capped, so what it reads never fills your context. Use it for a "
                "step that reads a lot and answers briefly: summarise, search, "
                "review or transform files. 'task' must say everything the worker "
                "needs: it sees nothing else. 'inputs' are what it reads (paths, "
                "globs or line ranges, measured like plan inputs). 'step' is the "
                "number of a plan step: its inputs are used when you give none, and "
                "it is marked done when the worker finishes. 'tools' names the only "
                "tools the worker gets (default: all workspace tools; branch and "
                "return are always kept)."
            ),
        )

    def _build_map_tool(self, mapper: Mapper) -> Any:
        """The tool that runs one task over the parts of inputs too large for one worker."""
        from agents import function_tool
        from agents.tool_context import ToolContext

        @self.guard
        async def map_parts(
            ctx: Any,
            task: str,
            inputs: list[str] | None = None,
            chunk_tokens: int = 0,
            step: int = 0,
            tools: list[str] | None = None,
        ) -> str:
            return await mapper(
                ctx.tool_call_id,
                task,
                list(inputs or []),
                int(chunk_tokens or 0),
                int(step or 0),
                list(tools or []),
            )

        map_parts.__annotations__["ctx"] = ToolContext
        return function_tool(
            map_parts,
            name_override="map",
            description_override=(
                "Run the same task over inputs too large for one worker: the harness "
                "cuts them at line boundaries into parts a worker can read, runs one "
                "worker per part in a fresh context and returns every part's answer "
                "in order, each kept short so together they fit one slice. Use it to "
                "summarise, search or extract across a long file or many files. 'task' "
                "must say everything a worker needs and what to answer: each sees only "
                "its part. 'inputs' are paths, globs or line ranges; 'chunk_tokens' "
                "caps a part (default: what a worker may read). 'step' names a plan "
                "step: its inputs are used when you give none, and it is marked done. "
                "'tools' names the only tools the workers get (branch and return "
                "are always kept)."
            ),
        )

    def _build_fold_tools(self, folder: Folder) -> list[Any]:
        """The tools that fold a finished sub-task out of the context."""
        from agents import function_tool
        from agents.tool_context import ToolContext

        @self.guard
        async def branch(ctx: Any, goal: str) -> str:
            return folder.branch(ctx.tool_call_id, goal)

        @self.guard
        async def return_branch(ctx: Any, summary: str) -> str:
            return folder.back(ctx.tool_call_id, summary)

        branch.__annotations__["ctx"] = ToolContext
        return_branch.__annotations__["ctx"] = ToolContext
        return [
            function_tool(
                branch,
                name_override="branch",
                description_override=(
                    "Open a sub-task whose steps will not matter once it is done: "
                    "searching, reading around, trying things. Work on it, then call "
                    "return with what it found: every step between the two calls is "
                    "folded out of your context, and only the goal and the summary "
                    "stay. Branches nest."
                ),
            ),
            function_tool(
                return_branch,
                name_override="return",
                description_override=(
                    "Close the innermost open branch. 'summary' must hold everything "
                    "later steps need from it (findings, paths, decisions, open "
                    "problems): the branch's steps are folded out of your context."
                ),
            ),
        ]

    def _build_ask_tool(self, ask: Asker) -> Any:
        """The tool that lets the leader ask the person behind the run."""
        from agents import function_tool
        from agents.tool_context import ToolContext

        @self.guard
        async def ask_user(ctx: Any, question: str, options: list[str] | None = None) -> str:
            choices = [str(option).strip() for option in options or [] if str(option).strip()]
            answer = (await ask(ctx.tool_call_id, question.strip(), choices)).strip()
            return answer or "(the user did not answer)"

        # As for the shell: the class marks `ctx` as context, out of the schema.
        ask_user.__annotations__["ctx"] = ToolContext
        return function_tool(
            ask_user,
            name_override="ask_user",
            description_override=(
                "Ask the user a question and wait for the answer. Use it when the "
                "request is ambiguous or a decision is theirs to make, not for "
                "anything you can find out yourself. 'options' are suggested "
                "answers shown as choices; the user may still answer in words."
            ),
        )

    # NOTE: the hosted tools of openai-agents (ApplyPatchTool, ShellTool, ...)
    # require the OpenAI Responses API. Chat Completions backends such as
    # OpenRouter only support plain function tools, so we implement our own.
    def _build_defaults(
        self,
        ws: Workspace,
        vision: Callable[[str, str], Awaitable[str]] | None = None,
        progress: ToolReporter | None = None,
        read_limit: int | Callable[[], int] = 0,
    ) -> Iterable[Any]:
        from agents import function_tool
        from agents.tool_context import ToolContext

        guard = self.guard
        repeats = ReadRepeats()
        reach = " Paths outside the workspace may be read too." if ws.outside_read else ""
        write_reach = (
            " Paths outside the workspace may be written too." if ws.outside_write else ""
        )

        @function_tool(
            name_override="list_directory",
            description_override=(
                "List files and subdirectories of a workspace directory. Use "
                "relative paths such as '.' or 'notes'. One entry per line." + reach
            ),
        )
        @guard
        async def list_directory(path: str = ".") -> str:
            entries = await asyncio.to_thread(ws.list_dir, path)
            return "\n".join(entries) if entries else "(empty)"

        @function_tool(
            name_override="read_text_file",
            description_override=(
                "Read a UTF-8 text file from the workspace and return its contents. "
                "A long file comes back a page of whole lines at a time, ending with "
                "'[lines A-B of N ...; continue with offset=B+1]'. Pass 'offset' "
                "(first line, 1-based) and 'limit' (line count, 0 for as many as "
                "fit) to read a range." + reach
            ),
        )
        @guard
        async def read_text_file(path: str, offset: int = 1, limit: int = 0) -> str:
            key = (path.strip(), max(int(offset or 1), 1), max(int(limit or 0), 0))
            refused = repeats.check(key)
            if refused:
                return refused
            chars = read_limit() if callable(read_limit) else read_limit
            page = await asyncio.to_thread(ws.read_page, path, offset, limit, chars)
            repeats.record(key, page.footer)
            return page.render()

        @function_tool(
            name_override="write_text_file",
            description_override=(
                "Write text to a workspace file, creating parent directories as "
                "needed. Overwrites the file when it already exists." + write_reach
            ),
        )
        @guard
        async def write_text_file(path: str, content: str) -> str:
            repeats.forget()
            written = await asyncio.to_thread(ws.write_text, path, content)
            return f"Wrote {len(content)} characters to {written}"

        @function_tool(
            name_override="view_image",
            description_override=(
                "Read an image from the workspace and return it as a base64 data "
                "URL so it can be inspected." + reach
            ),
        )
        @guard
        async def view_image(path: str) -> str:
            return await asyncio.to_thread(ws.read_data_url, path)

        @function_tool(
            name_override="describe_image",
            description_override=(
                "Look at a workspace image with a vision model and return what it "
                "sees as text. Pass 'question' to ask about something specific, "
                "for example 'what text appears in this screenshot?'." + reach
            ),
        )
        @guard
        async def describe_image(path: str, question: str = "") -> str:
            assert vision is not None
            data_url = await asyncio.to_thread(ws.read_data_url, path)
            try:
                answer = await vision(data_url, question)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the specialist must never break the leader
                return f"Error: the vision model failed: {exc}"
            return answer or "(the vision model returned nothing)"

        image_tool = describe_image if vision is not None else view_image

        @function_tool(
            name_override="apply_patch",
            description_override=(
                "Apply a unified diff to a workspace file. operation is one of "
                "'create_file', 'update_file' or 'delete_file'." + write_reach
            ),
        )
        @guard
        async def apply_patch(
            path: str, diff: str, operation: str = "update_file"
        ) -> str:
            repeats.forget()
            return await asyncio.to_thread(ws.apply_patch, path, diff, operation)

        tools = [list_directory, read_text_file, write_text_file, image_tool, apply_patch]

        if self._shell and not ws.shell_allowed:
            self.withheld("no sandbox is available to hold it to the workspace")
        if self._shell and ws.shell_allowed:
            confinement = (
                ws.sandbox.describe(ws) if ws.sandbox is not None and ws.confined else ""
            )

            @guard
            async def run_shell_command(ctx: Any, command: str) -> str:
                repeats.forget()
                report = functools.partial(progress, ctx.tool_call_id) if progress else None
                return await ws.run_shell(command, report)

            # Postponed annotations would name the context type as a string the
            # SDK cannot resolve here; the class itself marks `ctx` as context,
            # which keeps it out of the schema and carries the id of the call.
            run_shell_command.__annotations__["ctx"] = ToolContext
            tools.append(
                function_tool(
                    run_shell_command,
                    name_override="run_shell_command",
                    description_override=(
                        "Run a shell command with the workspace as the working "
                        "directory and return stdout, stderr and the exit code."
                        + confinement
                    ),
                )
            )

        return tools

    #: Reasons the shell was withheld that were already reported.
    _withheld: set[str] = set()

    @classmethod
    def withheld(cls, reason: str) -> None:
        """Report, once per reason, that the shell tool was left out."""
        if reason in cls._withheld:
            return
        cls._withheld.add(reason)
        print(
            f"[shell] run_shell_command withheld: outside_read / outside_write confine "
            f"it and {reason} (see shell_sandbox)",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Console
#
# The default renderer is only an event subscriber: consumers can subclass it,
# swap it out, or drop it entirely and subscribe their own.
# ---------------------------------------------------------------------------


class ConsoleRenderer:
    """Streams agent block events to a terminal."""

    STYLES = {
        "reasoning": "\x1b[2;3m",
        "tool": "\x1b[2m",
        "error": "\x1b[31m",
        "title": "\x1b[1;36m",
        "question": "\x1b[1m",
    }
    RESET = "\x1b[0m"
    #: Seconds streamed text is gathered for before a terminal is written to.
    FRAME = 0.05

    def __init__(
        self,
        *,
        color: bool | None = None,
        stream: Any = None,
        banners: bool = True,
        frame: float | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        tty = bool(getattr(self.stream, "isatty", lambda: False)())
        if color is None:
            color = tty and os.environ.get("NO_COLOR") is None
        self.color = color
        #: Print the config banner when a run starts. A repl prints its own once
        #: and turns this off, so a banner is not repeated on every prompt.
        self.banners = banners
        #: Streamed text is written at most once per frame instead of once per
        #: token: a terminal that redraws on every token (VS Code over ConPTY)
        #: falls behind a fast model, and a Ctrl-C typed meanwhile waits for it
        #: to catch up. ``None`` frames a terminal only; ``0`` writes at once.
        self.frame = (self.FRAME if tty else 0.0) if frame is None else frame
        self._frame_kind: str | None = None
        self._frame_text: list[str] = []
        self._frame_timer: asyncio.TimerHandle | None = None
        self._prev_kind: str | None = None
        self._trailing_newlines = 0
        #: The newest `context.usage` of the run, printed when it ends.
        self._usage: dict[str, Any] | None = None

    # -- overridable output -------------------------------------------------

    def write(self, text: str, end: str = "") -> None:
        print(text, end=end, file=self.stream, flush=True)

    def style(self, kind: str, text: str) -> str:
        code = self.STYLES.get(kind, "") if self.color else ""
        return f"{code}{text}{self.RESET}" if code else text

    # -- subscription -------------------------------------------------------

    def attach(self, bus: EventBus) -> Callable[[], None]:
        """Subscribe to a bus; returns an unsubscriber."""
        return bus.on(EventType.ALL, self.handle)

    def handle(self, event: Event) -> None:
        if event.data.get("parent"):
            self.nested(event)
            return
        kind = event.data.get("kind", "output")
        if event.type == EventType.BLOCK_DELTA:
            self.gather(kind, event.data.get("text", ""))
            return
        self.flush()
        if event.type == EventType.AGENT_START:
            config = event.data.get("config")
            if self.banners and isinstance(config, AgentConfig):
                self.banner(config.name, config.banner_items())
        elif event.type == EventType.TOOL_START:
            self.emit("tool", f"-> {event.data.get('text', 'tool')}\n")
        elif event.type == EventType.TOOL_PROGRESS:
            if not event.data.get("text"):
                self.emit("tool", ".")  # a heartbeat: the call is still alive
        elif event.type == EventType.TOOL_END:
            beats = self._prev_kind == "tool" and not self._trailing_newlines
            self.emit("tool", ("\n" if beats else "") + self.tool_outcome(event.data))
        elif event.type == EventType.MODEL_RETRY:
            self.emit("error", self.retry_notice(event.data))
        elif event.type == EventType.CONTEXT_USAGE:
            self._usage = dict(event.data)
        elif event.type == EventType.CONTEXT_COMPACT:
            self.emit("tool", f"~~ {event.data.get('message') or 'compacted'}\n")
        elif event.type == EventType.PLAN_UPDATE:
            self.emit("tool", f"{plan_line(event.data.get('plan'))}\n")
        elif event.type == EventType.AGENT_ERROR:
            self.emit("error", str(event.data.get("error", "")))
        elif event.type == EventType.AGENT_END:
            if self._usage:
                self.emit("tool", self.usage_line(self._usage))
                self._usage = None
            if self._trailing_newlines == 0:
                self.write("\n")
            self._prev_kind = None
            self._trailing_newlines = 0

    def nested(self, event: Event) -> None:
        """A worker's step, indented under the call that started it by its
        `depth`: its calls with their outcome, its compactions and its answer.
        What it streams is left out, so parallel workers never interleave
        within a line."""
        data = event.data
        if event.type == EventType.TOOL_START:
            lines = [f"-> {data.get('text') or data.get('name') or 'tool'}"]
        elif event.type == EventType.TOOL_END:
            lines = [self.tool_outcome(data).split("\n", 1)[0]]
        elif event.type == EventType.CONTEXT_COMPACT:
            lines = [f"~~ {data.get('message') or 'compacted'}"]
        elif event.type == EventType.BLOCK_END and data.get("kind") == "output":
            lines = str(data.get("text") or "").strip().splitlines()
        else:
            return
        if not lines:
            return
        self.flush()
        prefix = "  " * max(int(data.get("depth") or 1), 1) + "| "
        lead = "\n" if self._prev_kind is not None and not self._trailing_newlines else ""
        self.emit("tool", lead + "".join(f"{prefix}{line}\n" for line in lines))

    @staticmethod
    def usage_line(usage: dict[str, Any]) -> str:
        """The context gauge as a line: ``ctx 45% · 58k / 102k``."""
        fill = float(usage.get("fill") or 0.0)
        tokens = compact_tokens(int(usage.get("tokens") or 0))
        usable = compact_tokens(int(usage.get("usable") or 0))
        speeds = speed_line(float(usage.get("prefill") or 0), float(usage.get("decode") or 0))
        return f"\nctx {fill:.0%} · {tokens} / {usable}{f' · {speeds}' if speeds else ''}\n"

    @staticmethod
    def retry_notice(data: dict[str, Any]) -> str:
        """The line printed when a model call is about to be tried again."""
        role = data.get("role", LEADER_ROLE)
        kind = data.get("error_kind", "error")
        attempt = int(data.get("attempt", 1)) + 1
        attempts = int(data.get("attempts", attempt))
        delay = float(data.get("delay") or 0.0)
        return f"!! {role} model {kind}: retry {attempt}/{attempts} in {delay:.1f}s\n"

    @staticmethod
    def tool_outcome(data: dict[str, Any]) -> str:
        """The closing line of a tool call: its status, duration and result."""
        status = "ok" if data.get("ok", True) else "failed"
        duration = float(data.get("duration") or 0.0) * 1000
        line = f"<- {data.get('name', 'tool')}: {status} in {duration:.0f} ms"
        body = str(data.get("result") or "")
        return f"{line}\n{body}\n" if body else f"{line}\n"

    def gather(self, kind: str, text: str) -> None:
        """Queue streamed text for the current frame, or print it without one."""
        if not text:
            return
        loop = None
        if self.frame > 0:
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
        if loop is None:
            self.emit(kind, text)
            return
        if self._frame_kind not in (None, kind):
            self.flush()
        self._frame_kind = kind
        self._frame_text.append(text)
        if self._frame_timer is None:
            self._frame_timer = loop.call_later(self.frame, self.flush)

    def flush(self) -> None:
        """Print the text gathered for the current frame."""
        if self._frame_timer is not None:
            self._frame_timer.cancel()
            self._frame_timer = None
        if not self._frame_text:
            return
        kind, text = self._frame_kind or "output", "".join(self._frame_text)
        self._frame_kind = None
        self._frame_text.clear()
        self.emit(kind, text)

    def emit(self, kind: str, text: str) -> None:
        """Print streamed text, keeping one blank line between blocks."""
        if not text:
            return
        self.flush()  # anything printed directly comes after the gathered text
        if self._prev_kind is not None and kind != self._prev_kind:
            needed = 2 - min(self._trailing_newlines, 2)
            if needed > 0:
                self.write("\n" * needed)
            text = text.lstrip("\n")
            self._trailing_newlines = 0
            if not text:
                self._prev_kind = kind
                return
        self.write(self.style(kind, text))
        self._trailing_newlines = min(len(text) - len(text.rstrip("\n")), 2)
        self._prev_kind = kind

    # -- banner -------------------------------------------------------------

    def banner(self, title: str, items: list[tuple[str, str]], width: int = 60) -> None:
        """Print a title and key/value rows inside an ASCII box."""
        items = items or [("", "")]
        label_w = max(len(label) for label, _ in items)
        value_w = max(1, width - label_w - 4)
        rows = [
            f"{label:<{label_w}} -> {truncate(value, value_w)}" for label, value in items
        ]
        title = truncate(title, width)
        inner = max([len(title), *(len(row) for row in rows)])
        border = "-" * (inner + 2)
        self.write(f"+{border}+\n")
        self.write(f"  {self.style('title', f'{title:<{inner}}')}\n")
        self.write(f"+{border}+\n")
        for row in rows:
            self.write(f"  {row}\n")
        self.write(f"+{border}+\n")


def truncate(text: str, max_len: int) -> str:
    """Truncate text; paths keep their tail, everything else keeps its head."""
    if max_len <= 3:
        return text[:max_len]
    if len(text) <= max_len:
        return text
    keep = max_len - 3
    if "/" in text or "\\" in text:
        return "..." + text[-keep:]
    return text[:keep] + "..."


# ---------------------------------------------------------------------------
# Commands
#
# Slash commands power everything the discussion container does not, on both
# the CLI and the web client.
# ---------------------------------------------------------------------------

COMMAND_RE = re.compile(r"^/(?P<name>[a-zA-Z][\w-]*)\s*(?P<args>.*)$", re.DOTALL)


@dataclass
class Command:
    name: str
    description: str
    handler: Callable[..., Any]


class CommandRegistry:
    """Registry of slash commands, keyed by name."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, name: str, description: str, handler: Callable[..., Any]) -> None:
        self._commands[name] = Command(name, description, handler)

    def unregister(self, name: str) -> None:
        self._commands.pop(name, None)

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def describe(self) -> list[dict[str, str]]:
        return [
            {"name": c.name, "description": c.description}
            for c in sorted(self._commands.values(), key=lambda c: c.name)
        ]

    @staticmethod
    def parse(text: str) -> tuple[str, str] | None:
        """Split ``/name args`` into ``(name, args)``; ``None`` when not a command."""
        match = COMMAND_RE.match((text or "").strip())
        if not match:
            return None
        return match.group("name").lower(), match.group("args").strip()

    async def invoke(self, text: str, **context: Any) -> Any:
        parsed = self.parse(text)
        if parsed is None:
            return None
        name, args = parsed
        command = self.get(name)
        if command is None:
            return {"ok": False, "message": f"Unknown command: /{name}"}
        result = command.handler(args, **context)
        if inspect.isawaitable(result):
            result = await result
        return result


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """A chronological unit of request or response content."""

    id: str
    kind: str
    role: str = "assistant"
    text: str = ""
    session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    #: The block is closed: nothing more will be streamed into it.
    done: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "role": self.role,
            "text": self.text,
            "session": self.session_id,
            "created_at": self.created_at,
            "done": self.done,
        }


#: Argument and result names whose value is never published.
SECRET_KEYS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "secret",
    "token",
)

#: What a redacted value is replaced with.
REDACTED = "[redacted]"


def secret_key(name: str) -> bool:
    """Whether an argument name looks like it carries a credential."""
    lowered = str(name).lower()
    return any(needle in lowered for needle in SECRET_KEYS)


def redact(value: Any, limit: int = 512) -> Any:
    """A JSON safe copy of a value with secrets removed and long text cut."""
    if isinstance(value, dict):
        return {
            str(k): REDACTED if secret_key(k) else redact(v, limit)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, limit) for v in value]
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value), limit)


@dataclass
class ToolCall:
    """One tool invocation of a run: what was called, with what, and how it went.

    The record is what makes a run auditable: it is published as `tool.start`
    when the model asks for the call, as `tool.progress` while it reports what
    it is doing, and as `tool.end` when the result comes back, and it is kept on
    the `RunResult` so a caller can inspect the whole sequence afterwards.
    """

    id: str
    name: str
    call_id: str | None = None
    arguments: Any = field(default_factory=dict)
    result: str = ""
    ok: bool = True
    started_at: float = field(default_factory=time.time)
    seen_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    #: The call waits on a person (`ask_user`), so it is alive for as long as
    #: it takes them to answer; the stall guard is for silent models only.
    asking: bool = False

    @property
    def done(self) -> bool:
        return self.ended_at is not None

    @property
    def duration(self) -> float:
        """Seconds the call took, so far or in total."""
        return (self.ended_at or time.time()) - self.started_at

    def touch(self) -> "ToolCall":
        """Record a sign of life: the call is still running."""
        self.seen_at = time.time()
        return self

    def finish(self, result: str, ok: bool = True) -> "ToolCall":
        self.result = result
        self.ok = ok
        self.ended_at = time.time()
        return self

    def signature(self) -> str:
        """The call as a single readable line: `name(key=value, …)`."""
        args = self.arguments
        if isinstance(args, dict):
            inner = ", ".join(f"{k}={json_dump(v)}" for k, v in args.items())
        elif args in ("", None):
            inner = ""
        else:
            inner = json_dump(args)
        return f"{self.name}({inner})"

    def report(self) -> str:
        """The call and, once it is done, its outcome, duration and result."""
        line = self.signature()
        if not self.done:
            return line
        status = "ok" if self.ok else "failed"
        line = f"{line} -> {status} in {self.duration * 1000:.0f} ms"
        return f"{line}\n{self.result}" if self.result else line

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "call_id": self.call_id,
            "arguments": self.arguments,
            "result": self.result,
            "ok": self.ok,
            "done": self.done,
            "duration": self.duration,
            "started_at": self.started_at,
        }


def json_dump(value: Any) -> str:
    """A value as compact JSON, falling back to its string form."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


class StepLog:
    """The steps of a run's workers as a client shows them, until the turn of
    the call that started them journals them.

    `record` keeps what a worker's step is published with (a block or a call
    carrying `parent` and `depth`) as the step now stands: its kind, name,
    text and outcome. A step belongs to the call at the root of its parents (a
    `map` part's worker steps to the `map` call): `take` hands the steps of one
    call over and forgets them, `live` shows those not taken yet. At most
    `LIMIT` steps a call are kept, the rest only counted; a step handed over
    keeps `TEXT` characters of its text, head and tail.
    """

    #: Steps kept for one call, at most; the rest are counted.
    LIMIT = 400
    #: Characters of a step's text handed over, at most.
    TEXT = 2_000
    #: What a step is published with; a tail (`tool.progress`) and the
    #: `context.compact` that repeats a compaction's block are not the step.
    KEPT = (
        EventType.BLOCK_START,
        EventType.BLOCK_DELTA,
        EventType.BLOCK_END,
        EventType.TOOL_START,
        EventType.TOOL_END,
    )

    def __init__(self) -> None:
        self.steps: dict[str, dict[str, Any]] = {}
        #: The call each step seen belongs to, dropped ones included.
        self.roots: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.dropped: dict[str, int] = {}

    def record(self, event_type: str, data: dict[str, Any]) -> None:
        """Keep what a nested event says of its step; anything else is ignored."""
        step_id, parent = str(data.get("id") or ""), str(data.get("parent") or "")
        if not step_id or not parent or event_type not in self.KEPT:
            return
        step = self.steps.get(step_id)
        if step is None:
            if step_id in self.roots:
                return  # dropped: past the limit of its call
            root = self.roots[step_id] = self.roots.get(parent, parent)
            if self.counts.get(root, 0) >= self.LIMIT:
                self.dropped[root] = self.dropped.get(root, 0) + 1
                return
            self.counts[root] = self.counts.get(root, 0) + 1
            step = self.steps[step_id] = {
                "id": step_id,
                "kind": str(data.get("kind") or "tool"),
                "role": str(data.get("role") or "assistant"),
                "parent": parent,
                "depth": int(data.get("depth") or 1),
                "text": "",
                "done": False,
            }
        if event_type == EventType.BLOCK_DELTA:
            step["text"] += str(data.get("text") or "")
            return
        if data.get("name"):
            step["name"] = str(data["name"])
        if event_type in (EventType.BLOCK_END, EventType.TOOL_END):
            step["done"] = True
        if event_type == EventType.TOOL_END:
            step["ok"] = bool(data.get("ok", True))
        text = data.get("text")
        if isinstance(text, str) and (text or step["done"]):
            step["text"] = text

    def take(self, root: str) -> tuple[list[dict[str, Any]], int]:
        """The steps of the call `root`, oldest first, each text clipped, and
        how many were dropped; forgotten here."""
        ids = [step_id for step_id, owner in self.roots.items() if owner == root]
        steps = [self.steps.pop(step_id) for step_id in ids if step_id in self.steps]
        for step_id in ids:
            del self.roots[step_id]
        self.counts.pop(root, None)
        for step in steps:
            step["text"] = clip_output(step["text"], self.TEXT)
        return steps, self.dropped.pop(root, 0)

    def live(self) -> list[dict[str, Any]]:
        """The steps not handed over yet, oldest first, as they stand."""
        return [dict(step) for step in self.steps.values()]


@dataclass
class RunResult:
    output: str = ""
    blocks: list[Block] = field(default_factory=list)
    tools: list[ToolCall] = field(default_factory=list)
    session_id: str | None = None
    #: Unique per run, so block ids stay unique across the runs of a session; a
    #: client keys its blocks by id and would otherwise rewrite an older turn.
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    prompt: str = ""
    error: str | None = None
    #: One of `ErrorKind` when the run failed, so a caller can tell a rate limit
    #: from a bad credential without reading `error`.
    error_kind: str | None = None
    #: Tokens and cost of every model call the run made, specialists included.
    usage: Usage = field(default_factory=Usage)
    #: The compaction of this run's context (`ContextRun`), when there is one.
    context: Any = field(default=None, repr=False, compare=False)
    #: What the run added to the context window, step by step as each one
    #: finished: assistant messages and tool calls with their outputs, as raw
    #: model input items. An application keeping its own history appends
    #: their `items` after its prompt; `Agent` journals each as it lands.
    turns: list[Turn] = field(default_factory=list)
    #: The steps of its workers not journaled yet, nested under the call that
    #: started them; that call's turn takes them (`Engine.checkpoint`).
    steps: StepLog = field(default_factory=StepLog, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def transient(self) -> bool:
        """True when the run failed for a reason another run might get past."""
        return self.error_kind in ErrorKind.TRANSIENT

    def fail(self, error: str, kind: str = ErrorKind.INTERNAL) -> "RunResult":
        """Record a failure and its kind on the result."""
        self.error = error
        self.error_kind = kind
        return self

    def text_of(self, kind: str) -> str:
        return "".join(b.text for b in self.blocks if b.kind == kind)


class WorkerRelay:
    """Relays what a worker streams as progress of the call that delegated it.

    With `forward`, the worker's blocks, calls and compactions (`NESTED`) are
    handed to it whole, to be shown nested under that call, and the call gets
    a heartbeat for them. Without, a tool call of the worker becomes a line
    (``-> read_text_file(...)``), so does a failed one (``!!``) and a
    compaction (``~~``). Anything else it streams (waits, retries, its context
    use) is a heartbeat; heartbeats come at most one every `BEAT` seconds.
    Each report keeps the delegating call alive for the leader's stall guard,
    as a busy shell command does; the worker's own start and end, and its
    context use, never reach the leader's bus.
    """

    #: Seconds between heartbeats relayed for a worker that streams.
    BEAT = 1.0
    #: Characters of one relayed line, at most.
    LINE = 160
    #: What a worker streams that only says it is alive.
    BEATS = (
        EventType.BLOCK_START,
        EventType.BLOCK_DELTA,
        EventType.BLOCK_END,
        EventType.TOOL_PROGRESS,
        EventType.TOOL_END,
        EventType.MODEL_WAIT,
        EventType.MODEL_RETRY,
        EventType.CONTEXT_USAGE,
    )
    #: What a worker streams that is shown nested under its call (`forward`).
    NESTED = (
        EventType.BLOCK_START,
        EventType.BLOCK_DELTA,
        EventType.BLOCK_END,
        EventType.TOOL_START,
        EventType.TOOL_PROGRESS,
        EventType.TOOL_END,
        EventType.CONTEXT_COMPACT,
    )

    def __init__(
        self,
        report: ToolReporter,
        call_id: str | None,
        clock: Callable[[], float] = time.monotonic,
        forward: Callable[[Event], Awaitable[None]] | None = None,
    ) -> None:
        self.report = report
        self.call_id = call_id
        self.clock = clock
        self.forward = forward
        self.beat_at: float | None = None

    def line(self, prefix: str, text: str) -> str:
        text = " ".join(text.split())
        if len(text) > self.LINE:
            text = text[: self.LINE - 3] + "..."
        return f"{prefix} {text}\n"

    async def __call__(self, event: Event) -> None:
        if self.forward is not None:
            if event.type in self.NESTED:
                await self.forward(event)
            elif event.type not in self.BEATS:
                return
            await self.beat()
            return
        data = event.data
        if event.type == EventType.TOOL_START:
            text = self.line("->", str(data.get("text") or data.get("name") or "tool"))
        elif event.type == EventType.TOOL_END and not data.get("ok", True):
            failed = f"{data.get('name') or 'tool'} failed: {data.get('result') or ''}"
            text = self.line("!!", failed)
        elif event.type == EventType.CONTEXT_COMPACT:
            text = self.line("~~", str(data.get("message") or "compacted"))
        elif event.type in self.BEATS:
            if self.beat_at is not None and self.clock() - self.beat_at < self.BEAT:
                return
            text = ""
        else:
            return
        self.beat_at = self.clock()
        await self.report(self.call_id, text)

    async def beat(self) -> None:
        """Report a heartbeat, unless one went out within `BEAT` seconds."""
        if self.beat_at is not None and self.clock() - self.beat_at < self.BEAT:
            return
        self.beat_at = self.clock()
        await self.report(self.call_id, "")


class Engine:
    """The agentic loop on its own: a model, its tools and a block stream.

    `Engine` is the embeddable half of the harness. It owns nothing but a
    configuration, an event bus, a prompt renderer, a tool registry and a model
    pool — no storage, no sessions, no conversation memory, no git checkouts, no
    console and no web layer — so an application that already has that
    infrastructure can drive inference and tool calls without inheriting any of
    it::

        engine = Engine(inference_key=..., inference_llm=..., env=False)
        result = await engine.run(my_messages, tools=my_tools)

    `run` takes the model input verbatim (a prompt string or chat messages the
    caller assembled from its own history) and returns a `RunResult`. `Agent`
    subclasses this and adds the batteries.
    """

    #: Delta stream events of the SDK mapped onto block kinds.
    DELTA_KINDS = {
        "response.reasoning_summary_text.delta": "reasoning",
        "response.reasoning_text.delta": "reasoning",
        "response.output_text.delta": "output",
    }

    def __init__(
        self,
        config: AgentConfig | None = None,
        *,
        events: EventBus | None = None,
        renderer: ConsoleRenderer | None = None,
        prompts: PromptRenderer | None = None,
        tools: ToolRegistry | None = None,
        models: ModelPool | None = None,
        workspace: Path | str | None = None,
        console: bool = False,
        env: bool = True,
        config_file: bool | str | Path | None = True,
        env_file: bool | str | Path | None = True,
        **overrides: Any,
    ) -> None:
        base = config or AgentConfig()
        #: The config file this instance was resolved from, when there was one.
        self.config_file = self.locate_config(config_file)
        #: The ``.env`` file loaded into the environment, when there was one.
        #: It is part of the environment layer, so ``env=False`` skips it too.
        self.env_file = self.locate_env(env_file) if env else None
        if self.env_file is not None:
            load_env_file(self.env_file, override=True)
        if self.config_file is not None:
            base = base.with_file(self.config_file)
        self.config = (base.with_env() if env else base).merge(**overrides)
        self.events = events or EventBus()
        self.prompts = prompts or PromptRenderer()
        self.tools = tools or ToolRegistry(shell=self.config.shell_enabled)
        self.models = models or ModelPool(RetryPolicy.from_config(self.config))
        #: Default directory the built-in file and shell tools are sandboxed to.
        #: Without one an embedded engine runs with no built-in tools at all.
        self.workspace = Path(workspace).expanduser().resolve() if workspace else None
        self.renderer = renderer
        if console and renderer is None and not self.config.quiet:
            self.renderer = ConsoleRenderer(color=self.config.color)
        if self.renderer is not None:
            self.renderer.attach(self.events)
        #: Questions the model asked that wait for an answer, by question id:
        #: the session they belong to and the future the answer resolves.
        self.questions: dict[str, tuple[str | None, asyncio.Future[str]]] = {}
        #: What each open question published, so a client that (re)connects
        #: is shown the card again (`pending_questions`).
        self.asking: dict[str, dict[str, Any]] = {}
        #: Token counters by leader model: each keeps its own calibration.
        self.counters: dict[tuple[str, str], TokenCounter] = {}
        #: Measured read and write speeds by leader model (`throughput`).
        self.speeds: dict[tuple[str, str], Throughput] = {}
        #: The plan of each session (`Planner`), bounded like any cache.
        self.plans: Cache = LruCache(maxsize=256)

    # -- configuration ------------------------------------------------------

    @staticmethod
    def locate_config(config_file: bool | str | Path | None = True) -> Path | None:
        """Resolve the ``config_file`` argument to a path, or to nothing.

        ``True`` searches (`find_config_file`), ``False`` or ``None`` skips the
        file layer entirely, and a path is taken as given — a path that does not
        exist is an error, because the caller asked for that file by name.
        """
        return _locate_file(config_file, find_config_file, "config file")

    @staticmethod
    def locate_env(env_file: bool | str | Path | None = True) -> Path | None:
        """Resolve the ``env_file`` argument the way `locate_config` does."""
        return _locate_file(env_file, find_env_file, "env file")

    def resolve_config(self, config: AgentConfig | None = None, **overrides: Any) -> AgentConfig:
        """Resolve the config used for a run.

        The ``input`` override is a file path or raw text; when it is a path the
        file is read and the text is assigned to ``config.input`` so templates
        can inject it.
        """
        base = (config or self.config).merge(**overrides)
        source = overrides.get("input") or base.input_source or base.input
        text = resolve_input(source) if source else ""
        return base.merge(input_source=str(source or ""), input=text)

    # -- model / sdk --------------------------------------------------------

    def build_model(self, config: AgentConfig, role: str = LEADER_ROLE) -> Any:
        """Return the SDK model for `role`, or ``None`` when it is unconfigured.

        Models come from the pool, so a role that shares an endpoint or a model
        name with another role reuses the same client and wrapper.
        """
        from agents import set_default_openai_client, set_tracing_disabled

        spec = config.model_spec(role)
        if spec is None:
            return None
        set_tracing_disabled(True)
        model = self.models.model(spec)
        if role == LEADER_ROLE:
            set_default_openai_client(self.models.client(spec))
        return model

    def build_policy(self, config: AgentConfig) -> RetryPolicy:
        """The policy every model call of a run is made under.

        Override to bound a role differently, to widen the transient set or to
        take the numbers from somewhere other than the configuration.
        """
        return RetryPolicy.from_config(config)

    def retry_reporter(
        self, session_id: str | None = None
    ) -> Callable[[dict[str, Any]], Awaitable[None]]:
        """A `RetryPolicy` callback that publishes `model.retry` on the bus."""

        async def report(info: dict[str, Any]) -> None:
            await self.events.publish(
                EventType.MODEL_RETRY, session_id=session_id, **info
            )

        return report

    def build_vision(
        self,
        config: AgentConfig,
        *,
        usage: "Usage | None" = None,
        session_id: str | None = None,
    ) -> Callable[[str, str], Awaitable[str]] | None:
        """Return an image describing delegate, or ``None`` when the leader sees.

        Without a separate vision model the leader is assumed to be multimodal
        and receives image data directly; with one, every image is routed to the
        specialist in a single completion and only its text answer comes back.
        """
        spec = config.model_spec(VISION_ROLE)
        if spec is None:
            return None
        policy = self.build_policy(config)
        on_retry = self.retry_reporter(session_id)

        async def describe(data_url: str, question: str = "") -> str:
            return await self.models.describe_image(
                spec,
                data_url,
                question,
                instructions=config.vision_instructions,
                max_tokens=config.vision_max_tokens or None,
                policy=policy,
                usage=usage,
                on_retry=on_retry,
            )

        return describe

    def build_tools(
        self,
        workspace: Workspace,
        config: AgentConfig | None = None,
        checkout: "Checkout | None" = None,
        *,
        usage: "Usage | None" = None,
        session_id: str | None = None,
        progress: ToolReporter | None = None,
        ask: Asker | None = None,
        read_limit: int | Callable[[], int] = 0,
        planner: Planner | None = None,
        delegate: Delegator | None = None,
        mapper: Mapper | None = None,
        folder: Folder | None = None,
    ) -> list[Any]:
        resolved = config or self.config
        vision = self.build_vision(resolved, usage=usage, session_id=session_id)
        return self.tools.build(
            workspace,
            vision=vision,
            checkout=checkout,
            progress=progress,
            ask=ask,
            read_limit=read_limit,
            planner=planner,
            delegate=delegate,
            mapper=mapper,
            folder=folder,
        )

    async def build_run_tools(
        self,
        workspace: Workspace,
        config: AgentConfig,
        result: RunResult,
        checkout: "Checkout | None" = None,
    ) -> list[Any]:
        """The built-in tools of one run over `workspace`, wired to the run:
        its progress reporter, its asker, its read page, its planner, its
        delegate, its map and its folder (each only when the config enables it)."""
        planner = self.build_planner(config, result, workspace)
        return self.build_tools(
            workspace,
            config,
            checkout,
            usage=result.usage,
            session_id=result.session_id,
            progress=self.tool_reporter(result),
            ask=self.build_asker(config, result),
            read_limit=self.page_limit(config, result, await self.read_limit(config)),
            planner=planner,
            delegate=self.build_delegate(config, result, workspace, planner),
            mapper=self.build_map(config, result, workspace, planner),
            folder=self.build_folder(config, result),
        )

    def build_sdk_agent(self, config: AgentConfig, tools: list[Any]) -> Any:
        from agents import Agent as SdkAgent, ModelSettings

        return SdkAgent(
            name=config.name,
            instructions=config.instructions,
            model=self.build_model(config),
            tools=tools,
            # usage on every streamed response: it prices the run and
            # calibrates the context count (most providers omit it unasked)
            model_settings=ModelSettings(
                include_usage=True,
                extra_body=self.models.extra_body(config.model_spec(LEADER_ROLE)),
            ),
        )

    # -- context ------------------------------------------------------------

    def counter(self, config: AgentConfig) -> TokenCounter:
        """The token counter of the leader model, shared by its runs."""
        key = (config.inference_llm, config.context_tokenizer)
        counter = self.counters.get(key)
        if counter is None:
            counter = self.counters[key] = TokenCounter(config.context_tokenizer or None)
        return counter

    def throughput(self, config: AgentConfig) -> Throughput:
        """The measured speeds of the leader model, shared by its runs."""
        key = (config.inference_url, config.inference_llm)
        speed = self.speeds.get(key)
        if speed is None:
            speed = self.speeds[key] = Throughput()
        return speed

    def build_planner(
        self, config: AgentConfig, result: RunResult, workspace: Workspace
    ) -> Planner | None:
        """The planner behind the `plan` tool of a run, or ``None`` when off."""
        if not config.plan_enabled:
            return None

        async def notify(view: dict[str, Any] | None) -> None:
            await self.events.publish(
                EventType.PLAN_UPDATE, session_id=result.session_id, plan=view
            )

        return Planner(
            workspace,
            self.counter(config),
            lambda: result.context.limits() if result.context is not None else None,
            self.plans,
            result.session_id or result.run_id,
            self.throughput(config),
            AskLimits.of(config),
            notify,
        )

    def build_folder(self, config: AgentConfig, result: RunResult) -> Folder | None:
        """The folder behind `branch` and `return`, or ``None`` when folding is
        off or the run compacts nothing (`context_trigger` 0)."""
        if not config.fold_enabled or config.context_trigger <= 0:
            return None
        return Folder(lambda: result.context)

    def plan_view(self, session_id: str | None) -> dict[str, Any] | None:
        """The plan of a session as a client shows it, outside any run."""
        plan = self.plans.get(session_id) if session_id else None
        return plan.view(None, self.throughput(self.config)) if plan is not None else None

    def plan_report(self, session_id: str | None) -> str:
        """The plan of a session and the measured speeds, for people (`/plan`)."""
        speed = self.throughput(self.config)
        speeds = speed_line(speed.prefill, speed.decode)
        view = self.plan_view(session_id)
        if view is None:
            return "No plan" + (f"\n\nThe model {speeds}" if speeds else "")
        lines = []
        for number, step in enumerate(view["steps"], 1):
            title = f"**{step['title']}**" if number == view["current"] else step["title"]
            mark = "x" if step["status"] == "done" else " "
            line = f"- [{mark}] {title}"
            if step["tokens"]:
                line += f" · {compact_tokens(step['tokens'])}"
            lines.append(line)
        tail = f"{view['done']} of {view['total']} done"
        if view["left"]:
            tail += f" · {compact_tokens(view['left'])} tokens left"
        if view["seconds"] >= 60:
            tail += f" · ~{view['seconds'] / 60:.0f} min"
        lines += ["", tail]
        if speeds:
            lines.append(f"The model {speeds}")
        return "\n".join(lines)

    async def context_window(self, config: AgentConfig, role: str = LEADER_ROLE) -> int:
        """The window of a role's model: configured, else asked, else assumed."""
        if config.context_window > 0:
            return int(config.context_window)
        spec = config.model_spec(role)
        found = await self.models.window(spec) if spec is not None else 0
        return found or DEFAULT_CONTEXT_WINDOW

    async def context_budget(self, config: AgentConfig) -> ContextBudget:
        return ContextBudget.of(
            await self.context_window(config),
            config.context_reserve,
            config.context_trigger,
            config.context_target,
        )

    async def read_limit(self, config: AgentConfig) -> int:
        """Characters one `read_text_file` call returns (0: whole files).

        Configured, else a quarter of the usable budget: a page always fits
        beside the conversation, so compaction never has to clip a fresh read
        (which the model would answer by reading it again, and again).
        """
        if config.read_output_limit:
            return max(int(config.read_output_limit), 0)
        budget = await self.context_budget(config)
        return max(budget.usable // 4 * TokenCounter.BYTES_PER_TOKEN, 2000)

    def page_limit(
        self, config: AgentConfig, result: RunResult, fixed: int
    ) -> int | Callable[[], int]:
        """The `read_text_file` page of a run: `fixed` when configured, else a
        quarter of the run's budget as it stands at each read, since the time
        cap can shrink it mid run below what `read_limit` sized."""
        if config.read_output_limit:
            return fixed

        def limit() -> int:
            if result.context is None:
                return fixed
            usable = result.context.budget.usable
            return min(fixed, max(usable // 4 * TokenCounter.BYTES_PER_TOKEN, 2000))

        return limit

    def build_summarizer(
        self, config: AgentConfig
    ) -> Callable[[str, str, str], Awaitable[str]] | None:
        """The delegate compaction folds a segment into the summary with.

        ``(previous summary, segment, focus) -> summary`` over the `summary`
        role, which falls back to the leader so a second model is optional.
        ``None`` (compaction drops instead) when `context_summary` is off.
        """
        if not config.context_summary:
            return None
        spec = config.model_spec(SUMMARY_ROLE) or config.model_spec(LEADER_ROLE)
        if spec is None:
            return None
        policy = self.build_policy(config)
        words = int(config.summary_max_tokens * 0.75)

        async def summarize(previous: str, segment: str, focus: str = "") -> str:
            instructions = f"{config.summary_instructions}\n\nStay within about {words} words."
            if focus:
                instructions += f"\n\nThe user asked the summary to focus on: {focus}"
            return await self.models.complete(
                spec,
                [
                    {"role": "system", "content": instructions},
                    {
                        "role": "user",
                        "content": (
                            f"<summary>\n{previous.strip() or '(empty)'}\n</summary>\n\n"
                            f"<segment>\n{segment}\n</segment>"
                        ),
                    },
                ],
                # room for a reasoning model to think before it writes
                max_tokens=max(4 * config.summary_max_tokens, 8192),
                policy=policy,
                on_retry=self.retry_reporter(),
            )

        return summarize

    async def build_compactor(self, config: AgentConfig) -> Compactor:
        """The compactor of a run: the leader's budget and the summary role."""
        budget = await self.context_budget(config)
        summarizer = self.build_summarizer(config)
        segment = 0
        if summarizer is not None:
            # a segment fits the summary model's own window with room to answer
            window = await self.context_window(config, SUMMARY_ROLE) if config.model_spec(
                SUMMARY_ROLE
            ) else budget.window
            segment = max(int(window * 0.6) - 2 * config.summary_max_tokens, 1024)
        return Compactor(
            self.counter(config),
            budget,
            summarize=summarizer,
            keep=config.context_keep,
            summary_tokens=config.summary_max_tokens,
            segment_limit=segment,
            reasoning=reasoning_replayed(config.inference_llm),
        )

    async def build_context(
        self,
        config: AgentConfig,
        result: RunResult,
        *,
        state: ContextState | None = None,
        pinned_at: int = -1,
        pinned: str = "",
        on_compact: Callable[[Compaction, list[Any]], Awaitable[None]] | None = None,
    ) -> ContextRun | None:
        """The in-run compaction of `result`, or ``None`` when it is off."""
        if config.context_trigger <= 0:
            return None
        compactor = await self.build_compactor(config)
        await asyncio.to_thread(compactor.counter.load)  # a first load may download
        session_id = result.session_id
        spec = config.model_spec(LEADER_ROLE)

        async def publish(event_type: str, **data: Any) -> None:
            await self.events.publish(event_type, session_id=session_id, **data)

        def learned(window: int) -> None:
            if spec is not None:
                self.models.learn_window(spec, window)

        plan_key = session_id or result.run_id
        return ContextRun(
            compactor,
            state=state,
            publish=publish,
            block=lambda n: f"{session_id}-{result.run_id}-c{n}",
            pinned_at=pinned_at,
            pinned=pinned,
            on_compact=on_compact,
            on_window=learned,
            throughput=self.throughput(config),
            stall=config.stream_timeout,
            slice_share=config.slice_share,
            slice_stall=config.slice_stall,
            plan=(lambda: self.plans.get(plan_key)) if config.plan_enabled else None,
            note=config.plan_enabled,
        )

    def build_run_config(self, config: AgentConfig, result: RunResult) -> Any:
        """The SDK run config: the context filter of the run, when it has one."""
        from agents import RunConfig

        return RunConfig(call_model_input_filter=result.context, tracing_disabled=True)

    def guard_model(self, sdk_agent: Any, result: RunResult) -> Any:
        """Wrap the leader's model so a context length error compacts and retries."""
        model = getattr(sdk_agent, "model", None)
        if result.context is None or model is None or isinstance(model, str):
            return sdk_agent
        sdk_agent.model = context_guard_class()(model, result.context)
        return sdk_agent

    def build_workspace(
        self, workspace: "Workspace | Path | str | None", config: AgentConfig
    ) -> Workspace | None:
        """Coerce whatever a caller passed as a workspace into a `Workspace`."""
        target = workspace if workspace is not None else self.workspace
        if target is None or isinstance(target, Workspace):
            return target
        outside_read, outside_write = bool(config.outside_read), bool(config.outside_write)
        return Workspace(
            target,
            shell_timeout=config.shell_timeout,
            shell_output_limit=config.shell_output_limit,
            shell_heartbeat=config.shell_heartbeat,
            outside_read=outside_read,
            outside_write=outside_write,
            sandbox=None if outside_read and outside_write else self.build_sandbox(config),
        )

    def build_sandbox(self, config: AgentConfig) -> Sandbox | None:
        """The sandbox that confines the shell of a run, or ``None`` for none.

        Override to supply a backend of your own (a container, a VM).
        """
        return pick_sandbox(config.shell_sandbox)

    # -- run ----------------------------------------------------------------

    async def run(
        self,
        model_input: str | list[dict[str, Any]],
        *,
        config: AgentConfig | None = None,
        tools: list[Any] | None = None,
        workspace: "Workspace | Path | str | None" = None,
        session_id: str | None = None,
        checkout: "Checkout | None" = None,
        **overrides: Any,
    ) -> RunResult:
        """Run the agentic loop over `model_input` and return a `RunResult`.

        `model_input` is used verbatim: a prompt string, or the chat messages
        the caller assembled from whatever history it keeps. `tools` replaces
        the built-in tool list; without it, tools are built for `workspace` (or
        the engine's default one) and there are none when neither is set.
        Failures are reported on the result rather than raised.
        """
        resolved = self.resolve_config(config, **overrides)
        result = RunResult(session_id=session_id or uuid.uuid4().hex[:12])
        async with self.running(resolved, result):
            result.prompt = prompt_of(model_input)
            await self.turn(
                model_input,
                resolved,
                result,
                tools=tools,
                workspace=workspace,
                checkout=checkout,
            )
        return result

    @contextlib.asynccontextmanager
    async def running(
        self, config: AgentConfig, result: RunResult
    ) -> AsyncIterator[RunResult]:
        """Publish the lifecycle events of a run, trap and classify its failures.

        The accounting of the result is published as well: while the body runs
        it is the sink every model call underneath finds, and `agent.end` always
        carries the tokens it collected, priced when the configuration says what
        a token costs.
        """
        token = _USAGE.set(result.usage)
        await self.events.publish(
            EventType.AGENT_START, session_id=result.session_id, config=config
        )
        try:
            yield result
        except asyncio.CancelledError:
            result.fail("cancelled", ErrorKind.CANCELLED)
            await self.events.publish(
                EventType.AGENT_ERROR,
                session_id=result.session_id,
                error=result.error,
                error_kind=result.error_kind,
            )
            raise
        except Exception as exc:
            result.fail(str(exc) or type(exc).__name__, classify_error(exc))
            await self.events.publish(
                EventType.AGENT_ERROR,
                session_id=result.session_id,
                error=result.error,
                error_kind=result.error_kind,
            )
        finally:
            result.usage.price(config.cost_input, config.cost_output)
            _USAGE.reset(token)
            await self.events.publish(
                EventType.AGENT_END,
                session_id=result.session_id,
                output=result.output,
                error=result.error,
                error_kind=result.error_kind,
                usage=result.usage.to_dict(),
                config=config,
            )

    async def turn(
        self,
        model_input: str | list[dict[str, Any]],
        config: AgentConfig,
        result: RunResult,
        *,
        tools: list[Any] | None = None,
        workspace: "Workspace | Path | str | None" = None,
        checkout: "Checkout | None" = None,
    ) -> RunResult:
        """One pass of the loop: build the tools, the model and stream it.

        The stream runs under the retry policy of the run, but only while it has
        produced nothing: once a block or a tool call exists, a second attempt
        would replay work the consumer has already seen, so a failure after that
        point is reported instead.
        """
        sandbox = self.build_workspace(workspace, config)
        if tools is None:
            tools = await self.build_run_tools(sandbox, config, result, checkout) if sandbox else []
        if result.context is None:
            result.context = await self.build_context(
                config, result, pinned_at=input_size(model_input) - 1, pinned=prompt_of(model_input)
            )
        leader = config.model_spec(LEADER_ROLE)
        if leader is not None:
            await self.models.num_ctx(leader)  # cached for build_sdk_agent
        sdk_agent = self.guard_model(self.build_sdk_agent(config, tools), result)
        return await self.build_policy(config).call(
            lambda: self.stream(sdk_agent, model_input, config, result),
            role=LEADER_ROLE,
            bounded=False,
            resumable=lambda: not (result.blocks or result.tools),
            on_retry=self.retry_reporter(result.session_id),
        )

    async def stream(
        self,
        sdk_agent: Any,
        model_input: str | list[dict[str, str]],
        config: AgentConfig,
        result: RunResult,
    ) -> RunResult:
        """Consume the SDK event stream, publishing block events as it goes.

        `model_input` is the prompt on its own, or a replayed transcript with
        the prompt as its newest message. The read is bounded by a stall guard:
        a provider that stops sending for longer than the budget of the run ends
        the attempt with `ErrorKind.TIMEOUT` instead of hanging the caller, and
        whatever the attempt spent is accounted even when it failed.
        """
        from agents import RawResponsesStreamEvent, RunItemStreamEvent, Runner

        session_id = result.session_id
        policy = self.build_policy(config)
        streamed = Runner.run_streamed(
            starting_agent=sdk_agent,
            input=model_input,
            max_turns=config.max_turns,
            run_config=self.build_run_config(config, result),
        )
        blocks: dict[str, Block] = {}
        current: Block | None = None
        #: The newest output block: the block an assistant message is journaled as.
        answer: Block | None = None
        #: The raw call item of each open tool call, by block id, until its output.
        calls: dict[str, dict[str, Any]] = {}

        def new_block(kind: str) -> Block:
            block = Block(
                id=f"{session_id}-{result.run_id}-{len(result.blocks)}",
                kind=kind,
                session_id=session_id,
            )
            blocks[block.id] = block
            result.blocks.append(block)
            return block

        async def open_block(kind: str) -> Block:
            nonlocal answer
            block = new_block(kind)
            if kind == "output":
                answer = block
            await self.events.publish(
                EventType.BLOCK_START,
                session_id=session_id,
                id=block.id,
                kind=kind,
                role=block.role,
            )
            return block

        async def close_block(block: Block | None) -> None:
            if block is None:
                return
            block.done = True
            await self.events.publish(
                EventType.BLOCK_END,
                session_id=session_id,
                id=block.id,
                kind=block.kind,
                text=block.text,
            )

        async def begin_tool(item: Any) -> None:
            nonlocal current
            await close_block(current)
            current = None
            block = new_block("tool")
            call = self.tool_call_of(item, block.id)
            result.tools.append(call)
            block.text = call.report()
            raw = self.input_item(item)
            if raw is not None:
                calls[block.id] = raw
            await self.events.publish(
                EventType.TOOL_START,
                session_id=session_id,
                id=block.id,
                kind="tool",
                name=call.name,
                call_id=call.call_id,
                arguments=call.arguments,
                text=block.text,
            )

        async def finish_tool(item: Any) -> None:
            output, ok = self.tool_result_of(item)
            call = self.pending_tool(result, getattr(item, "call_id", None))
            if call is None:
                return
            call.finish(output, ok)
            block = blocks.get(call.id)
            if block is not None:
                block.text = call.report()
                block.done = True
            await self.events.publish(
                EventType.TOOL_END,
                session_id=session_id,
                id=call.id,
                kind="tool",
                name=call.name,
                call_id=call.call_id,
                arguments=call.arguments,
                ok=call.ok,
                result=call.result,
                duration=call.duration,
                text=call.report(),
            )
            # Only a whole pair enters the context: a call without its output
            # is a request the next model call could not make sense of.
            raw_call, raw_output = calls.pop(call.id, None), self.input_item(item)
            if raw_call is not None and raw_output is not None:
                await self.checkpoint(
                    result,
                    Turn(
                        "tool",
                        call.report(),
                        id=call.id,
                        run=result.run_id,
                        items=[raw_call, raw_output],
                        meta={"name": call.name, "ok": call.ok},
                    ),
                )

        async def finish_message(item: Any) -> None:
            raw = self.input_item(item)
            if raw is None:
                return
            text = message_text(raw) or (answer.text if answer is not None else "")
            turn_id = (
                answer.id if answer is not None
                else f"{session_id}-{result.run_id}-m{len(result.turns)}"
            )
            await self.checkpoint(
                result, Turn("assistant", text, id=turn_id, run=result.run_id, items=[raw])
            )

        events = streamed.stream_events().__aiter__()
        try:
            while True:
                waiting = any(not call.done for call in result.tools)
                budget = self.stall_timeout(policy, config, waiting)
                try:
                    # Silence while a tool runs is the tool's; it reports itself.
                    event = await self.next_event(
                        events,
                        budget,
                        lambda: self.alive_at(result),
                        heartbeat=0.0 if waiting else config.stream_heartbeat,
                        on_wait=self.wait_reporter(result, budget),
                    )
                except StopAsyncIteration:
                    break
                if isinstance(event, RunItemStreamEvent):
                    if event.name == "tool_called":
                        await begin_tool(event.item)
                    elif event.name == "tool_output":
                        await finish_tool(event.item)
                    elif event.name == "message_output_created":
                        await finish_message(event.item)
                    continue
                if not isinstance(event, RawResponsesStreamEvent):
                    continue
                data = event.data
                kind = self.DELTA_KINDS.get(getattr(data, "type", ""))
                if kind is not None:
                    delta = getattr(data, "delta", "") or ""
                    if not delta:
                        continue
                    if current is None or current.kind != kind:
                        await close_block(current)
                        current = await open_block(kind)
                    current.text += delta
                    await self.events.publish(
                        EventType.BLOCK_DELTA,
                        session_id=session_id,
                        id=current.id,
                        kind=kind,
                        text=delta,
                    )
                elif getattr(data, "type", "") == "response.created":
                    if result.context is not None:
                        result.context.first_token()
                elif getattr(data, "type", "") == "response.completed":
                    await self.context_observed(result, data)
                elif getattr(data, "type", "") == "response.function_call_arguments.delta":
                    # The call itself is published from the run item event; here
                    # the only job is to end the block the model was writing.
                    await close_block(current)
                    current = None
        except BaseException:
            self.cancel_stream(streamed)
            raise
        finally:
            self.models.record(usage_of(streamed), result.usage)
        await close_block(current)
        result.output = streamed.final_output or result.text_of("output")
        return result

    def stall_timeout(
        self, policy: RetryPolicy, config: AgentConfig, waiting: bool = False
    ) -> float:
        """How long the leader may say nothing before the attempt is failed.

        The SDK runs tool calls inside the stream, so while one is still
        outstanding the budget is extended by the time a tool is allowed to
        take; otherwise a slow shell command would be indistinguishable from a
        stalled provider. A tool that reports progress restarts the budget with
        every report (see `next_event`), so only a silent one is bound by it.
        """
        if not policy.stall:
            return 0.0
        return policy.stall + config.shell_timeout if waiting else policy.stall

    async def next_event(
        self,
        events: Any,
        timeout: float = 0.0,
        alive: Callable[[], float] | None = None,
        *,
        heartbeat: float = 0.0,
        on_wait: Callable[[float], Awaitable[None]] | None = None,
    ) -> Any:
        """The next event of an SDK stream, bounded by a stall `timeout`.

        The budget runs from the latest sign of life: the start of the wait, or
        the newer time `alive` reports (the last heartbeat of a running tool),
        so a busy tool keeps the stream open while a silent one does not.
        While nothing arrives, `on_wait` is handed the seconds waited so far
        every `heartbeat` seconds; it observes the wait and never extends it.
        """
        beating = heartbeat > 0 and on_wait is not None
        if not timeout and not beating:
            return await events.__anext__()
        start = time.time()
        beat_at = start + heartbeat
        pending = asyncio.ensure_future(events.__anext__())
        try:
            while True:
                now = time.time()
                wait: float | None = None
                if timeout:
                    last = max(start, alive() if alive else start)
                    wait = last + timeout - now
                    if wait <= 0:
                        raise ModelError(
                            f"the model sent nothing for {timeout:g}s",
                            ErrorKind.TIMEOUT,
                            role=LEADER_ROLE,
                        )
                if beating:
                    due = max(beat_at - now, 0.0)
                    wait = due if wait is None else min(wait, due)
                done, _ = await asyncio.wait({pending}, timeout=wait)
                if done:
                    return pending.result()
                if beating and time.time() >= beat_at:
                    beat_at = time.time() + heartbeat
                    await self.notify_wait(on_wait, time.time() - start)
        finally:
            if not pending.done():
                pending.cancel()

    @staticmethod
    async def notify_wait(
        on_wait: Callable[[float], Awaitable[None]] | None, elapsed: float
    ) -> None:
        """Hand `elapsed` to `on_wait`; a failing observer never breaks the stream."""
        if on_wait is None:
            return
        try:
            await on_wait(elapsed)
        except Exception as exc:
            print(f"[stream] wait callback failed: {exc!r}", file=sys.stderr)

    def wait_reporter(
        self, result: RunResult, timeout: float = 0.0
    ) -> Callable[[float], Awaitable[None]]:
        """The `next_event` callback that publishes `model.wait` for a run.

        `elapsed` is the silence so far and `timeout` the stall budget it runs
        against (``0`` when unbounded), so a client can show both.
        """

        async def report(elapsed: float) -> None:
            await self.events.publish(
                EventType.MODEL_WAIT,
                session_id=result.session_id,
                role=LEADER_ROLE,
                elapsed=round(elapsed, 3),
                timeout=timeout,
            )

        return report

    @staticmethod
    def alive_at(result: RunResult) -> float:
        """When a tool call of `result` that is still open last showed life.

        A call that waits on a person is alive now, whatever it last reported.
        """
        now = time.time()
        return max(
            (now if call.asking else call.seen_at for call in result.tools if not call.done),
            default=0.0,
        )

    def tool_reporter(self, result: RunResult) -> ToolReporter:
        """The `(call_id, text)` callback the tools of a run report progress to.

        Each report marks the call alive for the stall guard and is published as
        `tool.progress` on the block of the call; `text` is output the tool
        produced, or empty for a heartbeat. A report for no open call is dropped.
        """

        async def report(call_id: str | None, text: str) -> None:
            call = self.pending_tool(result, call_id)
            if call is None or call.done:
                return
            call.touch()
            await self.events.publish(
                EventType.TOOL_PROGRESS,
                session_id=result.session_id,
                id=call.id,
                kind="tool",
                name=call.name,
                call_id=call.call_id,
                text=redact(text, self.TOOL_VALUE_LIMIT),
                duration=call.duration,
            )

        return report

    # -- questions ----------------------------------------------------------

    def build_asker(self, config: AgentConfig, result: RunResult) -> Asker | None:
        """The `ask_user` delegate of a run, or ``None`` when nobody can answer.

        A question is published as `question.start` on the block of its tool
        call and waits until a front end hands the answer to `answer`; it ends
        with `question.end`, also when the run is cancelled under it. The call
        is marked as waiting on a person meanwhile, so the stall guard never
        fails a run because its user is thinking.
        """
        if not config.ask_enabled:
            return None

        async def ask(call_id: str | None, question: str, options: list[str]) -> str:
            call = next(
                (c for c in result.tools if call_id is not None and c.call_id == str(call_id)),
                None,
            )
            if call is None and call_id is None:
                call = self.pending_tool(result, None)
            # The card takes over the block of its call; without one it gets a
            # block of its own, never an id an earlier question already used.
            question_id = (
                call.id if call is not None
                else f"{result.session_id}-{result.run_id}-q{uuid.uuid4().hex[:6]}"
            )
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self.questions[question_id] = (result.session_id, future)
            if call is not None:
                call.asking = True
            fields = {
                "id": question_id,
                "kind": "question",
                "call_id": call.call_id if call is not None else call_id,
            }
            answer, ok = "", False
            self.asking[question_id] = {**fields, "question": question, "options": list(options)}
            try:
                await self.events.publish(
                    EventType.QUESTION_START,
                    session_id=result.session_id,
                    question=question,
                    options=list(options),
                    **fields,
                )
                answer = await future
                ok = True
                return answer
            finally:
                self.questions.pop(question_id, None)
                self.asking.pop(question_id, None)
                if call is not None:
                    call.asking = False
                    call.touch()
                await self.events.publish(
                    EventType.QUESTION_END,
                    session_id=result.session_id,
                    answer=answer,
                    ok=ok,
                    **fields,
                )

        return ask

    def answer(self, question_id: str, text: str, *, session_id: str | None = None) -> bool:
        """Answer an open question. False when there is none by that id.

        With `session_id` the question must belong to that session, so a
        client can only answer what its own session was asked.
        """
        entry = self.questions.get(str(question_id))
        if entry is None:
            return False
        owner, future = entry
        if session_id is not None and owner != session_id:
            return False
        if future.done():
            return False
        future.set_result(str(text))
        return True

    def pending_questions(self, session_id: str | None) -> list[dict[str, Any]]:
        """The open questions of a session, as `question.start` published them."""
        return [
            dict(self.asking[question_id])
            for question_id, (owner, future) in self.questions.items()
            if owner == session_id and not future.done() and question_id in self.asking
        ]

    # -- workers ------------------------------------------------------------

    #: Share of a worker's fresh budget its inputs may take: the rest is room
    #: for its pages, its calls and its answer.
    WORKER_SHARE = 0.5
    #: The tools a worker keeps whatever `tools` names: they fold its context.
    FOLD_TOOLS = ("branch", "return")

    def worker_config(self, config: AgentConfig) -> AgentConfig:
        """The config a worker runs with: the `worker` role (else the leader)
        as its model, the slim `worker_instructions` with the size of its
        answer, `worker_max_turns`, and no plan, question or delegate. It
        folds its own branches when the leader does (`fold_enabled`)."""
        spec = config.model_spec(WORKER_ROLE) or config.model_spec(LEADER_ROLE)
        assert spec is not None  # the leader always resolves
        instructions = config.worker_instructions
        if config.worker_max_tokens > 0:
            words = max(int(config.worker_max_tokens * 0.75), 1)
            instructions += f"\n\nAnswer within about {words} words."
        return config.merge(
            inference_llm=spec.name,
            inference_url=spec.api_url,
            inference_key=spec.api_key,
            instructions=instructions,
            max_turns=max(int(config.worker_max_turns), 1),
            plan_enabled=False,
            ask_enabled=False,
            delegate_enabled=False,
        )

    def fork(self, events: EventBus) -> "Engine":
        """A shallow copy of this engine that publishes on `events` instead.

        It shares the model pool, the counters, speeds and plans, and keeps
        every override of the class, so a worker runs the way the leader does
        while what it streams stays off the leader's bus.
        """
        twin = copy.copy(self)
        twin.events = events
        return twin

    async def worker_budget(self, config: AgentConfig) -> int:
        """Tokens the inputs of a worker may take: `WORKER_SHARE` of a fresh
        context, under the time cap once its model's read speed is measured."""
        budget = await self.context_budget(config)
        timed = 0
        if config.slice_stall > 0:
            seconds = config.stream_timeout * config.slice_stall
            timed = self.throughput(config).readable(seconds, budget.reserve)
            timed = max(timed, ContextRun.TIMED_FLOOR) if timed else 0
        return SliceBudget(budget.usable, timed, self.WORKER_SHARE).slice

    def build_delegate(
        self,
        config: AgentConfig,
        result: RunResult,
        workspace: Workspace,
        planner: Planner | None = None,
    ) -> Delegator | None:
        """The `delegate` of a run, or ``None`` unless `delegate_enabled`.

        The inputs of a step are measured; more than a worker may read
        (`worker_budget`) is refused once with their sizes, and the same step
        sent back runs anyway (a line cannot be split). With a `planner`,
        `step` names a step of the plan: its inputs are the default, it is
        marked active while the worker runs and done once it answers.
        """
        if not config.delegate_enabled:
            return None
        refused: list[Any] = [None]

        async def delegate(
            call_id: str | None, task: str, inputs: list[str], step: int, tools: list[str]
        ) -> str:
            task = task.strip()
            if not task:
                return "Error: delegate needs a task"
            numbered: PlanStep | None = None
            if step:
                plan = planner.plan if planner is not None else None
                if plan is None or not 1 <= step <= len(plan.steps):
                    return f"Error: there is no plan step {step}"
                numbered = plan.steps[step - 1]
                inputs = inputs or list(numbered.inputs)
            worker = self.worker_config(config)
            measure = Planner(workspace, self.counter(worker), None, LruCache(1), "")
            sizes, missing = await asyncio.to_thread(measure.sizes, inputs)
            tokens, limit = sum(size for _, size in sizes), await self.worker_budget(worker)
            key = (task, tuple(inputs))
            if tokens > limit and refused[0] != key:
                refused[0] = key
                parts = ", ".join(f"{name} {compact_tokens(size)}" for name, size in sizes)
                return (
                    f"Not delegated: the inputs take ~{compact_tokens(tokens)} tokens ({parts}), "
                    f"more than the {compact_tokens(limit)} a worker may read. Split the step "
                    "by file or by line range ('path:1-400'), or send it again to run it anyway."
                )
            refused[0] = None
            before = numbered.status if numbered is not None else ""
            if planner is not None and numbered is not None:
                if planner.mark(step, "active", numbered.title) is not None:
                    await planner.changed()
            reply, ok = await self.run_worker(
                worker, result, workspace, task, sizes, missing, call_id=call_id, tools=tools
            )
            if planner is not None and numbered is not None:
                marked = planner.mark(step, "done" if ok else before, numbered.title)
                if marked is not None:
                    await planner.changed()
                if ok and marked is not None:
                    reply += f"\n[plan] step {step} marked done"
            return reply

        return delegate

    @staticmethod
    def worker_prompt(
        task: str, sizes: Iterable[tuple[str, int]] = (), missing: Iterable[str] = ()
    ) -> str:
        """What a worker is told: the task, and the inputs the harness measured."""
        lines = [f"- {name}: ~{compact_tokens(size)} tokens" for name, size in sizes]
        lines += [f"- {name}: not found" for name in missing]
        if not lines:
            return task
        return f"{task}\n\nInputs (read long ones a page at a time):\n" + "\n".join(lines)

    async def run_worker(
        self,
        config: AgentConfig,
        result: RunResult,
        workspace: Workspace,
        task: str,
        sizes: Iterable[tuple[str, int]] = (),
        missing: Iterable[str] = (),
        *,
        call_id: str | None = None,
        tools: Iterable[str] = (),
        parent: str | None = None,
        depth: int = 1,
        trailer: bool = True,
    ) -> tuple[str, bool]:
        """Run `task` in a fresh context: the worker's answer, and whether it finished.

        `config` is the worker's (`worker_config`). It streams onto a bus of
        its own, relayed by `WorkerRelay`: its blocks and calls are published
        on this bus nested under block `parent` (by default the block of the
        call that delegated it) at `depth`, and that call gets a heartbeat for
        them, so the page and the repl show what it does and the leader's stall
        guard sees it alive. It journals nothing; its tokens count to the run
        (`current_usage`). The answer is clipped to `worker_max_tokens` and,
        with `trailer`, followed by how many calls it took and how long.
        """
        if parent is None and call_id is not None:
            call = self.pending_tool(result, call_id)
            parent = call.id if call is not None else None
        forward = self.nest(result, parent, depth) if parent else None
        bus = EventBus()
        bus.on(EventType.ALL, WorkerRelay(self.tool_reporter(result), call_id, forward=forward))
        twin = self.fork(bus)
        worker = RunResult(run_id=f"{result.run_id}w{uuid.uuid4().hex[:4]}")
        built = await twin.build_run_tools(workspace, config, worker)
        wanted = {str(name).strip() for name in tools if str(name).strip()}
        if wanted:
            names = [tool.name for tool in built]
            unknown = sorted(wanted - set(names))
            if unknown:
                return (
                    f"Error: no worker tool named {', '.join(unknown)} "
                    f"(there are {', '.join(names)})", False
                )
            # folding acts on the worker's own context, not the workspace: kept
            built = [tool for tool in built if tool.name in wanted or tool.name in self.FOLD_TOOLS]
        started = time.monotonic()
        try:
            await twin.turn(
                self.worker_prompt(task, sizes, missing), config, worker,
                tools=built, workspace=workspace,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            kind = classify_error(exc)
            return (
                f"Error: the worker failed ({kind}) after {len(worker.tools)} tool calls: "
                f"{str(exc) or type(exc).__name__}", False
            )
        reply = worker.output.strip() or "(the worker answered nothing)"
        limit = int(config.worker_max_tokens)
        if limit > 0 and self.counter(config).text(reply) > limit:
            reply = clip_output(reply, limit * TokenCounter.BYTES_PER_TOKEN)
        if not trailer:
            return reply, True
        seconds = time.monotonic() - started
        return f"{reply}\n[worker: {len(worker.tools)} tool calls in {seconds:.0f}s]", True

    def nest(
        self, result: RunResult, parent: str, depth: int = 1
    ) -> Callable[[Event], Awaitable[None]]:
        """Publish an event a worker streamed on this bus, for the session of
        `result`, nested under block `parent` at `depth`."""

        async def forward(event: Event) -> None:
            data = {**event.data, "parent": parent, "depth": depth}
            await self.publish_step(result, event.type, **data)

        return forward

    async def publish_step(self, result: RunResult, event_type: str, /, **data: Any) -> None:
        """Publish a worker's step for the session of `result`, kept on its
        `StepLog` until the turn of the call that started it journals it.
        Positional: `data` may carry a `result` of its own (a call's)."""
        result.steps.record(event_type, data)
        await self.events.publish(event_type, session_id=result.session_id, **data)

    #: Parts one `map` may cut its inputs into, at most.
    MAP_LIMIT = 64
    #: Tokens the answer of one part keeps at least, however many share a slice.
    MAP_ANSWER = 100

    def map_answer(self, config: AgentConfig, share: int, parts: int) -> int:
        """Tokens each of `parts` answers may take so that together they fit
        `share` (the leader's slice): within `worker_max_tokens`, at least
        `MAP_ANSWER`."""
        each = int(share) // max(int(parts), 1)
        if config.worker_max_tokens > 0:
            each = min(each, int(config.worker_max_tokens))
        return max(each, self.MAP_ANSWER)

    @staticmethod
    def map_prompt(task: str, number: int, count: int, spec: str, tokens: int) -> str:
        """What the worker of one part is told: the task, its part and how to read it."""
        match = Planner.RANGE.fullmatch(spec)
        if match:
            path, first, last = match.group(1), int(match.group(2)), int(match.group(3))
            where = f"{path}, lines {first}-{last}"
            how = f'read_text_file(path="{path}", offset={first}, limit={last - first + 1})'
        elif guess_mime(spec).startswith("image/"):
            where, how = spec, "the image tool"
        else:
            where, how = spec, f'read_text_file(path="{spec}")'
        return (
            f"{task}\n\nYour part ({number} of {count}): {where} (~{compact_tokens(tokens)} "
            f"tokens). Read it with {how} and work on that part alone: the other parts "
            "go to other workers."
        )

    def build_map(
        self,
        config: AgentConfig,
        result: RunResult,
        workspace: Workspace,
        planner: Planner | None = None,
    ) -> Mapper | None:
        """The `map` of a run, or ``None`` unless `delegate_enabled`.

        The inputs are cut at line boundaries into parts a worker may read
        (`worker_budget`, or `chunk_tokens` when smaller); more than
        `MAP_LIMIT` parts is refused. Each part runs in a worker of its own,
        `map_parallel` at once, its answer sized so that together they fit the
        leader's slice (`map_answer`); the answers come back in order, a failed
        part marked and the rest kept. With a `planner`, `step` lends its
        inputs and is marked like a delegated one.
        """
        if not config.delegate_enabled:
            return None

        async def map_inputs(
            call_id: str | None,
            task: str,
            inputs: list[str],
            chunk_tokens: int,
            step: int,
            tools: list[str],
        ) -> str:
            task = task.strip()
            if not task:
                return "Error: map needs a task"
            numbered: PlanStep | None = None
            if step:
                plan = planner.plan if planner is not None else None
                if plan is None or not 1 <= step <= len(plan.steps):
                    return f"Error: there is no plan step {step}"
                numbered = plan.steps[step - 1]
                inputs = inputs or list(numbered.inputs)
            if not inputs:
                return "Error: map needs inputs (paths, globs or line ranges)"
            worker = self.worker_config(config)
            limit = await self.worker_budget(worker)
            size = min(chunk_tokens, limit) if chunk_tokens > 0 else limit
            cutter = Planner(workspace, self.counter(worker), None, LruCache(1), "")
            parts, missing = await asyncio.to_thread(cutter.chunks, inputs, size, self.MAP_LIMIT)
            if not parts:
                return f"Error: nothing to map: {', '.join(missing) or 'no input'} not found"
            if len(parts) > self.MAP_LIMIT:
                return (
                    f"Not mapped: the inputs make more than {self.MAP_LIMIT} parts of up to "
                    f"{compact_tokens(size)} tokens. Map fewer inputs at a time."
                )
            share = result.context.limits().slice if result.context is not None else limit
            answer = self.map_answer(config, share, len(parts))
            worker = self.worker_config(config.merge(worker_max_tokens=answer))
            before = numbered.status if numbered is not None else ""
            if planner is not None and numbered is not None:
                if planner.mark(step, "active", numbered.title) is not None:
                    await planner.changed()
            started = time.monotonic()
            replies = await self.map_parts(
                worker, result, workspace, task, parts,
                call_id=call_id, tools=tools, parallel=config.map_parallel,
            )
            failed = sum(1 for _, ok in replies if not ok)
            count = len(parts)
            head = (
                f"Mapped {count} part{'s' if count != 1 else ''} "
                f"(~{compact_tokens(sum(tokens for _, tokens in parts))} tokens) of "
                f"{', '.join(inputs)} in {time.monotonic() - started:.0f}s"
            )
            if failed:
                head += f", {failed} failed"
            if missing:
                head += f" (not found: {', '.join(missing)})"
            sections = [
                f"## {number}/{count} {spec}{'' if ok else ' (failed)'}\n{reply}"
                for number, ((spec, _), (reply, ok)) in enumerate(zip(parts, replies), 1)
            ]
            text = f"{head}:\n\n" + "\n\n".join(sections)
            if planner is not None and numbered is not None:
                marked = planner.mark(step, "done" if not failed else before, numbered.title)
                if marked is not None:
                    await planner.changed()
                if not failed and marked is not None:
                    text += f"\n[plan] step {step} marked done"
            return text

        return map_inputs

    async def map_parts(
        self,
        config: AgentConfig,
        result: RunResult,
        workspace: Workspace,
        task: str,
        parts: list[tuple[str, int]],
        *,
        call_id: str | None = None,
        tools: Iterable[str] = (),
        parallel: int = 1,
    ) -> list[tuple[str, bool]]:
        """Run one worker per part, `parallel` at once: each answer and
        whether it finished, in the order of `parts`. Each part is a block
        (``part n/N``) nested under the `map` call, its worker's steps under it."""
        gate = asyncio.Semaphore(max(int(parallel), 1))
        call = self.pending_tool(result, call_id) if call_id is not None else None
        count = len(parts)

        async def publish(event_type: str, **data: Any) -> None:
            if call is not None:
                await self.publish_step(result, event_type, parent=call.id, depth=1, **data)

        async def one(number: int, spec: str, tokens: int) -> tuple[str, bool]:
            async with gate:
                part = f"{call.id}-p{number}" if call is not None else None
                name = f"part {number}/{count}"
                title = f"{name}: {spec}"
                await publish(EventType.TOOL_START, id=part, kind="tool", name=name, text=title)
                started = time.monotonic()
                reply, ok = await self.run_worker(
                    config, result, workspace, self.map_prompt(task, number, count, spec, tokens),
                    call_id=call_id, tools=tools, parent=part, depth=2, trailer=False,
                )
                seconds = time.monotonic() - started
                status = "ok" if ok else "failed"
                await publish(
                    EventType.TOOL_END,
                    id=part,
                    kind="tool",
                    name=name,
                    ok=ok,
                    result=redact(reply, self.TOOL_VALUE_LIMIT),
                    duration=seconds,
                    text=f"{title} -> {status} in {seconds * 1000:.0f} ms\n{reply}",
                )
                return reply, ok

        return list(
            await asyncio.gather(
                *(one(number, spec, tokens) for number, (spec, tokens) in enumerate(parts, 1))
            )
        )

    @staticmethod
    def cancel_stream(streamed: Any) -> None:
        """Stop an SDK stream that will not be read any further."""
        cancel = getattr(streamed, "cancel", None)
        if callable(cancel):
            with contextlib.suppress(Exception):
                cancel()

    # -- tool call transparency ---------------------------------------------

    #: Longest argument or result value published before it is cut.
    TOOL_VALUE_LIMIT = 512

    def tool_call_of(self, item: Any, block_id: str) -> ToolCall:
        """The `ToolCall` of an SDK tool call item, with arguments redacted."""
        raw = getattr(item, "raw_item", None)
        name = (
            getattr(item, "tool_name", None)
            or getattr(raw, "name", None)
            or getattr(raw, "type", None)
            or "tool"
        )
        call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None)
        return ToolCall(
            id=block_id,
            name=str(name),
            call_id=str(call_id) if call_id else None,
            arguments=self.redact_arguments(getattr(raw, "arguments", None)),
        )

    def tool_result_of(self, item: Any) -> tuple[str, bool]:
        """The redacted result of an SDK tool output item and whether it is ok."""
        output = getattr(item, "output", None)
        if output is None:
            raw = getattr(item, "raw_item", None)
            output = raw.get("output") if isinstance(raw, dict) else raw
        text = output if isinstance(output, str) else json_dump(redact(output))
        ok = not text.lstrip().lower().startswith("error")
        return str(redact(text, self.TOOL_VALUE_LIMIT)), ok

    def redact_arguments(self, arguments: Any) -> Any:
        """Tool arguments as JSON safe data with credentials removed."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except (TypeError, ValueError):
                pass
        return redact(arguments if arguments is not None else {}, self.TOOL_VALUE_LIMIT)

    # -- journal ------------------------------------------------------------

    async def context_observed(self, result: RunResult, data: Any) -> None:
        """A model call finished: calibrate the context count on its usage."""
        if result.context is None:
            return
        usage = getattr(getattr(data, "response", None), "usage", None)
        details = usage.get("input_tokens_details") if isinstance(usage, dict) else getattr(
            usage, "input_tokens_details", None
        )
        payload = result.context.observe(
            usage_value(usage, "input_tokens", "prompt_tokens"),
            usage_value(usage, "output_tokens", "completion_tokens"),
            usage_value(details, "cached_tokens") if details is not None else 0,
        )
        if payload is not None:
            await self.events.publish(
                EventType.CONTEXT_USAGE, session_id=result.session_id, **payload
            )

    async def checkpoint(self, result: RunResult, turn: Turn) -> None:
        """A step of the run finished: record what it added to the context.

        The engine keeps it on `result.turns` only; `Agent` also journals it,
        so a run that dies later still leaves this step behind. A tool turn
        takes the steps of the workers its call ran (`meta["steps"]`, and
        `meta["dropped"]` past `StepLog.LIMIT`): shown again after a reload,
        never replayed to a model.
        """
        if turn.role == "tool" and turn.id:
            steps, dropped = result.steps.take(turn.id)
            if steps:
                turn.meta["steps"] = steps
            if dropped:
                turn.meta["dropped"] = dropped
        result.turns.append(turn)

    @staticmethod
    def input_item(item: Any) -> dict[str, Any] | None:
        """An SDK run item as a JSON safe model input item, or ``None``."""
        convert = getattr(item, "to_input_item", None)
        if not callable(convert):
            return None
        try:
            raw = json.loads(json.dumps(convert(), default=str))
        except (TypeError, ValueError, AttributeError) as exc:
            print(f"[journal] run item not replayable: {exc!r}", file=sys.stderr)
            return None
        return raw if isinstance(raw, dict) else None

    @staticmethod
    def pending_tool(result: RunResult, call_id: Any) -> ToolCall | None:
        """The call an output belongs to: matched by id, else the oldest open one."""
        if call_id is not None:
            for call in result.tools:
                if call.call_id == str(call_id):
                    return call
        return next((call for call in result.tools if not call.done), None)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Release the resources of the engine: its clients, models and plans."""
        self.models.clear()
        self.plans.clear()

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    async def __aenter__(self) -> "Engine":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()


def input_size(model_input: str | list[dict[str, Any]]) -> int:
    """How many items a model input is (a prompt string is one)."""
    return 1 if isinstance(model_input, str) else len(model_input)


def prompt_of(model_input: str | list[dict[str, Any]]) -> str:
    """The prompt of a model input: itself, or the newest message in a list."""
    if isinstance(model_input, str):
        return model_input
    return str(model_input[-1].get("content", "")) if model_input else ""


def message_text(item: dict[str, Any]) -> str:
    """The text of an assistant message input item (its ``output_text`` parts)."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") in ("output_text", "text")
    )


class Agent(Engine):
    """The one API: a feature rich agent harness.

    ``await Agent().run(template)`` resolves the configuration, renders the
    template against it and runs the agentic loop, streaming block events onto
    the bus. Every step is an overridable method or an injectable collaborator.

    Everything below the loop itself — sessions, workspaces, storage,
    conversation memory, git checkouts, slash commands, console and web — lives
    here; consumers that only want inference and tool calls use `Engine`.
    """

    #: Template used when the harness is driven by the web layer.
    template: str | None = None

    def __init__(
        self,
        config: AgentConfig | None = None,
        *,
        events: EventBus | None = None,
        renderer: ConsoleRenderer | None = None,
        prompts: PromptRenderer | None = None,
        tools: ToolRegistry | None = None,
        models: ModelPool | None = None,
        store: Store | None = None,
        cache: Cache | None = None,
        sessions: SessionManager | None = None,
        memory: "ConversationMemory | None" = None,
        repos: "RepoManager | None" = None,
        commands: CommandRegistry | None = None,
        rules: "RuleBook | None" = None,
        console: bool = True,
        config_file: bool | str | Path | None = True,
        env_file: bool | str | Path | None = True,
        mode: str | None = None,
        **overrides: Any,
    ) -> None:
        super().__init__(
            config,
            events=events,
            renderer=renderer,
            prompts=prompts,
            tools=tools,
            models=models,
            console=console,
            config_file=config_file,
            env_file=env_file,
            **overrides,
        )
        #: The front end this agent was built for (``repl``, ``web``), which
        #: settles what the layers left unset (`MODE_TOOLS`, `MODE_STATE`).
        self.mode = mode
        if mode is not None:
            self.config = self.config.with_defaults(
                **MODE_TOOLS.get(mode, {}), **MODE_STATE.get(mode, {})
            )
        self.config = self.config.state_paths()
        self.store = store or SqliteStore(self.config.db_path)
        self.cache = cache or LruCache(self.config.cache_size)
        #: Plans by session, written through to the store: a resumed session,
        #: or a restarted process, continues the plan where it stood.
        self.plans = PlanBook(self.store)
        self.sessions = sessions or SessionManager(
            root=self.config.workspace_root,
            ttl=self.config.session_ttl or DEFAULT_SESSION_TTL,
            store=self.store,
            cache=self.cache,
            prefix=self.config.name,
            keep_workspace=self.config.keep_workspace,
            seed=self.config.workspace_seed,
            durable=bool(self.config.session_durable),
            sweep_interval=self.config.session_sweep_interval,
        )
        self.repos = repos or RepoManager(
            RepoSpec.from_config(self.config), timeout=self.config.repo_timeout
        )
        self.memory = memory or ConversationMemory(
            self.store,
            enabled=self.config.memory_enabled,
            tools=self.config.memory_tools,
        )
        #: Compactions of the journal running in the background, by session.
        self.compacting: dict[str, asyncio.Task[Any]] = {}
        #: The newest `context.usage` of each session, for a client that joins.
        self.context_seen: dict[str, dict[str, Any]] = {}
        #: What the instructions and tool schemas of each session cost, last run.
        self.context_fixed: dict[str, int] = {}
        self.events.on(EventType.CONTEXT_USAGE, self.context_noted)
        self.rules = rules or RuleBook(
            self.store,
            enabled=self.config.rules_enabled,
            max_chars=self.config.rules_max_chars,
        )
        #: The run in flight per session, with the task running it.
        self.live: dict[str, tuple[RunResult, "asyncio.Task[Any] | None"]] = {}
        self.sessions.listeners.append(self.session_closed)
        self.reconcile()
        self.commands = commands or CommandRegistry()
        self.register_default_commands()

    # -- prompt -------------------------------------------------------------

    def build_prompt(self, template: str, config: AgentConfig, **context: Any) -> str:
        """Render the consumer's template against the resolved config."""
        return self.decorate_prompt(
            self.prompts.render(template, config, **context), config
        )

    def decorate_prompt(self, prompt: str, config: AgentConfig) -> str:
        """Append harness supplied context (attachments) to a rendered prompt."""
        files = config.extras.get("attachments") or []
        if not files or "## Attached files" in prompt:
            return prompt
        listing = "\n".join(f"- {name}" for name in files)
        return f"{prompt}\n\n## Attached files\n\n{listing}"

    # -- model / sdk --------------------------------------------------------

    # -- run ----------------------------------------------------------------

    async def run(
        self,
        template: str,
        *,
        config: AgentConfig | None = None,
        session: Session | str | None = None,
        context: dict[str, Any] | None = None,
        **overrides: Any,
    ) -> RunResult:
        """Render `template` and run the agentic loop, returning a `RunResult`.

        Unlike `Engine.run`, which takes the model input verbatim, this renders
        the consumer's template inside a session workspace and replays what that
        session remembers.
        """
        resolved = self.resolve_config(config, **overrides)
        resolved = resolved.merge(
            instructions=self.decorate_instructions(resolved.instructions, resolved)
        )
        session_obj = self.acquire_session(session)
        await self.prepare_session(session_obj)
        result = RunResult(session_id=session_obj.id)
        self.live[session_obj.id] = (result, asyncio.current_task())
        try:
            async with self.running(resolved, result):
                await self.publish_prompt(resolved, result)
                result.prompt = self.build_prompt(template, resolved, **(context or {}))
                await self.compacted(session_obj.id)
                model_input = self.build_input(result.prompt, session_obj, resolved)
                self.journal_prompt(session_obj, resolved, result)
                await self.prepare_context(session_obj, resolved, result, model_input)
                await self.turn(
                    model_input,
                    resolved,
                    result,
                    workspace=session_obj.workspace,
                    checkout=self.checkout(session_obj),
                )
                await self.remember(session_obj, resolved, result)
        finally:
            if self.live.get(session_obj.id, (None,))[0] is result:
                del self.live[session_obj.id]
            self.settle(session_obj, result)
            self.settle_context(session_obj.id, result)
            self.schedule_compaction(session_obj.id, resolved)
        return result

    def acquire_session(self, session: Session | str | None) -> Session:
        if isinstance(session, Session):
            return session
        self.sessions.purge_expired()
        return self.sessions.ensure(session)

    def decorate_instructions(self, instructions: str, config: AgentConfig) -> str:
        """Append harness supplied standing context (learned rules)."""
        return self.rules.decorate(instructions) if config.rules_enabled else instructions

    # -- memory -------------------------------------------------------------

    #: Sent in place of a prompt by `/resume`, which runs it without the template.
    RESUME_PROMPT = (
        "Your previous run in this conversation stopped before it finished. "
        "Continue that task from where it stopped: everything already done is in "
        "the conversation above, so don't repeat finished steps, check the state "
        "of whatever was in progress, then carry on."
    )

    def build_input(
        self, prompt: str, session: Session, config: AgentConfig
    ) -> str | list[dict[str, Any]]:
        """The model input for a run: the prompt, preceded by what is remembered.

        A session with nothing remembered is a plain string, which is what the
        SDK expects for a one shot run; otherwise the journal is replayed as the
        context window (messages, and tool calls with their outputs) and the
        prompt becomes the newest user message.
        """
        history = self.memory.history(session.id) if config.memory_enabled else []
        if not history:
            return prompt
        return [*history, {"role": "user", "content": prompt}]

    # -- context ------------------------------------------------------------

    #: A compaction between runs starts this much earlier than one inside a
    #: run: nobody waits on it, and the next run then rarely has to.
    BACKGROUND_TRIGGER = 0.9

    async def prepare_context(
        self,
        session: Session,
        config: AgentConfig,
        result: RunResult,
        model_input: str | list[dict[str, Any]],
    ) -> None:
        """Seed the compaction of a run with what the journal decided.

        The input opens with the journal's summary, which the run's own
        compactions fold further; each item the run starts with is tied to the
        turn it stands for (the prompt, journaled after `build_input`, last),
        so a summary made mid run is journaled with the turn it keeps from. A
        consumer's own `build_input` loses that tie, never the compaction.
        """
        transcript = self.memory.transcript(session.id)
        items, owners = self.memory.window(session.id) if config.memory_enabled else ([], [])
        if len(owners) != input_size(model_input):
            owners = []
        state = ContextState()
        summary = transcript.summary() if config.memory_enabled else None
        first = model_input[0] if isinstance(model_input, list) and model_input else None
        if summary is not None and first == summary_message(summary.text):
            state = ContextState(summary=summary.text, cut=1)

        async def on_compact(report: Compaction, items: list[Any]) -> None:
            self.journal_compaction(session.id, result.run_id, report, items, owners)

        result.context = await self.build_context(
            config,
            result,
            state=state,
            pinned_at=input_size(model_input) - 1,
            pinned=prompt_of(model_input),
            on_compact=on_compact,
        )

    def journal_compaction(
        self,
        session_id: str,
        run_id: str,
        report: Compaction,
        items: list[Any],
        owners: list[Turn],
    ) -> Turn | None:
        """Journal a compaction as a ``compact`` turn: its marker, and its summary.

        A summary records the turn it keeps from (`keep_ref`), which is where
        the next window starts verbatim.
        """
        meta = {k: v for k, v in report.to_dict().items() if k != "id"}
        turn = Turn("compact", report.summary or report.describe(), id=report.id, run=run_id, meta=meta)
        if report.summarized:
            turn.meta["summary"] = True
            turn.meta["keep"] = self.keep_ref(items, report.cut, owners, run_id)
        self.memory.journal(session_id, turn)
        return turn

    @staticmethod
    def keep_ref(items: list[Any], cut: int, owners: list[Turn], run_id: str) -> dict[str, Any]:
        """Where a summary over `items` keeps from, in journal terms.

        The turn behind ``items[cut]`` when the run started with it; else the
        calls of the response the cut keeps from, whose tool turns the run
        journals as they finish.
        """
        if cut < len(owners) and owners[cut].seq:
            return {"seq": owners[cut].seq}
        calls: list[str] = []
        for item in items[cut:]:
            kind = item_kind(item)
            if is_output(kind):
                break
            if is_call(kind) and item.get("call_id"):
                calls.append(str(item["call_id"]))
        return {"run": run_id, "calls": calls} if calls else {}

    def settle_context(self, session_id: str, result: RunResult) -> None:
        """Carry the compaction of a run into the journal once it ended.

        Outputs the run cleared stay cleared for the next run (its prefix then
        matches what the model last saw), and a summary that keeps from the
        calls of a response keeps from their journaled turn. Never raises.
        """
        context = result.context
        if context is None or not self.memory.enabled:
            return
        try:
            if context.fixed:
                self.context_fixed[session_id] = context.fixed
            transcript = self.memory.transcript(session_id)
            changed = self.mark_cleared(transcript.turns, context.state)
            changed += self.mark_folded(transcript.run_of(result.run_id), context)
            for turn in transcript.run_of(result.run_id):
                keep = turn.meta.get("keep") if turn.role == "compact" else None
                if isinstance(keep, dict) and keep.get("calls"):
                    index = transcript.boundary(turn)
                    if index < len(transcript.turns):
                        turn.meta["keep"] = {"seq": transcript.turns[index].seq}
                        changed.append(turn)
            if changed:
                self.memory.persist(transcript, *changed)
        except Exception as exc:  # pragma: no cover - the run already ended
            print(f"[context] could not settle run {result.run_id}: {exc!r}", file=sys.stderr)

    @staticmethod
    def mark_cleared(turns: Iterable[Turn], state: ContextState) -> list[Turn]:
        """Mark the tool turns whose outputs `state` cleared; returns those changed."""
        changed: list[Turn] = []
        if not state.cleared:
            return changed
        for turn in turns:
            stub = state.cleared.get(turn.call_id) if turn.call_id else None
            if stub and turn.meta.get("cleared") != stub:
                turn.meta["cleared"] = stub
                changed.append(turn)
        return changed

    @staticmethod
    def mark_folded(turns: Iterable[Turn], context: ContextRun) -> list[Turn]:
        """Mark the turns of a run whose items its branches folded away, so the
        next run replays the fold; returns those changed."""
        items, state = context.items, context.state
        hidden = context.compactor.hidden(items, state)
        if not hidden:
            return []
        calls = {
            str(items[index].get("call_id") or "")
            for index in hidden
            if is_call(item_kind(items[index]))
        }
        keys = {TokenCounter.key(items[index]) for index in hidden}
        changed: list[Turn] = []
        for turn in turns:
            if turn.meta.get("folded") or not turn.items:
                continue
            if turn.call_id in calls if turn.role == "tool" else all(
                TokenCounter.key(item) in keys for item in turn.items
            ):
                turn.meta["folded"] = True
                changed.append(turn)
        return changed

    async def compacted(self, session_id: str) -> None:
        """Wait for a compaction of the session running in the background."""
        task = self.compacting.get(session_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            await asyncio.wait({task})

    def schedule_compaction(self, session_id: str, config: AgentConfig) -> asyncio.Task[Any] | None:
        """Compact the journal of a session in the background, when it needs it."""
        if not (config.memory_enabled and config.context_trigger > 0):
            return None
        if session_id in self.compacting or not len(self.memory.transcript(session_id)):
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(self.compact(session_id, config=config, background=True))
        self.compacting[session_id] = task

        def done(finished: asyncio.Task[Any]) -> None:
            if self.compacting.get(session_id) is finished:
                del self.compacting[session_id]
            if not finished.cancelled() and finished.exception() is not None:
                print(
                    f"[context] compaction of {session_id} failed: {finished.exception()!r}",
                    file=sys.stderr,
                )

        task.add_done_callback(done)
        return task

    async def compact(
        self,
        session_id: str,
        *,
        config: AgentConfig | None = None,
        force: bool = False,
        focus: str = "",
        background: bool = False,
    ) -> Compaction | None:
        """Compact the journal of a session; the next run starts from the result.

        Past the high watermark (`BACKGROUND_TRIGGER` lower for a compaction
        nobody waits on), or whatever the size when `force` (``/compact``,
        with an optional `focus` for the summary). Returns what was done, or
        ``None`` when nothing was needed.
        """
        config = config or self.config
        if not config.memory_enabled or (config.context_trigger <= 0 and not force):
            return None
        transcript = self.memory.transcript(session_id)
        items, owners = transcript.window(self.memory.tools)
        if not items:
            return None
        summary = transcript.summary()
        state = ContextState(summary=summary.text, cut=1) if summary is not None else ContextState()
        compactor = await self.build_compactor(config)
        await asyncio.to_thread(compactor.counter.load)
        if background:
            budget = compactor.budget
            compactor.budget = ContextBudget.of(
                budget.window, budget.reserve, budget.trigger * self.BACKGROUND_TRIGGER, budget.target
            )
        block = f"{session_id}-compact-{uuid.uuid4().hex[:8]}"

        async def publish(event_type: str, **data: Any) -> None:
            await self.events.publish(event_type, session_id=session_id, **data)

        async def on_compact(report: Compaction, items: list[Any]) -> None:
            changed = self.mark_cleared({id(t): t for t in owners}.values(), state)
            if changed:
                self.memory.persist(transcript, *changed)
            self.journal_compaction(session_id, "", report, items, owners)

        context = ContextRun(
            compactor,
            state=state,
            publish=publish,
            block=lambda n: block,
            on_compact=on_compact,
            throughput=self.throughput(config),
            stall=config.stream_timeout,
            slice_stall=config.slice_stall,
        )
        context.fixed = self.context_fixed.get(session_id) or context.fixed_cost(
            self.decorate_instructions(config.instructions, config), []
        )
        await context.fit(items, force=force, reason="forced" if force else "", focus=focus)
        return context.compactions[-1] if context.compactions else None

    def context_noted(self, event: Event) -> None:
        """Keep the newest `context.usage` of a session for a client that joins."""
        if event.session_id:
            self.context_seen[event.session_id] = dict(event.data)

    async def context_usage(self, session_id: str) -> dict[str, Any] | None:
        """The context use of a session: as last reported, else estimated.

        The estimate needs the window: configured, or already known to the
        pool; otherwise ``None`` until a run reports one.
        """
        seen = self.context_seen.get(session_id)
        if seen is not None or not self.config.memory_enabled:
            return seen
        items = self.memory.history(session_id)
        spec = self.config.model_spec(LEADER_ROLE)
        window = self.config.context_window or (self.models.known_window(spec) if spec else 0)
        if not items or not window:
            return None  # never wait on a provider for a gauge: the next run reports it
        counter = self.counter(self.config)
        await asyncio.to_thread(counter.load)
        fixed = self.context_fixed.get(session_id, 0)
        tokens = counter.scaled(fixed + counter.items(items))
        config = self.config
        return ContextBudget.of(
            window, config.context_reserve, config.context_trigger, config.context_target
        ).usage(tokens)

    @staticmethod
    def prompt_id(result: RunResult) -> str:
        """The block id of the prompt of a run, on the bus and in the journal."""
        return f"{result.session_id}-{result.run_id}-prompt"

    async def publish_prompt(self, config: AgentConfig, result: RunResult) -> None:
        """Show what the user asked as the first block of the run.

        Every client of the session sees it, not only the one that sent it, and
        under the id the journal keeps, so a reconnect replays the same block.
        """
        await self.events.publish(
            EventType.BLOCK_START,
            session_id=result.session_id,
            id=self.prompt_id(result),
            kind="prompt",
            role="user",
            text=(config.input or "").strip(),
            attachments=list(config.extras.get("attachments") or []),
            complete=True,
        )

    def journal_prompt(self, session: Session, config: AgentConfig, result: RunResult) -> None:
        """Write the prompt ahead of the run, marked as running.

        What is kept is the input the user actually wrote (falling back to the
        rendered prompt), not the rendered template, which would be replayed
        verbatim on every turn.
        """
        if not config.memory_enabled:
            return
        asked = (config.input or "").strip() or result.prompt
        self.memory.journal(
            session.id,
            Turn(
                "user",
                asked,
                id=self.prompt_id(result),
                run=result.run_id,
                meta={"status": RUN_RUNNING},
            ),
        )

    async def checkpoint(self, result: RunResult, turn: Turn) -> None:
        """Journal a finished step at once, so a crash later keeps it.

        A tool output is clipped to `ConversationMemory.TOOL_LIMIT` for the
        journal (the run itself saw it whole), and the session lease is
        renewed: a run that is still working keeps its session alive.
        """
        await super().checkpoint(result, turn)
        if not result.session_id or not self.memory.enabled:
            return
        limit = self.memory.TOOL_LIMIT
        items = [
            {**item, "output": clip_output(item["output"], limit)}
            if item.get("type") == "function_call_output" and isinstance(item.get("output"), str)
            else item
            for item in turn.items
        ]
        self.memory.journal(result.session_id, replace(turn, items=items, meta=dict(turn.meta)))
        self.sessions.get(result.session_id)

    async def remember(
        self, session: Session, config: AgentConfig, result: RunResult
    ) -> None:
        """Close the journal of a run that finished with its final answer.

        A model whose final answer never arrived as a message item (or a stream
        that does not report them) still has it journaled here. Override to
        remember more, less, or something else entirely.
        """
        if not config.memory_enabled:
            return
        output = result.output.strip()
        journaled = self.memory.transcript(session.id).run_of(result.run_id)
        if output and not any(t.role == "assistant" and t.text.strip() == output for t in journaled):
            answers = [b for b in result.blocks if b.kind == "output"]
            turn_id = answers[-1].id if answers else f"{result.session_id}-{result.run_id}-answer"
            self.memory.journal(
                session.id, Turn("assistant", output, id=turn_id, run=result.run_id)
            )

    def settle(self, session: Session, result: RunResult) -> None:
        """Record how a run ended on its journaled prompt. Never raises."""
        try:
            transcript = self.memory.transcript(session.id)
            prompt = transcript.prompt_of(result.run_id)
            if prompt is None:
                return
            if result.ok:
                prompt.meta["status"] = RUN_OK
            else:
                cancelled = result.error_kind == ErrorKind.CANCELLED
                prompt.meta["status"] = RUN_CANCELLED if cancelled else RUN_FAILED
                prompt.meta["error"] = result.error
            self.memory.persist(transcript, prompt)
        except Exception as exc:  # pragma: no cover - the run already ended
            print(f"[journal] could not settle run {result.run_id}: {exc!r}", file=sys.stderr)

    def unfinished(self, session_id: str | None) -> Turn | None:
        """The prompt of the newest run of a session when it did not finish.

        A run still marked running with nothing live behind it was interrupted
        (the process died under it); a failed or cancelled one stopped early.
        """
        if not session_id or session_id in self.live:
            return None
        last = self.memory.transcript(session_id).last_run()
        if last is None or last.meta.get("status") == RUN_OK:
            return None
        return last

    #: Follows `RESUME_PROMPT` when the session has a plan with steps open.
    RESUME_PLAN = "The plan of that task, as last recorded:"

    async def resume(self, session: Session | str | None, **overrides: Any) -> RunResult:
        """Continue the unfinished run of a session from its journal.

        A plan with steps still open is recited whole after the prompt (the
        budget note of every call only names the current step).
        """
        session_id = session.id if isinstance(session, Session) else session
        prompt = self.RESUME_PROMPT
        plan = self.plans.get(session_id) if session_id else None
        if plan is not None and plan.current() is not None:
            prompt += f"\n\n{self.RESUME_PLAN}\n{plan.render()}"
        return await self.run(PASSTHROUGH_TEMPLATE, session=session, input=prompt, **overrides)

    def session_closed(self, session: Session) -> None:
        """A session went away: its journal goes, and so does a run still in it.

        May be called off the loop (the sweeper sweeps on a thread), so the
        run is cancelled through its own loop.
        """
        self.memory.forget(session.id)
        self.plans.delete(session.id)
        self.drop_context(session.id)
        task = self.live.get(session.id, (None, None))[1]
        if task is not None and not task.done():
            with contextlib.suppress(RuntimeError):
                task.get_loop().call_soon_threadsafe(task.cancel)

    def drop_context(self, session_id: str) -> None:
        """Forget what compaction knows of a session and stop its background work."""
        self.context_seen.pop(session_id, None)
        self.context_fixed.pop(session_id, None)
        task = self.compacting.get(session_id)
        if task is not None and not task.done():
            with contextlib.suppress(RuntimeError):
                task.get_loop().call_soon_threadsafe(task.cancel)

    def reconcile(self) -> None:
        """Forget the journals and plans of sessions that did not survive a restart."""
        if self.sessions.store is not self.store:
            return
        live = {session.id for session in self.sessions.list()}
        for session_id, _record in self.store.list(ConversationMemory.NAMESPACE):
            if session_id not in live:
                self.memory.forget(session_id)
        for session_id, _record in self.store.list(PlanBook.NAMESPACE):
            if session_id not in live:
                self.plans.delete(session_id)

    # -- rules --------------------------------------------------------------

    def build_distiller(
        self, config: AgentConfig
    ) -> Callable[[list[dict[str, str]]], Awaitable[str]] | None:
        """Return the delegate that turns feedback into a rule, or ``None``.

        It uses the `summary` role, which falls back to the leader.
        """
        spec = config.model_spec(SUMMARY_ROLE) or config.model_spec(LEADER_ROLE)
        if spec is None:
            return None
        policy = self.build_policy(config)

        async def distill(messages: list[dict[str, str]]) -> str:
            return await self.models.complete(
                spec,
                messages,
                max_tokens=config.rules_max_tokens or None,
                policy=policy,
                on_retry=self.retry_reporter(),
            )

        return distill

    async def feedback(
        self,
        session_id: str | None,
        vote: str,
        *,
        target: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Learn a rule from a vote (``up`` or ``down``) on an answer.

        `target` is the block id of the answer; without one the newest answer
        of the session is meant. The trace is the run behind the answer, up to
        it. An answer is rated once. The outcome is returned and published as
        `feedback`, so every client of the session marks the answer.
        """
        vote = {"good": "up", "bad": "down"}.get(vote, vote)
        outcome: dict[str, Any] = {"ok": False, "id": target or "", "vote": vote}
        answer: Turn | None = None
        transcript = self.memory.transcript(session_id) if session_id else None
        try:
            if vote not in RULE_VOTES:
                outcome["message"] = f"Unknown vote: {vote!r} (up or down)"
                return outcome
            if not self.config.rules_enabled:
                outcome["message"] = "Learned rules are disabled (rules_enabled)"
                return outcome
            if transcript is not None:
                answer = transcript.find(target) if target else next(
                    (t for t in reversed(transcript.turns) if t.role == "assistant"), None
                )
            if answer is None or answer.role != "assistant":
                outcome["message"] = "No answer to rate"
                return outcome
            outcome["id"] = answer.id
            if answer.meta.get("vote"):
                outcome["message"] = f"Already rated {answer.meta['vote']}"
                return outcome
            distill = self.build_distiller(self.config)
            if distill is None:
                outcome["message"] = "No model to learn with"
                return outcome
            answer.meta["vote"] = vote  # claimed now, so a second click waits for nothing
            assert transcript is not None
            turns = transcript.run_of(answer.run) if answer.run else []
            trace = turns[: turns.index(answer) + 1] if answer in turns else [answer]
            try:
                action, rule = await self.rules.learn(
                    vote, trace, distill, note=note, instructions=self.config.rules_instructions
                )
            except BaseException as exc:
                answer.meta.pop("vote", None)
                if not isinstance(exc, Exception):
                    raise  # cancelled: nothing was learned, nothing is claimed
                outcome["message"] = f"Could not learn a rule: {exc}"
                return outcome
            self.memory.persist(transcript, answer)
            verb = "added" if action == "add" else "updated"
            outcome.update(
                ok=True,
                action=action,
                rule=rule.to_dict(),
                message=f"Rule {verb} ({rule.id}): {rule.line()}",
            )
            warning = self.rules.warning()
            if warning:
                outcome["warning"] = warning
                outcome["message"] += f"\n{warning}"
            return outcome
        finally:
            await self.events.publish(EventType.FEEDBACK, session_id=session_id, **outcome)

    # -- repository ---------------------------------------------------------

    def checkout(self, session: Session | None) -> Checkout | None:
        """Return the git checkout of `session`, when it has one."""
        meta = (session.meta.get("repo") if session else None) or {}
        return Checkout.from_meta(self.repos, meta) if meta.get("path") else None

    async def prepare_session(self, session: Session) -> Checkout | None:
        """Clone the default repository into a fresh session workspace.

        Idempotent: a session that already owns a checkout keeps it, and a run
        is never blocked by a clone failure (it is reported and the session
        simply has no checkout).
        """
        existing = self.checkout(session)
        if existing is not None or not self.repos.configured:
            return existing
        if not self.config.repo_clone:
            return None
        try:
            checkout = await self.repos.clone(session.workspace, session_id=session.id)
        except RepoError as exc:
            await self.events.publish(
                EventType.LOG,
                session_id=session.id,
                kind="error",
                message=f"clone failed: {exc}",
            )
            return None
        session.meta["repo"] = checkout.to_dict()
        self.sessions.update(session)
        await self.events.publish(
            EventType.SESSION_OPEN,
            session_id=session.id,
            kind="log",
            repo=checkout.to_dict(),
        )
        return checkout

    # -- commands -----------------------------------------------------------

    def register_default_commands(self) -> None:
        """Register the built-in slash commands. Override to add your own."""

        def help_command(_args: str, **_: Any) -> dict[str, Any]:
            return {"ok": True, "commands": self.commands.describe()}

        async def new_session(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            previous = self.sessions.get(session_id) if session_id else None
            client = previous.meta.get("client") if previous else None
            if session_id:
                self.sessions.close(session_id)
                self.memory.forget(session_id)
            session = self.sessions.create(**({"client": client} if client else {}))
            checkout = await self.prepare_session(session)
            message = "New session"
            if checkout is not None:
                message += f" on branch {checkout.branch} of {self.repos.spec.slug}"
            return {"ok": True, "session": session.to_dict(), "message": message}

        def end_session(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            target = args or session_id or ""
            closed = self.sessions.close(target)
            if closed:
                self.memory.forget(target)
            return {
                "ok": closed,
                "session": None,
                "message": "Session closed" if closed else "No such session",
            }

        def forget_command(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            if not session_id:
                return {"ok": False, "message": "No session"}
            remembered = len(self.memory.transcript(session_id))
            self.drop_context(session_id)
            self.memory.forget(session_id)
            self.plans.delete(session_id)
            return {"ok": True, "message": f"Forgot {remembered} remembered turns"}

        def list_sessions(_args: str, **_: Any) -> dict[str, Any]:
            return {"ok": True, "sessions": [s.to_dict() for s in self.sessions.list()]}

        def use_session(args: str, **_: Any) -> dict[str, Any]:
            session = self.sessions.get(args.strip())
            if session is None:
                return {"ok": False, "message": f"No such session: {args.strip()}"}
            return {"ok": True, "session": session.to_dict(), "message": "Session switched"}

        def model_command(args: str, **_: Any) -> dict[str, Any]:
            if args:
                self.config = self.config.merge(inference_llm=args.strip())
            return {"ok": True, "message": f"Model: {self.config.inference_llm}"}

        def vision_command(args: str, **_: Any) -> dict[str, Any]:
            if args:
                value = args.strip()
                self.config = self.config.merge(
                    inference_vlm="" if value in ("none", "off") else value
                )
            spec = self.config.model_spec(VISION_ROLE)
            name = spec.name if spec is not None else "(leader)"
            return {"ok": True, "message": f"Vision model: {name}"}

        def current_session(session_id: str | None) -> Session | None:
            return self.sessions.get(session_id)

        def repo_command(args: str, **_: Any) -> dict[str, Any]:
            if args:
                value = args.strip()
                self.config = self.config.merge(
                    repo_url="" if value in ("none", "off") else value
                )
                self.repos.spec = RepoSpec.from_config(self.config)
            url = self.repos.spec.url or "(unset)"
            return {"ok": True, "message": f"Repository: {url}"}

        async def clone_command(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            session = current_session(session_id)
            if session is None:
                return {"ok": False, "message": "No live session"}
            if not self.repos.configured:
                return {"ok": False, "message": "No repository is configured"}
            try:
                checkout = await self.prepare_session(session)
            except RepoError as exc:
                return {"ok": False, "message": str(exc)}
            if checkout is None:
                return {"ok": False, "message": "The repository could not be cloned"}
            return {
                "ok": True,
                "session": session.to_dict(),
                "message": (
                    f"Cloned {self.repos.spec.slug} into {checkout.path.name} "
                    f"on branch {checkout.branch}"
                ),
            }

        async def repo_action(
            session_id: str | None, action: Callable[[Checkout], Awaitable[str]]
        ) -> dict[str, Any]:
            checkout = self.checkout(current_session(session_id))
            if checkout is None:
                return {"ok": False, "message": "This session has no git checkout"}
            try:
                return {"ok": True, "message": await action(checkout)}
            except RepoError as exc:
                return {"ok": False, "message": str(exc)}

        async def status_command(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            return await repo_action(session_id, lambda c: c.status())

        async def commit_command(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            return await repo_action(session_id, lambda c: c.commit(args))

        async def push_command(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            return await repo_action(session_id, lambda c: c.push())

        async def pull_request_command(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            title, _, body = args.partition("\n")

            async def open_pull(checkout: Checkout) -> str:
                pull = await checkout.pull_request(title.strip(), body.strip())
                return f"Pull request #{pull['number']}: {pull['url']}"

            return await repo_action(session_id, open_pull)

        async def publish_command(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            title, _, body = args.partition("\n")

            async def publish(checkout: Checkout) -> str:
                result = await checkout.publish(title.strip(), body=body.strip())
                pull = result["pull_request"]
                return f"{result['commit']} -> pull request #{pull['number']}: {pull['url']}"

            return await repo_action(session_id, publish)

        async def good_command(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            return await self.feedback(session_id, "up", note=args)

        async def bad_command(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            return await self.feedback(session_id, "down", note=args)

        def rules_command(args: str, **_: Any) -> dict[str, Any]:
            action, _, target = args.strip().partition(" ")
            if action == "drop":
                dropped = self.rules.drop(target)
                return {
                    "ok": dropped,
                    "message": f"Dropped rule {target}" if dropped else f"No such rule: {target}",
                }
            if action == "clear":
                return {"ok": True, "message": f"Dropped {self.rules.clear()} rules"}
            if action:
                return {"ok": False, "message": "Usage: /rules [drop <id> | clear]"}
            rules = self.rules.rules()
            lines = [f"- `{r.id}` {r.line()}" for r in rules] or ["_no learned rules_"]
            lines.append(f"{self.rules.size:,} of {self.rules.max_chars:,} chars")
            warning = self.rules.warning()
            return {
                "ok": True,
                "message": "\n".join([*lines, *([warning] if warning else [])]),
                "rules": [r.to_dict() for r in rules],
            }

        async def compact_command(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            if not session_id:
                return {"ok": False, "message": "No session"}
            if session_id in self.live:
                return {"ok": False, "message": "A run is in progress"}
            await self.compacted(session_id)
            report = await self.compact(session_id, force=True, focus=args.strip())
            if report is None:
                return {"ok": True, "message": "Nothing to compact"}
            return {"ok": True, "message": report.describe(), "compaction": report.to_dict()}

        def plan_command(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "message": self.plan_report(session_id),
                "plan": self.plan_view(session_id),
            }

        def resume_command(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            if session_id and session_id in self.live:
                return {"ok": False, "message": "A run is in progress"}
            unfinished = self.unfinished(session_id)
            if unfinished is None:
                return {"ok": False, "message": "Nothing to resume"}
            state = {RUN_RUNNING: "was interrupted", RUN_FAILED: "failed"}.get(
                unfinished.meta.get("status"), "was cancelled"
            )
            # The front end starts the run: it owns the task and the stream.
            return {"ok": True, "resume": True, "message": f"Resuming the run that {state}"}

        self.commands.register("help", "List the available commands", help_command)
        self.commands.register("good", "Rate the last answer good: '/good [why]'", good_command)
        self.commands.register("bad", "Rate the last answer bad: '/bad [why]'", bad_command)
        self.commands.register(
            "rules", "List learned rules: '/rules [drop <id> | clear]'", rules_command
        )
        self.commands.register("resume", "Continue the run that did not finish", resume_command)
        self.commands.register("plan", "Show the plan and how fast the model reads", plan_command)
        self.commands.register(
            "compact", "Summarise the conversation now: '/compact [focus]'", compact_command
        )
        self.commands.register("new", "Start a new isolated session", new_session)
        self.commands.register("end", "End the current session and wipe it", end_session)
        self.commands.register("sessions", "List live sessions", list_sessions)
        self.commands.register("use", "Switch to an existing session", use_session)
        self.commands.register(
            "forget", "Forget the conversation of this session", forget_command
        )
        self.commands.register("model", "Show or set the model", model_command)
        self.commands.register(
            "vision", "Show or set the vision model ('none' to unset)", vision_command
        )
        self.commands.register(
            "repo", "Show or set the default repository ('none' to unset)", repo_command
        )
        self.commands.register(
            "clone", "Clone the default repository into this session", clone_command
        )
        self.commands.register("status", "Show the git status of this session", status_command)
        self.commands.register("commit", "Commit every change with a message", commit_command)
        self.commands.register("push", "Push the session branch to the remote", push_command)
        self.commands.register(
            "pr", "Open a pull request: '/pr <title>' then an optional body", pull_request_command
        )
        self.commands.register(
            "publish", "Commit, push and open a pull request in one step", publish_command
        )

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Release every resource: workspaces are wiped, storage is closed.

        A durable manager keeps its workspaces and their records instead, so the
        next start rehydrates the sessions that were still live.
        """
        for task in list(self.compacting.values()):
            with contextlib.suppress(RuntimeError):
                task.get_loop().call_soon_threadsafe(task.cancel)
        self.sessions.close_all()
        self.cache.clear()
        self.store.close()
        super().close()

    async def aclose(self) -> None:
        await self.sessions.stop_sweeper()
        await asyncio.to_thread(self.close)

    # -- application entry points -------------------------------------------

    async def serve(self, **overrides: Any) -> None:
        """Run the REST + websocket server backed by this agent."""
        await WebServer(self, **overrides).serve()

    def repl(self, **overrides: Any) -> "Repl":
        """The interactive terminal for this agent. Override to swap the class."""
        return Repl(self, **overrides)

    @classmethod
    def cli(cls, template: str | None = None, argv: list[str] | None = None, **kwargs: Any) -> int:
        """Run this agent as a CLI application. Returns a process exit code.

        Without a template the one `load_template` resolves is used, which the
        config file, ``AGENTIC_TEMPLATE`` or an ``agentic_prompt.md`` beside the
        module can all supply.
        """
        args = parse_args(argv)
        kwargs.setdefault("config_file", args.config or True)
        kwargs.setdefault("mode", mode_of(args))
        agent = cls(**kwargs)
        return asyncio.run(agent.execute(template or load_template(agent.config), args))

    async def execute(self, template: str, args: argparse.Namespace) -> int:
        """Dispatch parsed CLI arguments to the repl, the run or the serve path.

        ``--serve`` wins, then an explicit ``--repl``, then ``--input``; with no
        arguments at all the terminal opens, because a harness with nothing to
        do is a harness waiting to be talked to.
        """
        self.template = template
        try:
            if args.serve:
                await self.serve()
                return 0
            if args.repl or not args.input:
                return await self.repl().start(opening=args.input)
            result = await self.run(template, input=args.input)
            return 0 if result.ok else 1
        finally:
            await self.aclose()


# ---------------------------------------------------------------------------
# Repl
#
# The terminal client: the same loop the web layer drives, reading lines from a
# terminal instead of a websocket. It wraps an agent harness instance and owns
# nothing of its own but the session it is pointed at.
# ---------------------------------------------------------------------------


class Repl:
    """An interactive terminal for an agent harness.

    Wrap any `Agent` (or a subclass) and call `start`::

        agent = Agent()
        await Repl(agent).start()

    A line that begins with ``/`` is dispatched to the agent's command registry,
    exactly as the web client dispatches it, and anything else is a prompt: it
    runs the agent's template in the current session and streams the answer
    through the agent's console renderer. The conversation is remembered, so the
    session carries from one prompt to the next until ``/new`` or ``/end``.

    Lines are read on a dedicated thread, so the event loop keeps running while
    the terminal waits: timers fire, the session sweeper sweeps and a run that is
    already streaming is never blocked by the prompt. Ctrl-C cancels the active
    run and leaves the repl open; Ctrl-D, ``/exit`` or ``/quit`` leaves it.
    """

    #: Commands the repl answers itself instead of passing to the agent.
    EXITS = ("exit", "quit")
    PROMPT = "> "
    #: The prompt a question of the model is answered at.
    ANSWER = "answer> "

    def __init__(
        self,
        agent: "Agent",
        *,
        template: str | None = None,
        prompt: str | None = None,
        stream: Any = None,
        reader: Callable[[str], str] | None = None,
    ) -> None:
        self.agent = agent
        # a terminal user is on their own machine and at the keyboard
        agent.config = agent.config.with_defaults(**MODE_TOOLS["repl"])
        self.template = (
            template or getattr(agent, "template", None) or PASSTHROUGH_TEMPLATE
        )
        self.prompt = self.PROMPT if prompt is None else prompt
        self.stream = stream or sys.stdout
        #: Reads one line, given the prompt to show. Injected by the tests.
        self.reader = reader or self.read_line
        self.session: Session | None = None
        self.task: "asyncio.Task[Any] | None" = None
        self.running = False
        #: The task reading the answer to each open question, by question id.
        self.replies: dict[str, "asyncio.Task[None]"] = {}
        self._replying = asyncio.Lock()  # one question at the terminal at a time
        self._asks: "queue.Queue[str | None]" = queue.Queue()
        self._lines: "asyncio.Queue[str | None] | None" = None
        self._thread: threading.Thread | None = None

    # -- loop ---------------------------------------------------------------

    async def start(self, opening: str = "") -> int:
        """Read and answer until the user leaves. Returns a process exit code.

        `opening` is answered first, which is how ``--repl --input ...`` hands
        the command line prompt to an interactive session.
        """
        resumed = self.agent.sessions.latest(client=self.CLIENT)
        self.session = resumed or self.agent.sessions.create(client=self.CLIENT)
        await self.agent.prepare_session(self.session)
        self.agent.sessions.start_sweeper()
        self.greet()
        self.recap(resumed is not None)
        disarm = self.arm_interrupt()
        unsubscribe = [
            self.agent.events.on(EventType.QUESTION_START, self.asked),
            self.agent.events.on(EventType.QUESTION_END, self.answered),
        ]
        self.running = True
        try:
            if opening.strip():
                await self.dispatch(opening.strip())
            while self.running:
                line = await self.read()
                if line is None:  # Ctrl-D
                    self.emit("output", "\n")
                    break
                if line.strip():
                    await self.dispatch(line.strip())
        finally:
            self.running = False
            for off in unsubscribe:
                off()
            for reply in list(self.replies.values()):
                reply.cancel()
            disarm()
            self.stop_reader()
        return 0

    async def dispatch(self, text: str) -> None:
        """Answer one line: a slash command, or a prompt for the agent."""
        parsed = CommandRegistry.parse(text)
        if parsed is None:
            await self.ask(text)
            return
        await self.command(text, parsed[0])

    async def ask(self, text: str, *, resume: bool = False) -> None:
        """Run one prompt in the current session, streaming the answer.

        With `resume` the unfinished run of the session is continued instead.
        """
        session = self.current()
        self.task = asyncio.create_task(
            self.agent.resume(session) if resume
            else self.agent.run(self.template, session=session, input=text)
        )
        try:
            await self.task
        except asyncio.CancelledError:
            self.emit("error", "\n(cancelled)\n")
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # the repl itself is going away, not just this run
        except Exception as exc:  # the run reported it; the repl stays open
            self.emit("error", f"{exc}\n")
        finally:
            self.task = None

    async def command(self, text: str, name: str = "") -> None:
        """Invoke a slash command and report what it returned."""
        name = name or (CommandRegistry.parse(text) or ("", ""))[0]
        if name in self.EXITS:
            self.running = False
            return
        result = await self.agent.commands.invoke(
            text, session_id=self.session.id if self.session else None, repl=self
        )
        self.adopt(result)
        self.report(result)
        if isinstance(result, dict) and result.get("ok") and result.get("resume"):
            await self.ask("", resume=True)

    # -- session ------------------------------------------------------------

    #: Tag on the sessions this front end opens, so a restart resumes its own.
    CLIENT = "repl"
    #: Remembered turns shown when a session is resumed.
    RECAP_TURNS = 4

    def current(self) -> Session:
        """The session prompts run in, replacing one that has gone away."""
        live = self.agent.sessions.get(self.session.id) if self.session else None
        if live is None:
            live = self.agent.sessions.create(client=self.CLIENT)
        self.session = live
        return live

    def recap(self, resumed: bool) -> None:
        """After the banner: where a resumed session left off, and warnings."""
        if resumed and self.session is not None:
            transcript = self.agent.memory.transcript(self.session.id)
            spoken = [t for t in transcript.turns if t.role in ("user", "assistant")]
            if spoken:
                self.emit(
                    "tool",
                    f"Resumed session {self.session.id}: {len(transcript)} turns remembered "
                    f"(/new for a fresh one)\n",
                )
                for turn in spoken[-self.RECAP_TURNS:]:
                    mark = ">" if turn.role == "user" else "<"
                    self.emit("tool", f"{mark} {truncate(' '.join(turn.text.split()), 200)}\n")
            unfinished = self.agent.unfinished(self.session.id)
            if unfinished is not None:
                self.emit(
                    "error",
                    f"The last run did not finish ({unfinished.meta.get('status')}): "
                    f"/resume to continue it\n",
                )
        warning = self.agent.rules.warning() if self.agent.config.rules_enabled else None
        if warning:
            self.emit("error", f"{warning}\n")

    def adopt(self, result: Any) -> None:
        """Follow a command that started, switched or ended a session."""
        if not isinstance(result, dict) or "session" not in result:
            return
        session = result.get("session")
        if isinstance(session, dict):
            self.session = self.agent.sessions.get(str(session.get("id"))) or self.session
        elif session is None and result.get("ok"):
            self.session = None  # ended; the next prompt opens a fresh one

    # -- questions ----------------------------------------------------------

    def asked(self, event: Event) -> None:
        """`question.start`: read the answer at the terminal, off the bus.

        Only questions of the session the repl is in are answered here, and the
        reading happens on a task of its own, so the run that asked keeps the
        loop while the person types.
        """
        if self.session is None or event.session_id != self.session.id:
            return
        question_id = str(event.data.get("id", ""))
        if question_id and question_id not in self.replies:
            self.replies[question_id] = asyncio.get_running_loop().create_task(
                self.reply(question_id, event.data)
            )

    def answered(self, event: Event) -> None:
        """`question.end`: stop reading for a question that closed without us.

        A cancelled run takes its question with it; the line the person is
        still typing then goes to the next prompt instead.
        """
        reply = self.replies.pop(str(event.data.get("id", "")), None)
        if reply is not None and not reply.done() and reply is not asyncio.current_task():
            reply.cancel()

    async def reply(self, question_id: str, data: dict[str, Any]) -> None:
        """Print a question, read the answer and hand it to the agent."""
        try:
            async with self._replying:
                options = [str(option) for option in data.get("options") or []]
                lines = [str(data.get("question") or "").strip() or "?"]
                lines += [f"  {n}. {option}" for n, option in enumerate(options, 1)]
                self.emit("question", "\n".join(lines) + "\n")
                line = await self.read(self.ANSWER)
                self.agent.answer(question_id, self.choice(line, options))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken terminal must not hang the run
            self.emit("error", f"question failed: {exc}\n")
            self.agent.answer(question_id, "")
        finally:
            self.replies.pop(question_id, None)

    @staticmethod
    def choice(line: str | None, options: list[str]) -> str:
        """The answer a line stands for: an option by its number, else the line."""
        text = (line or "").strip()
        if text.isdigit() and 1 <= int(text) <= len(options):
            return options[int(text) - 1]
        return text

    # -- input --------------------------------------------------------------

    def read_line(self, prompt: str) -> str | None:
        """Read one line from the terminal. Returns ``None`` at end of input."""
        try:
            return input(prompt)
        except EOFError:
            return None

    async def read(self, prompt: str | None = None) -> str | None:
        """Ask the reader thread for the next line, without blocking the loop.

        Every request is answered by exactly one line, in order, so a reader
        that gives up (a question that closed) leaves its line to the next one.
        """
        if self._lines is None:
            self._lines = asyncio.Queue()
            self.start_reader(asyncio.get_running_loop())
        self._asks.put(self.prompt if prompt is None else prompt)
        return await self._lines.get()

    def start_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the one thread that reads the terminal for this repl."""
        if self._thread is not None:
            return
        with contextlib.suppress(ImportError):
            import readline  # noqa: F401 - line editing and history for input()

        self._thread = threading.Thread(
            target=self.pump, args=(loop,), name="repl-reader", daemon=True
        )
        self._thread.start()

    def pump(self, loop: asyncio.AbstractEventLoop) -> None:
        """Reader thread: one line per request, delivered back to the loop."""
        while True:
            prompt = self._asks.get()
            if prompt is None:
                return
            try:
                line = self.reader(prompt)
            except (EOFError, KeyboardInterrupt):
                line = None
            except Exception:  # pragma: no cover - a broken terminal
                line = None
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self.deliver, line)

    def deliver(self, line: str | None) -> None:
        """Hand a line the reader produced to whoever is waiting for it."""
        if self._lines is not None:
            self._lines.put_nowait(line)

    def stop_reader(self) -> None:
        """Release the reader thread. A blocked ``input()`` ends with stdin."""
        self._asks.put(None)

    # -- interrupts ---------------------------------------------------------

    def arm_interrupt(self) -> Callable[[], None]:
        """Make Ctrl-C cancel the active run instead of killing the process.

        Returns the undo. The loop handler is used where there is one (POSIX)
        and a plain signal handler otherwise, and a platform that allows neither
        simply keeps its default behaviour.
        """
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signal.SIGINT, self.interrupt)
            return lambda: _quietly(loop.remove_signal_handler, signal.SIGINT)
        previous = signal.getsignal(signal.SIGINT)

        def handler(_signum: int, _frame: Any) -> None:
            loop.call_soon_threadsafe(self.interrupt)

        with contextlib.suppress(ValueError, OSError, AttributeError):
            signal.signal(signal.SIGINT, handler)
            return lambda: _quietly(signal.signal, signal.SIGINT, previous)
        return lambda: None

    def interrupt(self) -> None:
        """Ctrl-C: cancel the run in flight, or remind an idle prompt."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            return
        self.emit("error", f"\n(/exit or Ctrl-D to leave)\n{self.prompt}")

    # -- output -------------------------------------------------------------

    def emit(self, kind: str, text: str) -> None:
        """Write through the agent's renderer so spacing stays consistent."""
        if not text:
            return
        renderer = getattr(self.agent, "renderer", None)
        if renderer is not None:
            renderer.emit(kind, text)
            return
        print(text, end="", file=self.stream, flush=True)

    def greet(self) -> None:
        """Print the banner the repl opens with."""
        renderer = getattr(self.agent, "renderer", None)
        if renderer is None:
            return
        renderer.banner(f"{self.agent.config.name} (repl)", self.banner_items())
        renderer.banners = False  # one banner per repl, not one per run

    def banner_items(self) -> list[tuple[str, str]]:
        """Key/value rows for the opening banner."""
        items = [
            item for item in self.agent.config.banner_items() if item[0] != "Input"
        ]
        file = getattr(self.agent, "config_file", None)
        if file is not None:
            items.append(("Config", str(file)))
        file = getattr(self.agent, "env_file", None)
        if file is not None:
            items.append(("Env", str(file)))
        if self.session is not None:
            items.append(("Session", self.session.id))
            checkout = self.agent.checkout(self.session)
            if checkout is not None:
                items.append(("Branch", checkout.branch))
        items.append(("Commands", "/help, /exit"))
        return items

    def describe(self) -> list[dict[str, str]]:
        """The agent's commands plus the ones the repl answers itself."""
        own = [{"name": "exit", "description": "Leave the repl (/quit, Ctrl-D)"}]
        return sorted(
            [*self.agent.commands.describe(), *own], key=lambda c: c["name"]
        )

    def report(self, result: Any) -> None:
        """Print what a command returned: a listing, a message, or both."""
        if result is None:
            self.emit("error", "Not a command\n")
            return
        if not isinstance(result, dict):
            self.emit("output", f"{result}\n")
            return
        lines: list[str] = []
        if result.get("commands") is not None:
            width = max((len(c["name"]) for c in self.describe()), default=0)
            lines += [
                f"/{c['name']:<{width}}  {c['description']}" for c in self.describe()
            ]
        for session in result.get("sessions") or []:
            marker = "*" if self.session and session["id"] == self.session.id else " "
            lines.append(f"{marker} {session['id']}  {session['workspace']}")
        message = str(result.get("message") or "")
        if message:
            lines.append(message)
        if not lines:
            lines.append("ok" if result.get("ok", True) else "failed")
        self.emit("output" if result.get("ok", True) else "error", "\n".join(lines) + "\n")


def _quietly(action: Callable[..., Any], *args: Any) -> None:
    """Undo a handler installation without caring that it is already gone."""
    with contextlib.suppress(ValueError, RuntimeError, OSError, NotImplementedError):
        action(*args)


# ---------------------------------------------------------------------------
# Web
#
# A tiny REST surface for read only state plus a websocket hub (channels,
# subscriptions and named message handlers, in the spirit of SignalR and
# socket.io) that streams block events to connected clients.
# ---------------------------------------------------------------------------

#: CSS variable to the VS Code colors it is taken from, best first. A theme
#: names whatever it likes, so each variable keeps a list of fallbacks and the
#: client stays on its own default for anything the file never mentions. The
#: same table lives in `ui.js` for themes loaded in the browser.
THEME_KEYS = {
    "--bg": ("editor.background",),
    "--fg": ("editor.foreground", "foreground"),
    "--bg-soft": (
        "sideBar.background",
        "editorGroupHeader.tabsBackground",
        "activityBar.background",
    ),
    "--bg-raised": (
        "editorWidget.background",
        "dropdown.background",
        "menu.background",
        "input.background",
    ),
    "--input-bg": ("input.background", "editorWidget.background"),
    "--input-fg": ("input.foreground", "editor.foreground", "foreground"),
    "--input-border": (
        "input.border",
        "editorWidget.border",
        "panel.border",
        "contrastBorder",
    ),
    "--border": (
        "panel.border",
        "editorGroup.border",
        "editorWidget.border",
        "contrastBorder",
        "input.border",
    ),
    "--accent": ("button.background", "focusBorder"),
    "--accent-fg": ("button.foreground",),
    "--focus": ("focusBorder", "button.background"),
    "--muted": (
        "descriptionForeground",
        "editorLineNumber.foreground",
        "disabledForeground",
    ),
    "--error": (
        "errorForeground",
        "editorError.foreground",
        "inputValidation.errorBorder",
    ),
    "--warn": (
        "editorWarning.foreground",
        "list.warningForeground",
        "inputValidation.warningBorder",
    ),
    "--link": ("textLink.foreground", "textLink.activeForeground"),
    "--code-bg": ("textCodeBlock.background", "editorWidget.background"),
    "--badge-bg": ("badge.background",),
    "--badge-fg": ("badge.foreground",),
    "--scroll": ("scrollbarSlider.background",),
}

#: Variables that are worthless unless they differ from the page background: a
#: code surface the color of the page is not a surface. When every candidate a
#: theme names is the background, the variable is left out and the stylesheet
#: keeps its own, which is a shade apart by construction.
THEME_DISTINCT = ("--code-bg",)

#: The theme kinds VS Code writes, mapped onto the ones the stylesheet knows.
THEME_TYPES = {
    "light": "light",
    "hc": "hc-dark",
    "hcdark": "hc-dark",
    "hc-dark": "hc-dark",
    "hclight": "hc-light",
    "hc-light": "hc-light",
}

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def theme_type(raw: Any) -> str:
    """Normalise a VS Code theme kind. Anything unknown is treated as dark."""
    value = re.sub(r"[\s_]+", "-", str(raw or "").strip().lower())
    return THEME_TYPES.get(value, "dark")


def load_vscode_theme(path: Path | str | None) -> dict[str, str]:
    """Map a VS Code theme file onto the CSS variables the client understands.

    Each variable takes the first color the file actually defines out of its
    candidates, so a theme that names only half the palette still reads as
    itself; the rest stays on the stylesheet default for its theme type.
    """
    if not path:
        return {}
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    colors = data.get("colors") or {}

    def pick(keys: Iterable[str], unlike: str | None = None) -> str | None:
        for key in keys:
            value = colors.get(key)
            if value and str(value) != unlike:
                return str(value)
        return None

    background = pick(THEME_KEYS["--bg"])
    theme: dict[str, str] = {}
    for css, keys in THEME_KEYS.items():
        value = pick(keys, background if css in THEME_DISTINCT else None)
        if value is not None:
            theme[css] = value
    theme["--theme-type"] = theme_type(data.get("type"))
    return theme


def safe_name(name: str) -> str:
    """Reduce an arbitrary client supplied file name to a safe leaf name."""
    leaf = Path(str(name or "file")).name
    leaf = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf).strip("._") or "file"
    return leaf[:128]


class Connection:
    """One websocket client."""

    def __init__(self, hub: "Hub", websocket: Any) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.hub = hub
        self.ws = websocket
        self.channels: set[str] = set()
        self.session_id: str | None = None
        self.task: asyncio.Task[Any] | None = None

    async def send(self, message_type: str, **data: Any) -> None:
        payload = json.dumps({"type": message_type, **data})
        with contextlib.suppress(Exception):
            await self.ws.send(payload)

    async def fail(self, message: str) -> None:
        await self.send("error", message=message)

    async def cancel_task(self) -> bool:
        """Cancel the in-flight agent run, if any."""
        task, self.task = self.task, None
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True


class Hub:
    """Channel based websocket hub with named message handlers."""

    def __init__(self) -> None:
        self.connections: dict[str, Connection] = {}
        self.channels: dict[str, set[str]] = {}
        self._handlers: dict[str, Callable[[Connection, dict[str, Any]], Any]] = {}

    def on(self, message_type: str, handler: Callable[[Connection, dict[str, Any]], Any]) -> None:
        self._handlers[message_type] = handler

    def join(self, connection: Connection, channel: str) -> None:
        self.channels.setdefault(channel, set()).add(connection.id)
        connection.channels.add(channel)

    def leave(self, connection: Connection, channel: str) -> None:
        self.channels.get(channel, set()).discard(connection.id)
        connection.channels.discard(channel)

    async def broadcast(self, channel: str, message_type: str, **data: Any) -> None:
        for connection_id in list(self.channels.get(channel, ())):
            connection = self.connections.get(connection_id)
            if connection is not None:
                await connection.send(message_type, **data)

    async def send_all(self, message_type: str, **data: Any) -> None:
        for connection in list(self.connections.values()):
            await connection.send(message_type, **data)

    async def dispatch(self, connection: Connection, message: dict[str, Any]) -> None:
        handler = self._handlers.get(str(message.get("type", "")))
        if handler is None:
            await connection.fail(f"Unknown message type: {message.get('type')}")
            return
        result = handler(connection, message)
        if inspect.isawaitable(result):
            await result

    async def serve_connection(self, websocket: Any) -> None:
        """Read/dispatch loop for a single client."""
        from websockets.exceptions import ConnectionClosed

        connection = Connection(self, websocket)
        self.connections[connection.id] = connection
        await self.connected(connection)
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if len(raw) > MAX_MESSAGE_BYTES:
                    await connection.fail("Message too large")
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await connection.fail("Invalid JSON")
                    continue
                if not isinstance(message, dict):
                    await connection.fail("Invalid message")
                    continue
                await self.dispatch(connection, message)
        except ConnectionClosed:
            pass
        finally:
            await connection.cancel_task()
            for channel in list(connection.channels):
                self.leave(connection, channel)
            self.connections.pop(connection.id, None)
            await self.disconnected(connection)

    # -- overridable lifecycle hooks ---------------------------------------

    async def connected(self, connection: Connection) -> None:
        await connection.send("ready", connection=connection.id)

    async def disconnected(self, connection: Connection) -> None:
        return None


class WebServer:
    """Serves the chat page, a read only REST API and the websocket hub.

    State changing operations (prompts, session management, cancellation) all
    travel over the hub so the client keeps a single ordered channel.
    """

    ASSETS = {
        "/": (f"{STEM}_ui.html", "text/html; charset=utf-8"),
        "/index.html": (f"{STEM}_ui.html", "text/html; charset=utf-8"),
        "/ui.css": (f"{STEM}_ui.css", "text/css; charset=utf-8"),
        "/ui.js": (f"{STEM}_ui.js", "text/javascript; charset=utf-8"),
    }

    def __init__(
        self,
        agent: "Agent",
        *,
        host: str | None = None,
        port: int | None = None,
        template: str | None = None,
        assets_root: Path | None = None,
        hub: Hub | None = None,
    ) -> None:
        self.agent = agent
        # a web client may be anyone who reaches the port: reads and writes stay
        # confined; the page answers the questions of the model on the discussion
        agent.config = agent.config.with_defaults(**MODE_TOOLS["web"])
        self.host = host or agent.config.host
        self.port = port or agent.config.port
        self.template = template or getattr(agent, "template", None) or PASSTHROUGH_TEMPLATE
        self.assets_root = assets_root or ROOT
        self.hub = hub or Hub()
        self.theme = load_vscode_theme(agent.config.theme)
        #: The run in flight per session. A run belongs to its session, not to
        #: the socket that started it: a dropped connection leaves it running,
        #: and whoever says `hello` to the session next picks it up.
        self.runs: dict[str, asyncio.Task[Any]] = {}
        #: Background work (rule learning) kept referenced until it finishes.
        self.tasks: set[asyncio.Task[Any]] = set()
        self.register_handlers()
        self.agent.events.on(EventType.ALL, self._on_agent_event)

    # -- agent bridge -------------------------------------------------------

    async def _on_agent_event(self, event: Event) -> None:
        if event.type in (EventType.AGENT_START,):
            return  # carries the config object, which is not serializable
        if event.session_id is None:
            return
        await self.hub.broadcast(
            self.channel(event.session_id), event.type, **self.encode(event)
        )

    @staticmethod
    def channel(session_id: str) -> str:
        return f"session:{session_id}"

    def encode(self, event: Event) -> dict[str, Any]:
        """Project an event onto a JSON safe payload. Override to add fields."""
        return {
            k: v
            for k, v in event.data.items()
            if isinstance(v, (str, int, float, bool, list, dict, type(None)))
        }

    # -- handlers -----------------------------------------------------------

    def register_handlers(self) -> None:
        self.hub.on("hello", self.on_hello)
        self.hub.on("prompt", self.on_prompt)
        self.hub.on("command", self.on_command)
        self.hub.on("cancel", self.on_cancel)
        self.hub.on("answer", self.on_answer)
        self.hub.on("feedback", self.on_feedback)
        self.hub.on("ping", lambda conn, _msg: conn.send("pong"))

    #: Tag on the sessions this front end opens.
    CLIENT = "web"

    async def on_hello(self, connection: Connection, message: dict[str, Any]) -> None:
        """(Re)attach a client to its session: the journal, the run in flight
        with the blocks it has not journaled yet, and its open questions."""
        session = self.agent.sessions.ensure(message.get("session"), client=self.CLIENT)
        await self.agent.prepare_session(session)
        self.bind(connection, session.id)
        unfinished = self.agent.unfinished(session.id)
        await connection.send(
            "hello",
            session=session.to_dict(),
            history=self.agent.memory.replay(session.id),
            live=self.live_blocks(session.id),
            questions=self.agent.pending_questions(session.id),
            busy=self.active(session.id),
            unfinished=unfinished.run if unfinished is not None else None,
            context=await self.agent.context_usage(session.id),
            plan=self.agent.plan_view(session.id),
            warning=self.agent.rules.warning() if self.agent.config.rules_enabled else None,
            config=self.agent.config.to_dict(),
            commands=self.agent.commands.describe(),
            theme=self.theme,
        )

    async def on_prompt(self, connection: Connection, message: dict[str, Any]) -> None:
        text = str(message.get("text", "")).strip()
        if not text:
            await connection.fail("Prompt cannot be empty")
            return
        session = self.agent.sessions.ensure(connection.session_id, client=self.CLIENT)
        self.bind(connection, session.id)
        if self.active(session.id):
            await connection.fail("A run is already active")
            return
        try:
            attachments = self.store_attachments(session, message.get("attachments") or [])
        except (ValueError, OSError) as exc:
            await connection.fail(f"Attachment rejected: {exc}")
            return
        # the run publishes the prompt block itself, to every client of the session
        self.start(
            session,
            self.agent.run(self.template, session=session, input=text, attachments=attachments),
        )

    async def on_command(self, connection: Connection, message: dict[str, Any]) -> None:
        text = str(message.get("text", ""))
        result = await self.agent.commands.invoke(
            text, session_id=connection.session_id, connection=connection
        )
        if result is None:
            await connection.fail("Not a command")
            return
        if isinstance(result, dict) and isinstance(result.get("session"), dict):
            self.bind(connection, str(result["session"]["id"]))
        await connection.send("command", command=text, result=result)
        if isinstance(result, dict) and result.get("ok") and result.get("resume"):
            session = self.agent.sessions.get(connection.session_id)
            if session is not None and not self.active(session.id):
                self.start(session, self.agent.resume(session))

    async def on_cancel(self, connection: Connection, _message: dict[str, Any]) -> None:
        cancelled = await self.cancel(connection.session_id)
        await connection.send("cancelled", ok=cancelled)

    async def on_feedback(self, connection: Connection, message: dict[str, Any]) -> None:
        """A thumb on an answer. Learning calls a model, so it runs off the
        read loop; the outcome reaches every client of the session as `feedback`."""
        if not connection.session_id:
            await connection.fail("No session")
            return
        task = asyncio.create_task(
            self.agent.feedback(
                connection.session_id,
                str(message.get("vote", "")),
                target=str(message.get("id", "")) or None,
                note=str(message.get("note", "")),
            )
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def on_answer(self, connection: Connection, message: dict[str, Any]) -> None:
        """Answer a question the model asked the session of this connection.

        The outcome reaches every client of the session as `question.end`, so
        nothing is sent back here unless the answer found no question.
        """
        question = str(message.get("id", ""))
        text = str(message.get("text", "")).strip()
        # "" owns nothing: a connection without a session answers nothing
        owner = connection.session_id or ""
        if not self.agent.answer(question, text, session_id=owner):
            await connection.fail("No open question to answer")

    # -- helpers ------------------------------------------------------------

    def bind(self, connection: Connection, session_id: str) -> None:
        """Point a connection at a session channel, leaving the previous one."""
        if connection.session_id == session_id:
            self.hub.join(connection, self.channel(session_id))
            return
        if connection.session_id:
            self.hub.leave(connection, self.channel(connection.session_id))
        connection.session_id = session_id
        self.hub.join(connection, self.channel(session_id))

    def store_attachments(
        self, session: Session, attachments: list[dict[str, Any]]
    ) -> list[str]:
        """Persist client attachments inside the session workspace."""
        workspace = Workspace(session.workspace)
        saved: list[str] = []
        total = 0
        for attachment in attachments:
            raw = str(attachment.get("data", ""))
            payload = raw.split(",", 1)[1] if raw.startswith("data:") else raw
            try:
                blob = base64.b64decode(payload, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"invalid encoding for {attachment.get('name')}") from exc
            total += len(blob)
            if total > MAX_ATTACHMENT_BYTES:
                raise ValueError("attachments exceed the size limit")
            path = workspace.resolve(f"attachments/{safe_name(attachment.get('name'))}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            saved.append(workspace.relative(path))
        return saved

    def active(self, session_id: str | None) -> bool:
        """Whether a run of this session is in flight."""
        task = self.runs.get(session_id or "")
        return task is not None and not task.done()

    def start(self, session: Session, work: Awaitable[Any]) -> None:
        """Run `work` (an agent run) as the run of `session`."""
        self.runs[session.id] = asyncio.create_task(self.run(session, work))

    async def cancel(self, session_id: str | None) -> bool:
        """Cancel the run of a session, if any."""
        task = self.runs.pop(session_id or "", None)
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True

    async def run(self, session: Session, work: Awaitable[Any]) -> None:
        try:
            await work
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            await self.hub.broadcast(self.channel(session.id), "error", message=str(exc))
        finally:
            if self.runs.get(session.id) is asyncio.current_task():
                del self.runs[session.id]

    def live_blocks(self, session_id: str) -> list[dict[str, Any]]:
        """The blocks of the run in flight that the journal does not hold yet.

        A client that reconnects mid run gets the block being streamed (and a
        tool still running) whole, then the stream carries on from there; the
        steps of a worker still running follow, each with its `parent`.
        """
        live = self.agent.live.get(session_id)
        if live is None:
            return []
        result = live[0]
        journaled = {t.id for t in self.agent.memory.transcript(session_id).run_of(result.run_id)}
        names = {call.id: call.name for call in result.tools}
        return [
            {**block.to_dict(), **({"name": names[block.id]} if block.id in names else {})}
            for block in result.blocks
            if block.id not in journaled
        ] + result.steps.live()

    # -- http ---------------------------------------------------------------

    def rest(self, path: str) -> tuple[int, str] | None:
        """Read only REST endpoints. Returns ``(status, json)`` or ``None``."""
        if path == "/api/health":
            return 200, json.dumps({"ok": True, "name": self.agent.config.name})
        if path == "/api/config":
            return 200, json.dumps(self.agent.config.to_dict())
        if path == "/api/commands":
            return 200, json.dumps(self.agent.commands.describe())
        if path == "/api/theme":
            return 200, json.dumps(self.theme)
        if path == "/api/sessions":
            return 200, json.dumps([s.to_dict() for s in self.agent.sessions.list()])
        return None

    async def http(self, connection: Any, request: Any) -> Any:
        path = request.path.split("?", 1)[0]
        if path == "/ws":
            return None  # hand over to the websocket handler
        asset = self.ASSETS.get(path)
        if asset is not None:
            name, content_type = asset
            file = self.assets_root / name
            if not file.is_file():
                return connection.respond(404, "Not Found")
            response = connection.respond(200, file.read_text(encoding="utf-8"))
            self.retype(response, content_type)
            response.headers["Cache-Control"] = "no-cache"
            return response
        rest = self.rest(path)
        if rest is not None:
            status, body = rest
            response = connection.respond(status, body)
            self.retype(response, "application/json")
            return response
        return connection.respond(404, "Not Found")

    @staticmethod
    def retype(response: Any, content_type: str) -> Any:
        """Set the content type of a response, replacing the one it was built with.

        The header map appends rather than overwrites, so assigning on top of
        the ``text/plain`` the transport picked would send two content types and
        the browser would believe the first — which is how a stylesheet ends up
        ignored.
        """
        with contextlib.suppress(KeyError):
            del response.headers["Content-Type"]
        response.headers["Content-Type"] = content_type
        return response

    def greet(self) -> None:
        """Print the banner the server opens with."""
        renderer = self.agent.renderer
        if renderer is None:
            return
        renderer.banner(
            f"{self.agent.config.name} (web)",
            [
                ("URL", f"http://{self.host}:{self.port}"),
                ("Model", self.agent.config.inference_llm),
                ("Vision Model", self.agent.config.inference_vlm or "(leader)"),
                ("Theme", str(self.agent.config.theme or "(default)")),
            ],
        )
        renderer.banners = False  # one banner per server, not one per message

    async def serve(self) -> None:
        """Serve until cancelled."""
        from websockets.asyncio.server import serve as ws_serve

        self.greet()
        async with ws_serve(
            self.hub.serve_connection,
            self.host,
            self.port,
            process_request=self.http,
            compression=None,
            max_size=MAX_MESSAGE_BYTES,
        ):
            self.agent.sessions.start_sweeper()
            try:
                await asyncio.Future()
            finally:
                await self.agent.sessions.stop_sweeper()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

PASSTHROUGH_TEMPLATE = "{{ config.input }}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the supported arguments: ``--input``, ``--repl``, ``--serve``, ``--config``."""
    parser = argparse.ArgumentParser(prog=STEM, description=__doc__.splitlines()[0])
    parser.add_argument("--input", default="", help="a prompt file path or raw text")
    parser.add_argument(
        "--repl",
        action="store_true",
        help="chat in the terminal (the default with no arguments)",
    )
    parser.add_argument(
        "--serve", action="store_true", help="serve the web UI and websocket hub"
    )
    parser.add_argument(
        "--config",
        default="",
        help=f"a JSON or YAML config file (default: {STEM}.json|.yaml here or beside {STEM}.py)",
    )
    return parser.parse_args(argv)


def mode_of(args: argparse.Namespace) -> str | None:
    """The front end parsed arguments select: ``web``, ``repl`` or ``None``
    for a one shot run, in the order `Agent.execute` dispatches them."""
    if args.serve:
        return "web"
    if args.repl or not args.input:
        return "repl"
    return None


def template_candidates(directories: Iterable[Path | str] | None = None) -> list[Path]:
    """Every template file that is looked at, in order of precedence."""
    return [
        directory / f"{STEM}_prompt.md" for directory in config_directories(directories)
    ]


def load_template(config: AgentConfig | None = None) -> str:
    """Resolve the template for the data driven application.

    Looks at `config.template` — which a config file or ``AGENTIC_TEMPLATE`` sets,
    as a path or as the template itself — then ``agentic_prompt.md`` in the current
    directory and next to ``agentic.py``, and finally falls back to passing the
    input through.
    """
    raw = (getattr(config, "template", "") or "").strip()
    raw = raw or os.environ.get(f"{ENV_PREFIX}TEMPLATE", "").strip()
    if raw:
        path = Path(raw).expanduser()
        with contextlib.suppress(OSError, ValueError):
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return raw
    for default in template_candidates():
        if default.is_file():
            return default.read_text(encoding="utf-8")
    return PASSTHROUGH_TEMPLATE


def main(argv: list[str] | None = None) -> int:
    """Entry point of the data driven application."""
    return Agent.cli(None, argv)


if __name__ == "__main__":
    raise SystemExit(main())
