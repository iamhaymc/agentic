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
python app/agentic.py                     # no argument -> REPL
python app/agentic.py --input notes.md    # any arguments -> one-shot
```

`--input`, `--repl`, `--serve` and `--config` are the only command line arguments

## ▶️ Run (Page)

```bash
python app/agentic.py --serve
```

> Default URL: `http://127.0.0.1:8765/`

## ▶️ Test

BDD Test(s):

```bash
python -m unittest test.agentic_bdd -v
```

E2E Test(s):

```bash
uv sync --extra e2e && playwright install chromium
python -m test.agentic_e2e
```
