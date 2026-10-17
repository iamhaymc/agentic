<table>
<tbody>
<tr>
<td><img src="./data/_brand/logo_3.svg" width="64px"/></td>
<td><h1 style="color:#234;">AGENTIC</h1></td>
</tr>
</tbody>
</table>

An agent that is easy to use for many things.

### Highlights

- Embedding Library
- Context Monitor
- Persistent Memory
- Pluggable Tools
- Durable Sessions
- Sandbox Workspaces
- Workspace SCM
- A2A Orchestration
- Console App + REPL
- Browser App + Host

### Documents

- [DESIGN](DESIGN.md) — Overview and reference
- [EXAMPLE](EXAMPLE.ipynb) — Notebook of examples
- [OPTIONS](OPTIONS.md) — Configuration properties
- [TODO](TODO.md) — Open development task

## ▶️ Consume

Add repository as project dependency:

```bash
uv add "agentic @ git+https://github.com/iamhaymc/agentic"
```

or clone repository and setup environment:

```bash
git clone https://github.com/iamhaymc/agentic
cd ./agentic && ./util/setup.sh
```

> `agentic` command added to PATH (source edits apply automatically).

## ▶️ Configure

Settings are resolved in layers:
**defaults → config file → env vars → constructor / program args**

A config file is `agentic.json`, `agentic.yaml` or `settings.yml`

- looked for in current directory then next to `agentic.py` as a fallback.
- Point at another one with `--config path` or `Agent(config_file="path")`
- Switch the layer off entirely with `Agent(config_file=False)`.

A `.env` file is loaded into the environment before the config file is read

- looked for in the same directories as the config file.
- the file wins over variables already set in the shell, so editing it always takes effect.
- Point at another one with `Agent(env_file="path")`, skip it with `Agent(env_file=False)`.

> Reach: `outside_read` / `outside_write` (repl both, web neither); confined shells deny network access by default, and every shell receives a scrubbed environment. [DESIGN](DESIGN.md#sandbox) — Boundaries And Command Controls.

```yaml
inference: # a group flattens onto inference_*
  url: ${oc.env:AGENTIC_INFERENCE_URL}
  key: ${oc.env:AGENTIC_INFERENCE_KEY}
  llm: some/general-model
  vlm: some/vision-model
  ldm: clef-flash # decision model (System One API)
```

## ▶️ Embed Agent

> ❗ For applications that want a capable harness

```python
import asyncio
from agentic import Agent
PROMPT = """
Summarise the following notes as bullet points.
{{ config.input }}
"""
RESULT = asyncio.run(Agent().run(PROMPT, input="notes.md"))
```

`input` is a file path or raw text. When it is a path the file is read and the
text is assigned to `config.input`, which is what the template injects.

## ▶️ Enbed Engine

> ❗ For applications that want a minimal harness

`Engine` is the inference and tool call loop without: store, sessions, memory, checkouts, console, web layer.

```python
from agentic import Engine
engine = Engine(inference_llm="some/model", inference_key=..., env=False)
result = await engine.run(
  [{"role": "user", "content": "What did I ask before?"}],
  tools=my_tools, # or workspace="/some/dir" for the built-in ones
)
print(result.output, result.ok)
```

- `env=False` keeps the `AGENTIC_*` environment (and `.env`) out of an embedded process.
- `tools=` replaces the tool list; `workspace=` (per run, or on the constructor)
  builds the sandboxed file and shell tools instead. With neither, the model runs
  with no tools at all.
- `engine.events.on(EventType.BLOCK_DELTA, handler)` streams tokens into the host
  application; nothing is printed unless you pass `console=True`.
- `session_id=` is an opaque correlation id echoed on every event and on the
  result — the engine itself keeps no session state.

`Agent` is this same class with the batteries bolted on, so anything overridden
or injected below works in both.

## ▶️ Run (Tool mode)

Interact with REPL by omitting input

```bash
python app/agentic.py                   # REPL
python app/agentic.py --repl            # REPL
python app/agentic.py --input notes.md  # One-shot
```

Arguments: `--input`, `--repl`, `--serve`, `--config`, `--effort`, `--a2a`, `--a2a-port`, `--peer`, `--repo`, `--effort`

## ▶️ Run (Host mode)

```bash
python app/agentic.py --serve
```

> Default URL: `http://127.0.0.1:8765/`

## ▶️ Run (Peer mode)

**One integrator, isolated checkouts, one shared Git origin.** Both machines need Git access to the same origin; only session/base branches are pushed automatically, never the origin's main branch.

Generate a team bearer once; copy it into both terminals, never into tracked config:

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:AGENTIC_A2A_TOKEN = "paste-the-generated-bearer"
```

Start worker in terminal 1:

```powershell
uv run agentic --a2a --a2a-port 8767 --repo https://github.com/YOU/REPO.git
```

Start integrator in terminal 2, with the same bearer:

```powershell
uv run agentic --serve --a2a --peer http://127.0.0.1:8767 --repo https://github.com/YOU/REPO.git
```

Open `http://127.0.0.1:8765/` and give it a coordinated task:

> Implement the feature. Use the peer for an independent module with explicit owned paths; work on other files while it runs. Integrate its verified result, test the combined changes, and leave the session branch ready for review.

- Each instance keeps separate state under `.agentic/peer-PORT`; the original working tree stays unchanged.
- The integrator gets `peer_submit`, `peer_prompt`, `peer_status`, `peer_cancel`, `peer_assign` and `peer_integrate`; inbound workers do not get peer tools.
- The worker validates changed paths before publishing; the integrator independently verifies the exact commit and merges once. Use existing git tools to publish the reviewed combined branch.
- For another machine: expose only its A2A port, use its LAN URL, TLS and the same bearer; keep the web UI on localhost. [DESIGN](DESIGN.md#26-a2a-peer-coordination) — LAN Setup, Trust Boundaries And Recovery; [OPTIONS](OPTIONS.md#a2a-peers) — Peer Settings.
- Windows uses AppContainer for fully confined shells; partial outside access remains withheld. [DESIGN](DESIGN.md#sandbox) — Platform Limits And Executable Quoting.

## ▶️ Prompt Editing

Long prompts get a markdown editor with highlighting:

- `⌃` tab on the composer lifts it; `⌄` tab or `Esc` drops it, the text moving with it
- `Ctrl+Enter` sends; `Tab` / `Shift+Tab` indent; `Enter` continues a list or a quote
- the unsent text is kept in `localStorage` across reloads and cleared on send
- copy (top left) puts the whole draft on the clipboard

## ▶️ Session Recovery

Interactive state survives a crash:

- `.agentic/` in the cwd holds the database and the workspaces (`state_dir`)
- multiple local processes may share it: sessions, journals, rules and plans refresh without a restart; state writes are serialized, not model calls or tools
- every step of a run is journaled as it finishes: prompt, tool calls, answers
- a restart resumes the session: the repl its last one, the page its saved one
- a dropped socket leaves the run going; reconnecting picks it up mid-stream
- `/resume` (or ▶ on the prompt) continues a run that crashed, failed or was stopped

## ▶️ Context Training

Votes become rules the agent keeps:

```bash
/good keep the tables  # or thumb up on an answer
/bad too verbose       # or thumb down
/rules                 # list; /rules drop <id>, /rules clear
```

- the model distils one do / avoid rule from the run behind the answer, editing a related rule instead of adding a twin
- rules join the instructions of every run; past `rules_max_chars` a warning shows

## ▶️ Context Handling

Chat without end; the context is compacted as it fills:

```yaml
context: # a group flattens onto context_*
  window: 0 # 0 asks the provider
  trigger: 0.85 # compact past this share of the budget
  target: 0.5 # ... down to this share
```

- **old reasoning → old tool outputs → summary of the oldest turns**, cheapest first, before every model call
- the journal keeps every turn; the page shows a folded marker where a compaction happened
- `/compact [focus]` summarises now; the mark's core fills with the context in use

## ▶️ Work Slicing

Work too large for one pass is planned in steps that fit:

```yaml
plan:
  enabled: true # the plan tool and the budget note
fold:
  enabled: true # branch / return fold a finished sub-task into its summary
  ask_slices: 8 # a larger plan asks the user once (also ask_minutes, ask_cost)
slice:
  share: 0.25 # one step reads at most this share of the budget
  stall: 0.5 # a cold read of the context fits this share of stream_timeout
worker:
  model: "" # delegate runs on this model (empty: the leader)
  max_tokens: 1000 # a worker's answer is clipped to this
map:
  parallel: 1 # map workers at once (raise for a hosted model)
```

- **measure → cap → plan → ask → delegate → recite**, enforced by the harness, not left to the model
- the leader's read and write speeds are timed per call; a slow model's budget is capped to what it reads in time, leaving time to write its longest reply
- `plan` takes steps and their inputs (paths, globs, `notes.md:1-400`), measures each and refuses a step over one slice
- a large plan (slices, minutes, cost) or a split that depends on intent asks the user once
- `delegate` runs one step in a fresh context with its own tools; only its capped answer returns
- `map` cuts inputs by tokens at line boundaries and runs one worker per part; the answers come back in order, sized to fit one slice
- a worker's steps show nested under its call on the page and indented in the repl; a reload brings them back
- `branch` / `return` fold a finished sub-task out of the context, the leader's or a worker's; only its goal and summary stay
- every call ends with a budget note: context in use, the slice, effective read-page size, stream silence timeout and open plan; pacing guidance asks for targeted reads, section-sized patches and brief explanations
- the plan is written through to the store: `/resume` and a restart continue it
- the plan rings the mark (one arc per step) and sits in the bar; `/plan` lists it with the speeds

## ▶️ Testing

BDD Test(s):

```bash
python -m unittest test.agentic_bdd -v
uv run python -m unittest discover -s util -p "caveman_data_bdd.py"  # caveman data + trainer data path, ~2 s
```

Eval(s) (slow; `--scripted` runs offline):

```bash
python -m test.agentic_eval --scripted
python -m test.agentic_eval --model qwen3:8b@http://localhost:11434/v1 --runs 3  # or the configured leader
```

E2E Test(s):

Native sandbox boundaries (local OS processes and loopback fixtures; no inference):

```bash
uv run python -S -m unittest test.agentic_sandbox_e2e -v
```

Browser checks:

```bash
uv sync --extra e2e && playwright install chromium
python -m test.agentic_e2e
```
