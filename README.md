<table>
<tbody>
<tr>
<td><img src="./data/_brand/logo_3.svg" width="64px"/></td>
<td><h1 style="color:#234;">AGENTIC</h1></td>
</tr>
</tbody>
</table>

_Yet another agent harness_

- [EXAMPLE](EXAMPLE.ipynb) — Notebook of examples
- [OPTIONS](OPTIONS.md) — Configuration properties
- [DESIGN](DESIGN.md) — Complete overview and reference
- [CHANGES](CHANGES.md) — Closed development tasks (with rationale)
- [TODO](TODO.md) — Open development tasks (sorted by impact)

## ▶️ Package (Dev)

Clone repository and setup environment:

```bash
git clone https://github.com/iamhaymc/hayhay
cd ./hayhay
./setup # or run VS Code task "init"
```

> Setup also installs `agentic` command to PATH (`uv tool install --editable .`); source edits apply without reinstall.

## ▶️ Package (Use)

Add repository as project dependency:

```bash
uv add "agentic @ git+https://github.com/iamhaymc/hayhay"
```

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

> Reach: `outside_read` / `outside_write` (repl both, web neither); the shell runs sandboxed unless both are on

```yaml
inference: # a group flattens onto inference_*
  url: ${oc.env:AGENTIC_INFERENCE_URL}
  key: ${oc.env:AGENTIC_INFERENCE_KEY}
  llm: some/general-model
  vlm: some/vision-model
```

## ▶️ Use (Agent)

> ❗ For applications that want to embed an agentic engine.

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

## ▶️ Use (Inference)

> ❗ For applications that want to embed an inference engine.

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

## ▶️ Run (Tool)

```bash
python app/agentic.py                     # no argument -> REPL (or run VS Code task "repl")
python app/agentic.py --input notes.md    # any arguments -> one-shot
```

`--input`, `--repl`, `--serve` and `--config` are the only command line arguments

## ▶️ Run (Page)

```bash
python app/agentic.py --serve # or run VS Code task "serve"
```

> Default URL: `http://127.0.0.1:8765/`

## ▶️ Run (Data)

Shrink agent reasoning into SFT data, caveman voice, every action kept:

```bash
uv run python util/caveman_data.py fetch --limit 50    # seeded sample -> data/caveman/source.jsonl
uv run python util/caveman_data.py shrink --level full # -> data/caveman/full/train.jsonl
uv run python util/caveman_data.py stats --level full  # re-summarize turns.jsonl
```

- levels `lite` / `full` / `ultra`; hard turns get one level gentler; each trace tagged `Reasoning budget: <level>`
- a turn that loses a fact, a negation or its commands (replay check) keeps its original text
- `fetch --parquet --limit 0` downloads the whole dataset (1.75 GB) instead of sampling the viewer

> Compressor: the `.env` inference config (OpenRouter), or `CAVEMAN_URL` + `CAVEMAN_LLM` ([OPTIONS](OPTIONS.md#caveman-data)); depth in [DESIGN](DESIGN.md#24-caveman-sft-data)

## ▶️ Run (Student)

Mix levels into one SFT set, train a student remotely, eval it:

```bash
uv run python util/caveman_data.py mix                                          # plan + mix/train.jsonl + mix/eval.jsonl
uv run python util/caveman_data.py shrink --planned --level ultra --traces 8    # each trace once, at its planned level
uv run util/caveman_train.py --model Qwen/Qwen3.5-4B --out runs/caveman-4b      # LoRA, any CUDA box (80 GB)
uv run python util/caveman_data.py eval --url http://gpu:8000/v1 --model caveman  # similarity vs reasoning tokens per tag
```

- plan: 5% held out, 10% original and untagged, the rest one level each (`lite` / `full` / `ultra`)
- `replay --verifier qwen/qwen3.5-9b` re-checks shrunk turns with a student-sized verifier
- Terminal-Bench (Harbor) and BFCL runs against the served student: [DESIGN](DESIGN.md#eval)

## ▶️ Write (Editor)

Long prompts get a markdown editor with highlighting:

- `⌃` tab on the composer lifts it; `⌄` tab or `Esc` drops it, the text moving with it
- `Ctrl+Enter` sends; `Tab` / `Shift+Tab` indent; `Enter` continues a list or a quote
- the unsent text is kept in `localStorage` across reloads and cleared on send
- copy (top left) puts the whole draft on the clipboard

## ▶️ Resume (Session)

Interactive state survives a crash:

- `.agentic/` in the cwd holds the database and the workspaces (`state_dir`)
- every step of a run is journaled as it finishes: prompt, tool calls, answers
- a restart resumes the session: the repl its last one, the page its saved one
- a dropped socket leaves the run going; reconnecting picks it up mid-stream
- `/resume` (or ▶ on the prompt) continues a run that crashed, failed or was stopped

## ▶️ Teach (Rules)

Votes become rules the agent keeps:

```bash
/good keep the tables  # or thumb up on an answer
/bad too verbose       # or thumb down
/rules                 # list; /rules drop <id>, /rules clear
```

- the model distils one do / avoid rule from the run behind the answer, editing a related rule instead of adding a twin
- rules join the instructions of every run; past `rules_max_chars` a warning shows

## ▶️ Compact (Context)

Chat without end; the context is compacted as it fills:

```yaml
context: # a group flattens onto context_*
  window: 0      # 0 asks the provider
  trigger: 0.85  # compact past this share of the budget
  target: 0.5    # ... down to this share
```

- **old reasoning → old tool outputs → summary of the oldest turns**, cheapest first, before every model call
- the journal keeps every turn; the page shows a folded marker where a compaction happened
- `/compact [focus]` summarises now; the mark's core fills with the context in use

## ▶️ Slice (Work)

Work too large for one pass is planned in steps that fit:

```yaml
plan:
  enabled: true  # the plan tool and the budget note
fold:
  enabled: true  # branch / return fold a finished sub-task into its summary
  ask_slices: 8  # a larger plan asks the user once (also ask_minutes, ask_cost)
slice:
  share: 0.25    # one step reads at most this share of the budget
  stall: 0.5     # a cold read of the context fits this share of stream_timeout
worker:
  model: ""      # delegate runs on this model (empty: the leader)
  max_tokens: 1000  # a worker's answer is clipped to this
map:
  parallel: 1    # map workers at once (raise for a hosted model)
```

- **measure → cap → plan → ask → delegate → recite**, enforced by the harness, not left to the model
- the leader's read and write speeds are timed per call; a slow model's budget is capped to what it reads in time, leaving time to write its longest reply
- `plan` takes steps and their inputs (paths, globs, `notes.md:1-400`), measures each and refuses a step over one slice
- a large plan (slices, minutes, cost) or a split that depends on intent asks the user once
- `delegate` runs one step in a fresh context with its own tools; only its capped answer returns
- `map` cuts inputs by tokens at line boundaries and runs one worker per part; the answers come back in order, sized to fit one slice
- a worker's steps show nested under its call on the page and indented in the repl; a reload brings them back
- `branch` / `return` fold a finished sub-task out of the context, the leader's or a worker's; only its goal and summary stay
- every call ends with a budget note: context in use, the slice, the open plan
- the plan is written through to the store: `/resume` and a restart continue it
- the plan rings the mark (one arc per step) and sits in the bar; `/plan` lists it with the speeds

## ▶️ Test

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

```bash
uv sync --extra e2e && playwright install chromium
python -m test.agentic_e2e
```
