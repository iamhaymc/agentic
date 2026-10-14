# ⏬ Agentic — Changes

The record of what has been built and why it is built that way: the constraints
the harness was designed against, the reasoning behind each subsystem, and the
defects that were worth understanding rather than merely patching. It is written
for whoever picks the library up next, so an entry is kept while it explains a
decision that is still load-bearing and dropped when the code has stopped
depending on it.

Three other files carry the rest. [`README.md`](README.md) is the short version —
what the harness is and how to install, run and test it. [`DESIGN.md`](DESIGN.md)
is the walkthrough of the implementation, subsystem by subsystem, with the
configuration, protocol and extension reference. [`TODO.md`](TODO.md) is what is
left, in one flat list.

There are no releases yet, so this is grouped by subsystem rather than by
version, with the phase log and the fixes at the end.

---

## Constraints and design intent

### Goals

- **Application agnostic.** The harness knows nothing about any product built on
  it. A consumer owns a prompt template and, at most, a subclass; everything
  else is configuration.
- **One class is the API.** `Agent` resolves its own configuration, renders the
  template, runs the agentic loop and streams the result. `await Agent().run(t)`
  is a complete program.
- **The loop is usable alone.** The batteries are additive, not structural:
  `Engine` is the inference and tool call loop with no storage, sessions,
  memory, repository, console or web, and `Agent` is that class with those
  bolted on. An application that already has its own infrastructure embeds
  `Engine` and pays for nothing else.
- **Everything is replaceable.** Every subsystem is either an injectable
  collaborator (`store=`, `cache=`, `tools=`, …), an overridable method
  (`build_model`, `build_prompt`, `stream`, …) or a registry entry (tools,
  commands, event subscribers). No behaviour is reachable only by patching.
- **A run leaves no trace.** Work happens inside a session that owns a temporary
  directory; closing or expiring the session removes the directory, its database
  record and its cache entry.
- **One implementation, every front end.** The same `Agent` drives a one shot
  console run, the terminal repl and the web UI, because printing and streaming
  are event subscribers rather than code paths inside the loop, and because the
  repl and the web server are both clients of the same command registry.
- **Secrets never leak.** Credentials are masked in every serialization, banner
  and command output, and are never written to disk.

### Non-goals

- A framework. There is no plugin discovery, no dependency injection container
  and no configuration DSL: a dataclass, an event bus and two registries.
- A hosted product. There is no user model and no multi-tenancy; conversations
  are remembered per session, not per person, and the harness is a library
  first.
- A model abstraction layer. The `openai-agents` SDK is the loop; the harness
  supplies configuration, tools, sessions and transport around it.

### Constraints that shaped the code

- **Chat Completions backends only.** The hosted tools of `openai-agents`
  (`ApplyPatchTool`, `ShellTool`, …) require the OpenAI Responses API.
  OpenRouter and most other providers speak Chat Completions, which supports
  plain function tools only — so the file, shell, image and patch tools are
  implemented here rather than taken from the SDK.
- **Async end to end.** The web layer, the SDK stream and the tools all live on
  one event loop, so every blocking primitive (file IO, `git`, subprocesses)
  either runs in a thread or uses an async subprocess.
- **A single flat module.** `agentic.py` is one file with commented subsystem
  sections. It keeps the import graph trivial, the library copyable into another
  tree and the whole surface readable in one pass.

---

## Configuration

`AgentConfig` is a dataclass resolved in layers: dataclass defaults, then a JSON
or YAML file, then `AGENTIC_*` environment variables, then whatever the consumer or
the CLI passes in. Layering in that order is what lets the same object serve a
library caller, a shell and a deployment without any of them knowing about the
others.

Values are coerced from the environment against the field annotations, so
`AGENTIC_PORT` becomes an `int` and `AGENTIC_THEME` a `Path` without a schema.
Unknown keys are not errors: they land in `extras` and are reachable from a
template as `config.<name>`, which is how an application adds its own values
without subclassing the config.

`input` is deliberately overloaded. A caller passes a file path or raw text and
the harness decides: short single-line values that name an existing file are
read, everything else is used as is. That is the whole reason the CLI needs only
`--input`.

Secrets are recognized by field name (`*_api_key`, `*_token`, `*secret*`, …) and
masked by `to_dict()` and by the console banner, so a config can be logged,
served over the REST API or printed without redacting it by hand at each site.
An empty `api_key` is a valid state, not a missing one: endpoints that need no
auth are configured by leaving the key unset.

### Config files

The environment is a poor place to keep two dozen settings: it is flat, it is
untyped, and a deployment ends up with a wrapper script whose only job is to
export things. A file layer fixes that without becoming a framework.

- **Named after the module.** `agentic.json`, `agentic.yaml` or `settings.yml` — the
  stem of `agentic.py`. A consumer that renames the module renames its config with
  it, and nothing needs to be configured to find the configuration. The same
  stem names the environment prefix (`AGENTIC_*`), the prompt file
  (`agentic_prompt.md`), the CLI program and the page assets
  (`agentic_ui.*`), so renaming the module is a file rename and nothing else.
  The module was renamed from `agent` to `agentic` to match the package and
  its script; `AGENT_*` variables in an existing `.env` must be renamed.
- **The current directory first, the module directory as a fallback.** An
  installed harness ships whatever defaults it likes beside itself; the
  application that runs it overrides them from where it is run. The first file
  that exists wins, because merging two files invites the question of which one
  set what.
- **Below the environment.** A file is checked in, the environment is not, so
  `AGENTIC_API_KEY` must still beat a value in the file — and a credential that
  belongs nowhere near a repository can be pulled in explicitly with
  `${oc.env:AGENTIC_API_KEY}` instead.
- **omegaconf, not a schema.** It reads both formats and resolves `${...}`
  interpolations to the environment and to other keys, which is what makes one
  file serve several environments. It stays optional at runtime: JSON is read by
  the standard library and YAML falls back to `pyyaml`, so a stripped install
  still starts.
- **Nested groups, flattened carefully.** `vision: {model: v}` becomes
  `vision_model` only when every key of the group names a real field. Anything
  else is left alone and lands in `extras`, so an application's own nested values
  survive intact and a typo never silently invents a setting.
- **Off is a value.** `config_file=False` keeps an embedded engine from reading a
  stray `agentic.yaml` in the working directory of its host, in the same spirit as
  `env=False`; a named file that does not exist raises, because the caller asked
  for that file by name.

### Env files

Credentials belong in neither the config file nor the repository, and exporting
them by hand before every run is the wrapper script the file layer set out to
remove. A `.env` file closes that gap.

- **Loaded into the environment, not into the config.** `.env` feeds
  `os.environ` before the config file is read, so it serves both
  `${oc.env:VAR}` interpolations (`INFERENCE_URL`, ...) and the `AGENTIC_*` layer
  without a second resolution path.
- **Fills gaps, never overrides.** A variable already set in the shell or the
  deployment wins, so CI and production need not care whether a stray `.env`
  is lying around.
- **Same search path, same switch.** `.env` is looked for where the config file
  is (cwd, then the module directory), and `env_file=` takes the same `True` /
  `False` / path values as `config_file=`. It is part of the environment layer,
  so `env=False` skips it as well and an embedded host's process environment is
  left untouched.
- **python-dotenv, imported lazily.** The parser handles quoting, `export` and
  `${VAR}` expansion; a vendored `agentic.py` without it only fails when a `.env`
  is actually there to read.

## Events

Every observable side effect of a run — printing, streaming to the browser,
logging — is a subscriber on `EventBus` rather than a branch inside the loop.
This is what makes the console renderer deletable (`console=False`), the web
server a pure bridge and telemetry a three-line addition.

A failing subscriber is caught and reported, never propagated: a broken renderer
must not be able to kill a run. Handlers may be sync or async, so the bus is
usable from a thread-shaped consumer without wrapping.

Tool calls are part of that stream rather than a hint that something happened.
`tool.start` once carried nothing but `kind`, so a console or a browser could
show that the model had paused without ever showing what it ran; the loop now
publishes the tool name, the arguments, the outcome, the result and the duration,
and keeps the same records on `RunResult.tools` for a caller that audits a run
afterwards. The arguments come from the run item events of the SDK instead of the
raw argument deltas, because that is where a complete, already assembled call and
its matching output are available. Everything published is passed through
`redact()` first: a run is worth watching only if watching it cannot leak the
credentials the model was handed, and a transcript is worth keeping only if one
tool that returns a megabyte cannot flood it.

A silent model is an event too. Ollama sends a tool call whole once the model has
finished writing it, so a long `write_text_file` was minutes in which the page
showed nothing at all and a reader could not tell a model at work from a dead
one. `model.wait` is published every `stream_heartbeat` seconds of that silence
with the seconds waited and the stall budget. It is published from
`next_event()` rather than from a timer of its own because that is the one place
that already knows when the stream last spoke, it is suppressed while a tool call
is open because that silence belongs to the tool and the tool reports itself
(`tool.progress`), and it observes the stall guard without feeding it: a
heartbeat that restarted the budget would make the guard unable to fire.

The model can also ask. `ask_user` puts a question with optional choices to the
person and waits; the engine only brokers it (`question.start`, `answer()`,
`question.end`) and a front end shows and answers it, which keeps the core free
of any terminal or browser. Whether the tool exists is a mode decision
(`ask_enabled`, settled like `outside_read`): the repl and the web server have
someone at the other end, an embedded engine and a one shot run do not, and a
question nobody can answer would hold the run forever. Waiting on
a person is not a stall, so an asking call reports itself alive (`ToolCall.asking`)
and the run waits as long as the person does; cancelling the run is what ends an
unanswered question, and that is published as `question.end` with `ok: false` so
every client closes the card.

## Prompt

The consumer owns the template; the harness only renders it. Jinja runs
sandboxed with `StrictUndefined`, so a typo in a template fails loudly instead of
silently emitting an empty prompt, and a template cannot reach into the host
process. `config` is the single object exposed to the template, which keeps the
contract between application and harness to one name.

## Storage and cache

`Store` and `Cache` are protocols with a SQLite and an in-memory LRU
implementation. The split exists because the two have different lifetimes: the
store is durable and namespaced (sessions today, transcripts tomorrow), the
cache is a bounded, optionally expiring accelerator. Both are constructor
arguments, so Redis or Postgres is a substitution and not a fork.

## Sessions

A session owns a temporary directory and every tool is scoped to it. Isolation
is the point: concurrent runs cannot see each other's files, and a crash cannot
leave content behind on the host. Expiry is checked on access and on every run,
so an abandoned browser tab cannot pin a workspace forever; `keep_workspace` is
the escape hatch for debugging.

`workspace_seed` copies an application content directory into each new workspace,
which is how a consumer ships data to the model without teaching the harness
about it.

Sessions used to live only in a process dictionary, so a restart orphaned every
workspace and forgot every id while the store still held the record. Three
changes closed that gap:

- **The store is the source of truth.** A manager rehydrates the live sessions
  from the store when it is built, and a record that cannot be read, has expired
  or has lost its directory is reconciled away rather than adopted — a restart
  either returns the same session or none at all, never a broken one.
- **Nothing is left behind.** The directories under the workspace root that no
  live session owns are reaped, restricted to a configured root and to the
  manager's own prefix so a shared temporary directory can never be swept up by
  it.
- **Expiry on a timer, a lease on access.** `session_sweep_interval` runs
  expiry and reaping in the background instead of waiting for someone to ask for
  a session, and access renews the lease so a busy session is not expired under
  a client while an abandoned one still runs out.

Durability is a mode decision (`session_durable`, tri-state like the tool
flags): the repl and the web server are durable, because a person comes back to
them; a one shot run and an embedded engine are not, because the default
promise of a library is that a crash leaves no content on the host. Durability
was useless while the default store was an in-memory SQLite database, so
`state_dir` (``.agentic`` in the current directory for both interactive modes)
now gives the database and the workspace root a home, and the idle lease of an
interactive session is a day rather than an hour. The mode has to be known
before the store opens, which is why it is `Agent(mode=)` — passed by `cli()` —
rather than something `Repl` or `WebServer` can settle after the fact.

Two leaks closed with it: a transcript outlived the session it belonged to (an
expired session left its journal in the store forever), and a run longer than
the lease could have its workspace swept from under it. `SessionManager.listeners`
now hears every destruction, so the journal goes and a run still in the session
is cancelled; a run renews its lease at every checkpoint; and a restart forgets
journals whose session did not come back.

## Memory

A run used to be one shot: the prompt was rendered, the loop ran, the blocks were
streamed and nothing survived. That was defensible for the CLI and a lie in the
chat UI, where a second message started from zero. A session now owns a
transcript.

Three decisions shaped it:

- **The store, not the process.** Transcripts are persisted through the same
  namespaced `Store` as sessions, so they outlive a reconnect and can outlive a
  restart; the in-process dictionary is only a read-through cache.
- **What is remembered is the exchange, not the prompt.** Replaying the rendered
  template on every turn would repeat the consumer's instructions verbatim and
  spend the context window on them, so the default records the input the user
  wrote and the output of the model. `Agent.remember()` is the hook for a
  consumer that wants another rule.
- **Keep everything, show a window.** The journal is never trimmed; what the
  model sees is decided by [context compaction](#context-compaction). The turn
  and character budgets that used to trim the transcript (and the opt-in
  `memory_summary`) are gone: they lost the history a person scrolls back
  through, counted characters where providers bill tokens, and only ran
  between runs, so one long run could still overflow.

The web layer replays the transcript in the `hello` reply, which is what makes a
reload or a reconnect restore the discussion instead of showing an empty page
attached to a model that remembers. `/forget` clears a transcript, and closing a
session forgets it along with its workspace.

### The journal

The transcript used to be written once, after a run succeeded: a crash, a
failure or a stop lost the prompt and every step of the run, and the next run
saw only words, never the tool calls behind them. Recovery needs both the
message history and the context window back, so the transcript became a
write-ahead journal:

- **Each step lands as it finishes.** The prompt is journaled (marked
  `running`) before the model is called; the stream hands every assistant
  message and every whole tool call to `Engine.checkpoint()`, which `Agent`
  persists at once. A crash costs at most the step in flight.
- **Raw items, not prose.** A step keeps the model input items the SDK reports
  (`to_input_item()`): an assistant message as a `ResponseOutputMessage`, a tool
  call as its `function_call` plus `function_call_output`. Plain
  `{"role": "assistant"}` messages would not do — the Chat Completions
  converter merges a tool call into a preceding output message but not into a
  plain one — and a call without its output is never journaled, since a
  request without its answer is input the next model call would reject. Tool
  output is clipped to `ConversationMemory.TOOL_LIMIT` for the journal, the run
  itself having seen it whole; `memory_tools: false` keeps tool calls out of the
  replay while still journaling them for display.
- **Runs carry a status.** `settle()` records `ok`, `failed` or `cancelled` on
  the prompt; one still `running` with nothing live behind it was interrupted.
  `unfinished()` finds such a run and `/resume` continues it: a passthrough
  prompt asking the model to carry on, over a context that already holds what
  was done. Failed and stopped runs keep their steps too, which reverses the old
  rule that a failed run left nothing — losing finished work was the larger harm.
- **Ids that survive.** A journaled turn keeps the id of the block it came from
  and the id of its run, so a client that reconnects replays the same blocks it
  showed, and a vote can name the answer it is about.

`RunResult.turns` exposes the same steps to an embedded `Engine`, so an
application with its own history appends `items` after its prompt and gets the
same context window back.

### Context compaction

The goal is a chat without end, with a model that thinks at length and calls
tools in any pattern. The constraints that shaped it:

- **The SDK's compaction does not reach this harness.** `openai-agents` ships
  `OpenAIResponsesCompactionSession` and `ModelSettings.context_management`,
  but both need the OpenAI Responses API and `gpt-*` / `o*` models; the harness
  speaks Chat Completions to OpenRouter, Ollama and the rest. What the SDK does
  offer everywhere is `RunConfig.call_model_input_filter`, called before every
  model call with the whole input, so compaction is the harness's own, plugged
  in there (`ContextRun`).
- **Inside a run, not only between runs.** A thinking model in a long tool loop
  fills the window within one run; a filter on every call compacts it as it
  goes, and the same `Compactor` works over the journal between runs.
- **Tokens, calibrated.** No local tokenizer is the provider's, and the vocabulary
  of tiktoken is a download that may be blocked, so the count is an estimate
  (tiktoken, else bytes / 4) scaled by what the provider reported for recent
  calls (`TokenCounter.ratio`, from `response.completed`; usage is asked for on
  every stream). The estimate only has to be proportional.
- **Cheapest first.** Old reasoning, then old tool outputs (observation
  masking: cleared outputs are re-fetchable, and masking holds up against
  summarising at a fraction of the cost), and only then a summary, which costs
  a model call; a giant newest output is clipped last. Each tier stops at the
  target.
- **Rare and cache friendly.** Compaction starts past a high watermark and goes
  down to a low one, so it happens once per `high − low` tokens of growth, and
  its decisions are sticky: every later call applies them first, the input
  changes only at a compaction, and the prefix a provider caches holds between
  them. Cleared outputs are marked in the journal, so the next run's prefix is
  the one the model last saw.
- **Anchored, incremental summaries.** The summarizer folds a segment into the
  previous summary under fixed sections (goal, user messages, decisions, files,
  errors, progress, open work) instead of rebuilding it; a segment larger than
  the summary model is folded a chunk at a time; the request in flight is
  pinned verbatim; a failing summarizer leaves an extract rather than a hole.
- **A cut never breaks a pair.** Calls stay with their outputs, reasoning with
  what it led to, parallel calls together (`safe_cuts`), so any provider
  accepts what is left.
- **The journal stays O(1) a write.** A transcript that is never trimmed would
  make every step rewrite the whole conversation, so turns became one record
  each; the old inline format is rewritten on load.
- **Nobody waits when nobody has to.** After a run the journal is compacted in
  the background a little before the trigger; the next run awaits it. When the
  provider still says the input is too long, the guard reads the window from
  the error, teaches it to the pool, compacts hard and retries.
- **Visible.** The mark's core fills with the context in use, the page and the
  repl say when a compaction happened and what it saved, its summary is one
  click away, and `/compact [focus]` does it on demand.

### Work slicing

Compaction reacts once the context is full; slicing plans ahead so it rarely
has to. The incident behind it: a 27B local model read a long document a page
at a time until its context held ~60k tokens, then sat silent for an hour, six
attempts each failed by a 10 minute stall guard. The window was not the limit,
the read speed was: ~100 tokens/s, so a cold read of the context (after a cache
miss) took longer than the guard allowed. The constraints that shaped it:

- **Time is a budget, like tokens.** The window says what fits; the read speed
  says what fits *in time*. Each call is timed from the input handed over to the
  first chunk, over the tokens the provider actually had to read (all of them
  after a compaction, else the growth since the call before), and once a model
  is measured its budget is capped to what it reads from scratch in half the
  stall guard (`slice_stall`). Hosted models read thousands of tokens a second
  and never feel the cap; a local one compacts before it can stall.
- **Measured, never guessed.** A model asked how much it will read guesses
  badly, and a small one worse. The `plan` tool takes paths, globs and line
  ranges and the harness counts them, the same counter the context uses.
- **Enforced, with a way out.** A step over one slice is refused with its sizes,
  which tells the model how to split it; the same steps sent back are kept with
  a warning, since a single line cannot be split and a refusal loop is worse
  than a large step.
- **Recited, not remembered.** The open plan and the budget ride on a short note
  at the end of every call, after compaction and outside the journal, so no
  summary can lose them and the model always sees where it stands (the
  todo-list recitation long running agents use against drifting goals). At the
  end of the input it costs the provider's cache only the newest response.
- **Off with one switch.** `plan_enabled: false` restores the input byte for
  byte, which is what the compaction tests run under.

## Learned rules

Feedback on an answer used to go nowhere. A thumb (or `/good`, `/bad` in the
repl or the composer) now teaches the agent a rule it keeps:

- **Distilled, not recorded.** The trace of the run behind the answer — prompt,
  tool calls, the answer, cut to `RuleBook.TRACE_LIMIT` — goes to the `summary`
  role (falling back to the leader) with the vote, an optional note and every
  existing rule. The model answers with JSON: add a rule, or edit the one that
  covers the same behaviour, which keeps the book from filling with near
  duplicates. A reply that is not JSON is taken as the rule itself.
- **A standing system message.** `Agent.decorate_instructions()` appends the
  rules (`Do: …` / `Avoid: …`) to the instructions of every run.
- **Serialized and claimed.** Learning holds a lock, so two votes in a row
  cannot both add what one should have edited, and an answer is claimed before
  the model call, so a double click learns once; a failed distillation releases
  the claim and costs the rule, never the session.
- **A budget that warns.** Every run pays for the rules, so past
  `rules_max_chars` a warning is shown — after the vote, on `hello` and when the
  repl starts — rather than anything being dropped silently; `/rules` lists,
  `/rules drop <id>` and `/rules clear` prune.

Rules live in the store (namespace `rules`), so they persist wherever the store
does; with the interactive modes durable by default, that is across restarts.

## Resilience

Two failures used to end a run with nothing useful to say: a provider that stops
answering, which hung the caller until it gave up, and a provider that answers
with a refusal, which arrived as a string a consumer had to parse. Both are now
bounded and named.

Naming came first, because retrying is a decision and a decision needs a
category. `ErrorKind` is that category and `classify_error()` assigns it by
exception class name, then by HTTP status, then by whether it is an `OSError` —
in that order, and by *name* rather than by class, so the harness classifies an
`openai` error, an `httpx` error or a provider SDK nobody has written yet
without importing any of them. `ErrorKind.TRANSIENT` is the subset another
identical attempt may get past, which is what `RetryPolicy` consults; everything
else is a decision of the provider and is reported at once. An unrecognized
failure classifies as `internal` and is re-raised unchanged rather than wrapped
in a `ModelError`, because a defect in the harness dressed up as a provider
fault is worse than no classification at all.

Backoff is exponential with jitter, and the jitter is the point: clients that a
rate limit failed together will otherwise come back together and reproduce it.
Retrying is bounded by attempts rather than by a deadline so that a caller can
reason about the worst case from the configuration alone.

The leader and the specialists are shaped differently, so they are bounded
differently. A specialist completion is one request and a timeout around the
whole call is right. The leader's stream is a long lived read whose total
duration is legitimately unbounded — that is what an agentic loop is — so the
leader has a stall guard on the wait for the *next* event instead, with a
budget of its own (`stream_timeout`) because a silent stream is not always a dead
one: a provider may hold a tool call back until the model has finished writing
it.
That guard has to know about tools: the SDK runs a tool call inside the stream,
so the budget is extended by `shell_timeout` while a call is outstanding, or a
slow shell command would be indistinguishable from a dead provider. A fixed
extension is still a guess about how long a tool may take, so the budget runs
from the latest sign of life instead: a running command heartbeats
(`tool.progress`, every `shell_heartbeat` seconds) and each beat restarts the
budget, which leaves `shell_timeout` as the one bound on a live command and the
stall guard as the bound on a silent one. And a stream
is only restarted while it has produced nothing: once a block or a tool call
exists, a retry would replay work the consumer has already seen and re-run tools
that already had effects, so the failure is reported instead. That rule, not the
error kind, is what makes retrying the leader safe.

Retrying in two places at once is worse than retrying in neither: the `openai`
client retries twice on its own by default, so the first end to end test of this
work showed a 503 disappearing with nothing on the bus and nothing in the
accounting. The client is now built with `max_retries=0`. A retry that
the harness cannot see cannot be reported, jittered by the policy that is
supposed to own the decision, or counted against the run.

Accounting was folded into the same change because tokens are only knowable
where the calls are made, and a run makes them in several places: the leader's
stream, a tool delegating to the vision model, the summariser compressing what
fell out of memory. Threading a sink through all of them would have put an
accounting parameter on half the extension points, so the run publishes its
`Usage` in a context variable for the length of `running()` and every call
underneath finds it there — and two concurrent runs never collide, because each
run is its own task. A failed attempt is accounted too: the tokens were spent
whether or not the answer arrived. Pricing is one rate pair per deployment
(`cost_input`, `cost_output`, per million tokens) rather than a table per model,
which covers the common case and leaves the rest to an override of `price`.

## Models

A run has one **leader** model that drives the agentic loop and, optionally,
specialists that tools delegate to. The first specialist is **vision**, added
because the strongest text leaders are frequently blind: with `vision_model`
unset the leader is assumed multimodal and gets `view_image`, which returns a
base64 data URL; with it set the leader gets `describe_image` instead and the
image never enters its context — the specialist answers in a single completion,
without tools and without a loop, and only its text comes back.

`ModelSpec` is the resolved identity of a role and `ModelPool` keeps one client
per endpoint and one SDK model per name, shared across roles, sessions and runs,
so an additional role costs no additional connection. Specialist endpoints and
credentials fall back to the leader's, which makes a second model on the same
provider a single setting. Adding a role is therefore three steps: add the
fields, resolve with `config.model_spec(role)`, call the pool.

The API key is optional. Endpoints that need no auth — a local Ollama, a
forwarding proxy — are configured with an empty key, and the pool hands the SDK
a placeholder rather than letting it refuse to construct a client over a
credential it would never send. A set `OPENAI_API_KEY` still wins, so the SDK's
own environment fallback keeps working.

`inference_ctx` (`AGENTIC_INFERENCE_CTX`) is sent with every leader and
specialist request as `extra_body={"options": {"num_ctx": n}}`, Ollama's name for
the context size, because a Modelfile can pin a small one (e.g. `num_ctx 8192`
on a model trained for `262144`). Unset, the pool looks up the trained length
once per model (`/api/show` `model_info.<arch>.context_length`) and sends
nothing when there is none (any non-Ollama provider). Caveat: Ollama 0.34's
OpenAI-compatible `/v1` drops `options`, so the value takes effect only on a
server that reads it; the harness window (`context_window`) is not derived from
it, since a server that ignores it still truncates at its own `num_ctx`.

## Tools

`Workspace` holds the sandboxed primitives and `ToolRegistry` exposes them to the
model. Keeping them apart means the security-critical part — path resolution,
size limits, shell timeouts — is plain synchronous code that can be unit tested
without an SDK, a model or a network.

Paths are resolved through `Path.resolve()` and rejected unless they stay inside
the root, which also settles symlinks: a link pointing outside the workspace
resolves outside it and is refused. Reads are capped, empty files are rejected
before they become malformed data URLs, and shell commands run with an explicit
timeout and are killed when they exceed it, because an unbounded subprocess is
an unbounded hang of the whole event loop.

`outside_read` and `outside_write` relax the boundary, for reads and for writes.
A terminal user is on their own machine and expects the agent to reach files
they point it at, so the repl allows both by default; a web client is whoever
reaches the port, so the web server allows neither, and nor does a one shot run
or an embedded engine. The defaults are a mode decision rather than dataclass
defaults, hence `None` plus `AgentConfig.with_defaults()`, which settles only
what the config layers left unset. (`read_outside` was renamed into the pair;
there are no releases to stay compatible with.)

The shell was never confined, so the old flag guarded the door while the shell
stood open. It is now held to the same pair by an OS sandbox, picked by
`shell_sandbox`:

- **Linux: Landlock.** A launcher (`python -I -S -c`, stdlib `ctypes`) applies a
  ruleset to itself and execs `/bin/sh`, so the restriction is inherited by
  everything the command starts: full access to the workspace, read and execute
  to the system directories, the interpreter and the `PATH` entries (or
  everywhere with `outside_read`), and writes outside only with
  `outside_write`. Any failure exits 126 before the command runs.
- **Linux without Landlock: bubblewrap**, when installed and allowed to make a
  namespace; coarser, since a writable root is readable.
- **macOS: Seatbelt**, a generated `sandbox-exec` profile denying on top of the
  default policy.
- **Elsewhere (Windows): none**, and the shell is withheld rather than run
  unconfined — the boundary the flags promise is either enforced or not offered.

What a confined command cannot write it cannot use, so its temp directory (and,
when reads are confined, its home) points into `.sandbox/` in the workspace;
otherwise `git` dies on an unreadable `~/.gitconfig`. The shell's description
tells the model the boundary. `shell_enabled: false` remains the off switch.

Every tool is wrapped by one shared guard that turns workspace and repository
errors into text the model can read and act on. A tool that raises ends the run;
a tool that returns `Error: …` lets the model correct itself. The guard preserves
the wrapped signature, since the SDK derives the tool schema from it.

## Repository

A session may be backed by a git checkout, which turns the harness from a text
generator into something that can finish a piece of work. Setting `repo_url` is
enough: on first use the repository is cloned into the session workspace and a
branch of its own (`<prefix>/<session id>`) is checked out from the base branch.

The clone is an ordinary directory inside the workspace, so every existing file
and shell tool already works on it and it is wiped with the session — no second
sandbox, no second path model. `RepoSpec` normalizes `owner/name`, HTTPS, SSH and
filesystem URLs onto one identity so the same configuration works against
github.com, an Enterprise host and a local fixture (which is what makes the git
tests hermetic).

Credential handling is the delicate part and is deliberately narrow: the token is
embedded only in the argument list of the git invocations that need it, the
stored remote is rewritten to the clean URL immediately after cloning, and every
command output and API error is masked before it is surfaced. Tokens shorter
than eight characters are not masked, because replacing a one-character string
would shred unrelated output without protecting anything.

A clone failure is reported and the run continues without a checkout: a
repository is an enhancement of a session, never a precondition for it.

The five git tools (`git_status`, `git_commit`, `git_push`, `open_pull_request`,
`publish_work`) exist as one set with a matching set of slash commands so that a
human and a model drive the same operations through the same code.

## Core

The core was one class until an embedding consumer needed the loop without the
harness: `Agent.__init__` opened a SQLite database and a session manager (and so
a temporary directory) before it could run anything, which is exactly what an
application with its own sessions and history does not want. Splitting it was
cheaper than making each battery optional — `Engine` holds the pieces the loop
genuinely needs (config, events, prompts, tools, models) and `Agent` adds the
rest, so neither class carries a flag for the other's behaviour.

Two extractions made the split clean. `running()` is an async context manager
around the body of a run, so the `agent.start` / `agent.error` / `agent.end`
lifecycle and the "report failures on the result, re-raise cancellation" rule
are written once and shared by both `run()` methods. `turn()` is one pass of the
loop (workspace, tools, SDK agent, stream), so `Agent.run()` differs from
`Engine.run()` only where it should: acquiring a session, rendering a template
and replaying what the session remembers. `stream()` lost its `Session` argument
in the process — it now takes the `RunResult`, whose `session_id` is only a
correlation id, which is what let the loop stop knowing about sessions at all.

## Console

The default renderer is only a subscriber. It prints a banner from
`config.banner_items()`, streams blocks with per-kind styling, and disables
colour when the stream is not a TTY. Consumers subclass it, replace it or drop it
and subscribe their own; nothing in the loop knows it exists.

Streamed text is written once per 50 ms frame rather than once per token. Ctrl-C
in the repl inside a VS Code task kept the answer streaming for seconds before
`(cancelled)` appeared, while the harness itself stopped within 2 ms in every
setup that could be measured (plain console, ConPTY, `uv run`, a real `input()`
reader, the real SDK stream). Windows 10's ConPTY forwards each write as its own
terminal update — 202 tokens/s became 202 redraws/s — so the editor's terminal
most likely fell behind and only saw the keypress once it caught up (VS Code
itself could not be driven to confirm it). Framing cuts that to
about 20 updates/s at the same text throughput; a non-TTY stream still writes at
once, which keeps the tests synchronous.

## Commands

Slash commands power everything the discussion itself does not: session
lifecycle, model and vision switching, and every git operation. One registry
serves the repl and the browser alike, so a command written once is available in
both, and the client discovers the list at handshake time instead of hard coding
it.

## Repl

Running the module with no arguments used to be an error message. It now opens a
terminal, because the shortest path from an installed harness to a conversation
should not be a flag, a browser or a Python file — and because a one shot
`--input` run cannot show the thing the harness is actually built around, a
session that remembers what was said.

`Repl` takes an agent instance and nothing else, which keeps it a client rather
than a mode: it dispatches slash commands to the agent's registry exactly as the
web client does, hands everything else to `agent.run()` and lets the existing
`ConsoleRenderer` do the printing. A consumer opens one on its own subclass with
`await Repl(my_agent).start()`, and `Agent.repl()` is the seam for swapping the
class.

Two details are worth keeping:

- **The terminal must not own the loop.** Lines are read on one dedicated thread
  that is asked for a line at a time, so timers, the session sweeper and a
  streaming run keep running while the prompt waits. One thread, not one per
  line, so a Ctrl-C at the prompt cannot leave two readers racing for the next
  line — and a daemon thread, so a blocked `input()` cannot hold up exit.
- **Ctrl-C cancels the run, not the process.** The interrupt is handled on the
  loop where the platform has a loop handler and through `signal` otherwise; at
  an idle prompt it prints a hint rather than killing a session the user is in
  the middle of. Leaving is `/exit`, `/quit` or Ctrl-D.

The banner is printed once instead of once per run (`ConsoleRenderer.banners`),
which is the only change the repl needed in the console renderer.

A question of the model is read on the same reader thread as every other line,
at an `answer>` prompt, with the options numbered so a choice is one keystroke.
A second reader would race the first for the terminal, so the thread stays
single and the rule stays "one line per request, in order"; a question that a
Ctrl-C closes gives its pending line to the next prompt instead of swallowing
it. Questions are answered one at a time, because two prompts printed together
cannot be told apart at a terminal.

## Web layer

REST is read-only state (`/api/health`, `/api/config`, `/api/commands`,
`/api/sessions`, `/api/theme`); everything that changes state travels over the
websocket. One ordered channel per client removes the interleaving problem
between a POST and a stream, and makes cancellation a message rather than a
second endpoint with its own auth story.

Connections subscribe to the channel of their session, and the server is a pure
bridge: it subscribes to the bus, filters events to JSON-safe values and
broadcasts them. `agent.start` is not forwarded, since it carries the resolved
config object.

Attachments are uploaded as data URLs on the prompt message, size-capped, stored
under `attachments/` in the session workspace with sanitized names, and appended
to the prompt as a file listing — so an attachment is just a workspace file the
model can open with the tools it already has.

A reload used to start a new session, so the conversation was gone even though
the server still held it. The page now keeps the id of its session in
`localStorage` and says `hello` with it; the server needed no change, since
`sessions.ensure()` already returns a live session and opens a new one for an
expired or unknown id. The decision of whether a session is still active stays
with the one party that knows, and the client only notes when it was not.

An answer travels as its own message, scoped to the session of the connection
that sends it, because a question id is visible to every client on the channel
and must not be answerable from another session. A connection that never said
`hello` has no session and answers nothing.

A run used to belong to the socket that started it, so a phone that slept or a
network that blinked cancelled the work. A run now belongs to its session
(`WebServer.runs`): a dropped socket leaves it running, Stop cancels it from any
client of the session, and one run per session is enforced across connections.
`hello` became a reattach: besides the journal it carries the blocks of the run
in flight that are not journaled yet (whole, with `done`), the open questions
(`Engine.pending_questions()`), whether a run is busy, the run that did not
finish and the rules warning. The prompt block is published by the run itself
under its journal id, so every client of the session — not only the sender —
shows it, and a replay shows the same block.

A vote travels as `feedback` and learns off the read loop, since it calls a
model; the outcome reaches every client of the session as a `feedback` event.

## Web client

Blocks are content subscribers, not a transcript: a block is created on first
sight of its id and may be updated concurrently and out of order, which is what
allows reasoning, output and tool activity to stream in parallel without the page
having to model turn order.

Markdown is rendered from fully escaped text, so untrusted model output cannot
inject HTML, and URLs are allow-listed (`http(s)`, `mailto`, relative paths and
`data:` for media only) before they become links or players. Images, video and
audio are recognized by extension or data URL and rendered inline.

The UI is mobile first, centred, and shrinks content before wrapping it. It
disables itself while disconnected and reconnects with exponential backoff, so a
dropped socket degrades visibly instead of silently swallowing input.

**One mark says everything about state.** The page used to carry a word for a
title, a second word for the connection and a coloured dot beside it — three
things saying what one shape can. Now a single SVG in the top left is the title
and the status light: its colour, its ring and its motion carry connecting,
connected, working and disconnected. Status that lives in the icon cannot drift
out of sync with the icon, costs no width on a phone, and reads before it is
read. The words stay for a screen reader and in the tooltip, because a shape is
not an accessible name.

**Theming has to survive a theme that names half a palette.** A VS Code theme
defines whatever colours its author cared about, so mapping one key to one
variable left the rest on the built-in dark values — which is how a light theme
used to produce light panels with dark everything else. Two things fix it: each
variable now takes the first of several candidate keys the file actually
defines, and the stylesheet carries a complete fallback palette per theme type,
switched by `data-theme-type` on `<html>`. A theme sets what it knows; the rest
already matches.

**Icons are inline with the code.** A modern icon pack as a dependency means a
font, a build step or a network fetch for a page whose whole point is that it is
three static files. The set is a table of SVG path strings: it inherits
`currentColor`, scales with the box it sits in, themes itself for free, and adds
nothing to load.

**A block is a container, not a paragraph.** Every block has a head that
collapses it (chevron, kind icon, label) and a button group that copies its
text, so a long tool transcript or a long reasoning pass can be folded away
without losing what the model actually said. Clipboard writes fall back to a
hidden selection where the async API is barred, which is every deployment not on
`localhost` or TLS.

**Nothing on the chrome competes with the discussion.** A filled accent button
on the composer is the brightest thing on the page, and it sits next to the one
region the reader is actually reading. Every composer button now wears the same
neutral outline and only the stop icon carries a colour, and the box between
the buttons takes all the width they leave, inset by the same amount on every
side.

A bar shadow is a depth cue, and a cue that is always on says nothing. Each bar
casts a hairline only while there is content under that edge — the client
watches the discussion and writes `data-under-head` and `data-under-foot` — so
a conversation that fits on the screen sits on a flat page and the shadow
appears exactly when something is passing behind.

**Code has to be a surface, not a hole.** `--code-bg` was within two points of
`--bg` in the dark palette, so a fence read as a gap in the text rather than as
a panel. Both palettes now put a clear step between them, a hairline border
draws the edge, and — because a theme is free to point `textCodeBlock.background`
straight back at the editor background — a candidate equal to the page
background is skipped rather than used. An inline span is centred on the line it
interrupts instead of hung off the baseline: a mono face carries more descent
than the prose around it, which is what drags an unaligned chip low.

**Rendering is coalesced onto a frame.** Re-parsing the whole markdown of a
block on every delta is quadratic and, at the rate a provider emits tokens,
spends more time rendering than painting. `append` marks the body dirty and the
paint happens on the next animation frame, so the cost is one render per frame
regardless of token rate and the text still appears as it arrives.

**A question is the tool call it came from.** `ask_user` already opens a tool
block, and a separate card under it would say the same thing twice, so
`question.start` carries the id of that block and the client turns it into the
card: the question, a pill per choice, and afterwards the pressed pill or the
answer in words. The composer answers while a question is open rather than a
second text box appearing inside the card, which keeps one place to type on a
phone; the choices stay the one-tap path. The mark says whose turn it is: the
ring stops and the halo ripples outward while the run waits on the reader.

**Silence gets a clock.** The server reports a silent model every few seconds;
the timer beside the mark starts from what it reports and ticks on its own in
between, so it reads as a clock rather than jumping five seconds at a time, and
any sign of the stream stops it.

**A vote is two icons and a quarter turn.** Each finished answer carries a
thumb up and a thumb down beside copy, hidden while it streams. A vote presses
its thumb and hides the other, pulses while the rule is learned — the mark's
core turns a quarter and back meanwhile (`data-learning`) — and comes back
pressed after a reload, since the vote lives in the journal. The rule learned
arrives as a note, an oversized book as a `warning` note in the theme's
`--warn`.

**A reconnect picks up where it was.** The replay keeps the ids of the journal,
the blocks of a run in flight are revived whole and then streamed into, an open
question is a card again and Stop is live again. A prompt whose run did not
finish is drawn dashed, and the one that can still be continued wears a resume
button that sends `/resume`.

**A long prompt gets an editor, not a taller box.** The composer box is right
for a line and wrong for a page of markdown: it caps at 14 rem and highlights
nothing. A tab on the composer lifts the draft into a full-height editor over
the discussion, and a tab under the header drops it back; the two fields hand
the text and the caret to each other on every switch, so there is one draft,
never two. The draft lives in `localStorage` and only a submit clears it,
because losing a page of writing to a reload is the failure worth designing
out. A submit from the editor drops back to the discussion, since that is
where the answer appears.

**Highlighting is an overlay, not an editor dependency.** CodeMirror or Monaco
would mean a bundle, a build step or a CDN for a page that is three static
files. Instead the textarea stays the input — its own caret, selection, undo
stack, IME and accessibility — with its text transparent over a `<pre>` that
`Syntax` repaints on every keystroke. The cost is one invariant: both layers
must place every glyph identically. So `Syntax` emits each source character
exactly once, in order, and its spans change colour and slant but never
weight (a synthetic bold is a pixel wider per glyph); both layers pin one mono
face, no ligatures or kerning, the same wrapping, and a stable scrollbar gutter
so a desktop scrollbar narrows both alike. The paint follows the textarea's
scroll by a transform rather than its own scroll, so it never clamps short at
the bottom. The discussion stays laid out under the editor (`visibility:
hidden`, not `display: none`), because a hidden discussion has no height and
its auto-scroll would park it at the top while blocks arrive.

## End to end capture

`test/agentic_e2e.py` exists because the page was the one subsystem nothing looked at.
Every other part is asserted by `test/agentic_bdd.py`, while the client was only ever
checked by a person opening a browser — so the two defects below sat in plain
sight.

It serves the real web layer on a background thread and drives it with
Playwright, replacing only the three seams that reach a provider
(`build_tools`, `build_sdk_agent`, `stream`); sessions, memory, the event bridge,
the hub, the markup, the styles and the client are all the shipped ones, so a
capture is a test of the page rather than of a mock of it. The scripted run parks
itself in the middle of its answer until the capture releases it, because the
alternative — sleeping and hoping — photographs a different frame every time.
Nine images (three moments across a desktop, a phone in portrait and a phone in
landscape) are written to `assets/` and shown in [`DESIGN.md`](DESIGN.md), which
makes a layout regression something a reader can see in a diff.

The scripted run publishes a reasoning block before it calls its tool, so the
capture covers the three kinds of block the page draws rather than two, and the
middle moment is asserted as well as photographed: the reasoning must be on the
page and the answer must be there *and* incomplete. A client that buffered a
block until it ended would still take a plausible looking screenshot; it would
not pass that assertion.

## Consumer port: storynu

`utils/storynu` was rewritten onto the harness to prove the boundary: it owns
`story_prompt.md` (a Jinja template that injects `config.input`) and `story.py`,
which sets the model, the instructions, story specific config values and an
exporter that copies the finished story out of the session workspace. Everything
else it used to carry — argument parsing, streaming, tools, sandboxing — is the
harness.

---

## Phase log

1. Flat package scaffold: `pyproject.toml`, the subsystem layout of `main.py`
   and the placeholder page assets.
2. Config subsystem: `AgentConfig` with layered resolution, input resolution,
   secret masking and validation.
3. Event subsystem: typed `Event`/`EventBus` with sync and async subscribers.
4. Prompt subsystem: sandboxed, strict Jinja rendering of a consumer template.
5. Storage subsystem: `Store` + `SqliteStore` and `Cache` + `LruCache`.
6. Session subsystem: isolated workspaces, expiry and guaranteed cleanup.
7. Tool subsystem: workspace scoped file/shell/image/patch tools and a registry.
8. Core `Agent`: construction from config, `run()`, block streaming,
   cancellation and overridable hooks.
9. Console renderer: banner and block aware streaming printer, as a subscriber.
10. CLI layer: `main()` with `--input` and `--serve`.
11. Web layer: REST plus a websocket hub bridging block events to clients.
12. Web client: chat UI with out-of-order blocks, markdown and media preview,
    attachments, connection state and a mobile first layout.
13. Slash commands: one registry shared by the CLI and the UI.
14. VS Code theme support mapped onto CSS custom properties.
15. Unit tests over config, prompts, events, storage, cache, sessions, tool path
    scoping, command parsing and the protocol codec.
16. Ported `storynu` onto the harness.
17. Fixed the defects found while porting (below).
18. `README.md`: overview, subsystem map, extension points and usage.
19. Multi-model support: `ModelSpec`/`ModelPool` and the vision specialist.
20. Repository support: `repo_*` config, `RepoSpec`/`RepoManager`/`Checkout`,
    session branches, the five git tools and the matching slash commands.
21. Documentation split: this file, [`DESIGN.md`](DESIGN.md), a README reduced to
    a summary and a quickstart, and a TODO of what is still open.
22. Conversation memory: `Turn`/`Transcript`/`ConversationMemory`, replay into
    the model and into the client, budgeted trimming with optional summarising,
    and `/forget`.
23. Durable sessions: rehydration from the store, reconciliation and reaping of
    unowned workspaces, a background expiry sweeper and `session_durable`.
24. Embeddable core: the loop split out as `Engine` (config, events, prompts,
    tools, models) with `Agent` subclassing it for the batteries, a shared
    `running()` lifecycle and `turn()`, model input taken verbatim, tools from
    the caller or from an optional workspace, and `env=False` for hosts that do
    not want the `AGENT_*` environment read.
25. Resilience of model calls: the `ErrorKind` taxonomy and `classify_error`,
    `RetryPolicy` (per-attempt timeouts, bounded retries, exponential backoff
    with jitter), the stall guard around the leader's stream, `model.retry` on
    the bus, and `Usage` accounting with pricing on `agent.end`.
26. Terminal repl and config files: `Repl` as the default entry point of
    `main.py`, `--repl` and `--config` beside `--input` and `--serve`, the JSON
    and YAML file layer between the defaults and the environment
    (`find_config_file`, `read_config_file`, `AgentConfig.with_file`), and a
    template that the config file can name.
27. End to end capture: `e2e.py`, a scripted agent behind the real server,
    a Playwright pass over desktop and phone viewports, and the screenshots in
    `assets/` that `DESIGN.md` shows.
28. Env files: `.env` loaded with `python-dotenv` into the environment before
    the config file (`find_env_file`, `read_env_file`, `load_env_file`,
    `Engine(env_file=...)`), existing variables winning.
29. Liveness, resume and questions, in three scopes:
    - [x] Minimum scope: `model.wait` from `next_event()` (`stream_heartbeat`),
      the session id kept by the page and resumed through `hello`, and
      `ask_user` with `question.start`/`question.end`, `Engine.answer()` and
      the web `answer` message.
    - [x] Complete scope: the timer and the mark states (`data-waiting`,
      `data-asking`), the question card with choices and answers in words,
      the repl's `answer>` prompt with numbered choices, session scoped
      answers, cancellation that closes questions everywhere, and the tests.
    - [ ] Extended scope: in [`TODO.md`](TODO.md) (pending questions in
      `hello`, structured answers, `ask_timeout`, stdin for one shot runs).
30. Resume, boundaries and rules, in three scopes:
    - [x] Minimum scope: the write-ahead journal (prompt, tool pairs, assistant
      messages as raw items; run status; `RunResult.turns`), `outside_read` /
      `outside_write` with the Landlock sandbox and a fail closed shell, durable
      interactive state (`state_dir`, `Agent(mode=)`, `MODE_TOOLS` /
      `MODE_STATE`), and `RuleBook` with `/good`, `/bad` and the size warning.
    - [x] Complete scope: bubblewrap and Seatbelt backends, `/resume` in the
      repl and the page, runs owned by sessions that survive a dropped socket,
      `hello` as a reattach (live blocks, open questions — closing that item of
      29 — busy, unfinished, warning), the repl resuming its last session,
      thumbs and the resume button on the page, `/rules`, and the tests.
    - [ ] Extended scope: in [`TODO.md`](TODO.md) (a Windows sandbox, several
      processes on one state directory, Seatbelt verified on a Mac).
31. Markdown editor, in three scopes:
    - [x] Minimum scope: the lift and drop tabs, `data-view` on the app, the
      text and caret handed across each switch, the `Draft` kept in
      `localStorage` and cleared by a submit, submit reading the field on
      screen.
    - [x] Complete scope: `Syntax` and the textarea over its paint, copy in
      the top left, list and quote continuation, Tab and Shift+Tab indent,
      Ctrl/Cmd+Enter to send and Escape to drop, the rise and fall
      transitions, and the `edit` check in `test/agentic_e2e.py`.
    - [ ] Extended scope: in [`TODO.md`](TODO.md) (formatting shortcuts, a
      rendered preview, paste of files and images).
32. Context compaction, in three scopes:
    - [x] Minimum scope: `TokenCounter` (tiktoken or bytes, calibrated on
      usage), `ContextBudget`, `safe_cuts`, the `Compactor` tiers, `ContextRun`
      as the SDK's `call_model_input_filter`, one journal record per turn with
      every turn kept, the window behind the newest summary, the background
      compaction between runs, and the `context_*` options in place of the
      memory budgets.
    - [x] Complete scope: window lookups (`/models`, Ollama) and windows learned
      from errors, `ContextGuard`, `context.usage` / `context.compact`, the
      `compact` marker live and replayed, `/compact [focus]`, the gauge in the
      mark, under the bar and in the repl, the `compact` check in
      `test/agentic_e2e.py`, and the docs.
    - [ ] Extended scope: in [`TODO.md`](TODO.md) (paged history, pre-summary
      at a soft watermark, exact tokenizers, compaction evals, the Responses
      API).
33. Work slicing, in three scopes:
    - [x] Minimum scope: `Throughput` timed per call (first chunk, usage,
      provider cache counts; median of recent samples, idle and whole replies
      skipped), `ContextBudget.capped` and the read-time cap (`slice_stall`)
      with the read page following it, `SliceBudget` (`slice_share`), the
      budget note on every call, `Planner` and the `plan` tool (paths, confined
      lazy globs, bounded line ranges; refuse once, then keep), `plan_enabled`,
      the `SliceTest` suite and the docs.
    - [ ] Complete scope: in [`TODO.md`](TODO.md) (`delegate` workers, the
      sizing question, plan progress journaled).
    - [ ] Extended scope: in [`TODO.md`](TODO.md) (`map`, context folding,
      evals).

## Fixes worth remembering

- **Hard coded credentials.** The pre-harness code carried an API key in the
  source. Credentials are now config fields, recognized by name and masked
  everywhere they are serialized.
- **Blocking IO on the event loop.** File reads, writes and subprocesses ran
  inline in async tools and stalled streaming. Synchronous primitives now run in
  a thread and shells use `asyncio` subprocesses.
- **Unbounded shells.** A command with no timeout hung the loop with no way back.
  Shell execution has an explicit timeout, kills the process on expiry and
  reports it as text.
- **Shell timeouts that did not kill.** A leader running a nested agent (`python app\agentic.py --input …`) failed with `leader model timeout: the model sent nothing for 240s` although `shell_timeout` was 120s. On Windows the shell is `cmd.exe /c …`, `process.kill()` reached only `cmd.exe`, and the grandchild `python.exe` kept stdout/stderr open; on Python 3.12 `Process.wait()` waits for those pipes, so the tool hung until the child ended and the stall guard (`request_timeout` + `shell_timeout`) fired first, leaving the child orphaned. A 2s timeout on an 8s sleep returned after 8.1s. `Workspace.kill_tree()` now kills the whole tree (`taskkill /F /T`, or `os.killpg` on the new session the shell starts in), on cancellation as well as on timeout, and the wait for the pipes afterwards is bounded by `KILL_GRACE`; stdin is closed so a prompt cannot hang a command either. The same fix streams output and heartbeats as `tool.progress`, so a slow command is visibly alive in the console (dots) and the web UI (tailed output, seconds in the label, a pulse of the mark). The default `shell_timeout` went from 120s to 900s, since a timeout that now really kills would otherwise cut a nested agent run short.
- **Stall guard shorter than a tool call.** A leader on Ollama asked for a 1,000 line story said `I'll write a 1000-line … story …` and then failed with `leader model timeout: the model sent nothing for 120s`. Ollama's OpenAI endpoint parses tool calls out of the model's output and sends each one whole once it is complete, so while the model writes the `content` argument of a `write_file` the stream carries no bytes at all: a direct probe of `satgeze/qwenpaw-9b-heretic-1m` (Ollama 0.34.0) was 17.2s of silence for a 3.4K char call, then one chunk with every argument; a story of 1,000 lines is ~15× that. `request_timeout` (120s) was both the stall guard and the read timeout of the shared `httpx` client, so either would have cut it. The stall guard is now its own setting, `stream_timeout` (default 600s, `RetryPolicy.stall`), and `ModelPool.client()` gives the transport a read timeout of the longer of the two while connect, write and pool stay at `request_timeout`; a specialist call is still bounded by `request_timeout` as a whole.
- **Unbounded shell output.** A recursive listing (`dir /s /b` over a project with a `.venv`, ~350K chars) went whole into the leader's context and the next model turn stalled past the 120s guard, so the web UI sat frozen on the tool block. `Workspace.run_shell()` now clips output to `shell_output_limit` characters (default 32,000, `0` disables), keeping the head and tail around an `[... N chars omitted ...]` marker, and logs each clip to stderr.
- **Compaction loop on a big read.** A leader on a small Ollama window read a ~22K token file; it outgrew the usable budget, so compaction clipped it (`Compacted 22k → 4.0k tokens: … clipped 1 tool outputs`), the model saw a truncated file and read it again, whole, to be clipped the same way. `read_text_file` returned whole files and the only exit offered was the same call (the stub even said `Call the tool again`). Three fixes: `read_text_file` takes `offset` / `limit` and returns a page of whole lines capped at `read_output_limit` (default a quarter of the usable budget), ending `[lines A-B of N of path; continue with offset=B+1]`, so a fresh read never needs clipping; a cut read repeated back to back with the same arguments is refused with a pointer to the next range (`ReadRepeats`, forgotten on any write or shell call); a clipped output is headed by `CLIP_NOTE` (a repeat returns the same size), and a cleared output over a quarter of the usable budget says it is too large to read again whole.
- **A boundary the shell walked around.** With `read_outside` off and writes
  confined, `run_shell_command` could still `cat ~/.ssh/id_rsa` or write
  anywhere the process could. The outside flags now hold the shell too (an OS
  sandbox), and where no sandbox exists the shell is withheld; a
  Landlock-confined `cat` of a file outside the workspace now reads
  `Permission denied`.
- **Transcripts that outlived their sessions.** Expiry removed a session's
  workspace and record but left its transcript in the store for good; with a
  durable store that is a slow leak of every conversation. Destruction now
  notifies `SessionManager.listeners`, and the journal goes with it.
- **A run that a sweep could orphan.** A run never renewed its session's lease,
  so a run longer than `session_ttl` could have its workspace removed mid-run.
  Each checkpoint renews it, and closing a session cancels its run.
- **Path handling duplicated per tool.** Each tool resolved and validated its own
  paths, so escapes depended on which tool was called. `Workspace.resolve()` is
  now the only path gate.
- **Output paths escaping the sandbox.** Results were written wherever the caller
  asked; export is now an explicit consumer step out of the session workspace.
- **Tool errors ending runs.** Exceptions from tools aborted the loop instead of
  informing the model. One shared guard turns them into `Error: …` text, and it
  is shared by the file and repository tools so both behave identically.
- **Model calls with no bound.** A stalled provider hung a run until the client
  gave up and a rate limit ended it outright. Every call is now made under a
  `RetryPolicy`: a timeout per attempt (a stall guard for the leader's stream),
  bounded retries with jittered backoff on transient kinds only, and a stream
  that is only restarted while it has produced nothing.
- **Tokens reaching disk.** An authenticated clone URL persists in `.git/config`
  by default; the remote is rewritten to the clean URL right after cloning and
  all git output is masked.
- **A missing key refused to start keyless endpoints.** The pool passed no key
  when none was configured and relied on the SDK's `OPENAI_API_KEY` fallback —
  but when that lookup also found nothing, the SDK refused to construct a client
  and a local Ollama could not be reached over a credential it never asks for.
  An empty key now falls back to a placeholder, with a set `OPENAI_API_KEY`
  still winning.
- **Two content types on every asset.** The response header map appends rather
  than overwrites, so setting `Content-Type` on top of the `text/plain` the
  transport had already written sent both — and the browser believed the first.
  The stylesheet was fetched and ignored on every load, and the page had been
  rendering unstyled since the web layer was written. `WebServer.retype()` now
  clears the header before setting it.
- **A hidden button that stayed on screen.** `.icon-button { display: grid }`
  outranks the browser rule behind the `hidden` attribute, so hiding send and
  showing stop did neither: both sat in the composer through every run. A single
  `[hidden] { display: none !important }` rule restores the attribute.
- **`Path` inputs crashed the run.** `resolve_input()` called `.strip()` on its
  argument, so `agent.run(..., input=Path("notes.md"))` raised `AttributeError`
  despite paths being the documented way to pass input. It now accepts any
  `os.PathLike` and normalizes it with `os.fspath()` first.
- **`from agent import Agent` did not resolve.** The wheel listed `agent.py` at
  the repo root while it lives in `app/`, so the editable install exposed the
  root and neither the import nor the `agentic` script worked outside `app/`.
  Hatch now maps `app/` to the package root with `sources = ["app"]`.
- **`.env` settings silently ignored.** The docs named the inference settings
  `inference_url` / `_key` / `_llm` / `_vlm` while the code read `api_url`,
  `api_key`, `model` and `vision_model`, so `AGENT_INFERENCE_*` in a `.env`
  matched no field and every run fell back to the OpenRouter default. The fields
  are now the documented `inference_*` names (vision resolves through
  `ROLE_MODEL_FIELDS`), and the engine loads `.env` with `override=True` so a
  stale shell variable can no longer hide an edit to the file.
- **Two `SECRET_KEYS`.** The config's credential list and the event redaction
  list shared one name, so the second silently replaced the first and
  `is_secret_key()` lost its bare `key` entry — `inference_key` would have been
  serialized unmasked. The config list is now `SECRET_FIELDS`.
- **Unit tests read the developer's `.env`.** Default discovery found the real
  file in the working directory and loaded it into the test process. The test
  module now makes the default search find nothing; explicit lookups still run.
- **The page served nothing.** `WebServer.ASSETS` mapped `/` to `ui.html` while
  the files on disk were `agent_ui.*`, and the page linked `/ui.css` beside
  `/agent_ui.js`, so every asset was a 404. Asset file names are now built from
  the module stem, the page links only the served `/ui.*` paths, and a unit
  test asserts both.
- **Turns rewrote earlier turns.** Block ids were `{session}-{n}` with `n`
  counted per `RunResult`, so every run of a session restarted at `-0`. The
  client keys blocks by id, so a second prompt's reasoning and output were
  written into the first turn's blocks above the new prompt, which looked like
  output arriving before input. `RunResult.run_id` now joins the id
  (`{session}-{run}-{n}`), and a unit test asserts two runs never share one.
