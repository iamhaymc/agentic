# ⏬ Agentic — Options

Environment variables may also be set in a `.env` file (cwd, then beside `agentic.py`); the file wins over variables already set in the shell.

### `inference_url`
Inference API URL
- Environment variable: `AGENTIC_INFERENCE_URL`
- Expected format `http:{{SERVER_HOST}}:{{SERVER_PORT?}}/v1`
- OpenRouter URL: `https://openrouter.ai/api/v1`
- Ollama URL: `http://127.0.0.1:11434/v1 `

### `inference_key`
Inference API key.
- Environment variable: `AGENTIC_INFERENCE_KEY`
- Expected format `{{SECRET_KEY}}` or empty/null

### `inference_llm`
Inference model ID (general)
- Environment variable: `AGENTIC_INFERENCE_LLM`
- Expected format `{{VENDOR_ID}}/{{MODEL_ID}}`
- OpenRouter ID: `deepseek/deepseek-v4-flash-0731`
- Ollama ID: `huihui_ai/Qwen3.8-abliterated:27b`

### `inference_ctx`
Context size sent with every model request as `options.num_ctx` (Ollama).
- Environment variable: `AGENTIC_INFERENCE_CTX`
- Expected format `{{TOKENS}}` (integer), default `0`: look up the model's trained length (Ollama `/api/show`), else send none
- Applies to every role (leader, vision, summary)
- ❗ Ollama 0.34's OpenAI-compatible `/v1` ignores `num_ctx`; for now raise it server side (Modelfile `PARAMETER num_ctx`, or `OLLAMA_CONTEXT_LENGTH`)
- Does not set `context_window`

### `inference_vlm`
Inference model ID (vision)
- Environment variable: `AGENTIC_INFERENCE_VLM`
- Expected format `{{VENDOR_ID}}/{{MODEL_ID}}`
- OpenRouter ID: `deepseek/deepseek-v4-flash-0731`
- Ollama ID: `huihui_ai/Qwen3.8-abliterated:27b`

### `inference_ldm`
Inference model ID (decision): typed answers (`noul` / `choice` / `score`) with probabilities, no text
- Environment variable: `AGENTIC_INFERENCE_LDM`
- Expected format `{{MODEL_ID}}`, default empty: decision role off
- Ollama ID: `clef-flash` (9B, fast) or `clef` (27B), `ollama pull clef-flash`
- ❗ Needs Ollama ≥ 0.35.1 (`/v1/systemone`); restart the server after an upgrade
- Called through `SystemOneClient` (System One API), not the OpenAI client

### `decision_url`
Decision endpoint, when not the leader's.
- Environment variable: `AGENTIC_DECISION_URL`
- Expected format `http://{{SERVER_HOST}}:{{SERVER_PORT}}` (root, `/v1` or `/v1/systemone` all work), default empty: `inference_url`

### `decision_key`
Decision credential, when not the leader's; sent as a bearer token.
- Environment variable: `AGENTIC_DECISION_KEY`
- Expected format `{{SECRET_KEY}}` or empty, default empty: `inference_key` (Ollama ignores it)

### `request_timeout`
Seconds a single model request may take (a specialist call, and the connect of every request).
- Environment variable: `AGENTIC_REQUEST_TIMEOUT`
- Expected format `{{SECONDS}}` (float), default `120`, `0` disables

### `stream_timeout`
Seconds the leader's stream may send nothing before the attempt fails (`leader model timeout: the model sent nothing for …`).
- Environment variable: `AGENTIC_STREAM_TIMEOUT`
- Expected format `{{SECONDS}}` (float), default `600`, `0` disables
- Ollama sends a tool call only once it is complete, so a model writing a long file is silent until it is done; raise it for large or slow local models
- Extended by `shell_timeout` while a tool runs and restarted by each heartbeat

### `stream_heartbeat`
Seconds between heartbeats (`model.wait`) while the leader's stream is silent and no tool call is open.
- Environment variable: `AGENTIC_STREAM_HEARTBEAT`
- Expected format `{{SECONDS}}` (float), default `5`, `0` disables
- Carries the seconds waited so far and the `stream_timeout` budget; the web UI pulses the mark and shows a timer beside it
- Only observes the silence: it never restarts or extends `stream_timeout`

### `shell_timeout`
Seconds a shell command may run before it and every process it started are killed.
- Environment variable: `AGENTIC_SHELL_TIMEOUT`
- Expected format `{{SECONDS}}` (integer), default `900`
- Raise it for long commands (e.g. a nested agent run); the leader's stall guard is extended by it while a tool runs

### `shell_heartbeat`
Seconds between heartbeats (`tool.progress`) of a running shell command.
- Environment variable: `AGENTIC_SHELL_HEARTBEAT`
- Expected format `{{SECONDS}}` (float), default `5`, `0` disables
- Each heartbeat restarts the leader's stall guard, so a slow but live command is never failed as a silent model

### `read_output_limit`
Characters one `read_text_file` call returns; a longer file comes back a page of whole lines at a time.
- Environment variable: `AGENTIC_READ_OUTPUT_LIMIT`
- Expected format `{{CHARS}}` (integer), default `0`: a quarter of the usable context budget; `-1` returns files whole
- Each page ends with `[lines A-B of N of path; continue with offset=B+1]`
- A page always fits beside the conversation, so compaction never clips a fresh read (which the model would answer by reading it again)

### `outside_read`
Let the read tools (`list_directory`, `read_text_file`, `view_image`, `describe_image`) and the shell read outside the session workspace.
- Environment variable: `AGENTIC_OUTSIDE_READ`
- Expected format `true`, `false` or unset
- Unset follows the mode: repl `true`, web `false`, embedded `Engine` / one shot `--input` run `false`
- With it off, a sandboxed shell reads only the workspace and the system directories (and `HOME` points into the workspace)

### `outside_write`
Let the write tools (`write_text_file`, `apply_patch`) and the shell write outside the session workspace.
- Environment variable: `AGENTIC_OUTSIDE_WRITE`
- Expected format `true`, `false` or unset
- Unset follows the mode: repl `true`, web `false`, embedded `Engine` / one shot `--input` run `false`
- Unless both outside flags are `true`, `run_shell_command` runs in the `shell_sandbox`, or is withheld when there is none

### `shell_sandbox`
OS sandbox that holds the shell to `outside_read` / `outside_write`.
- Environment variable: `AGENTIC_SHELL_SANDBOX`
- Expected format `auto`, `landlock`, `bwrap`, `seatbelt` or `none`, default `auto`
- `auto`: Landlock (Linux 5.13+), else bubblewrap (Linux), Seatbelt (macOS); none on Windows
- `none` (or no backend available): a confined shell is withheld, never run unconfined
- Network access is not restricted

### `state_dir`
Directory that durable state lives in: `agentic.db` and `workspaces/`.
- Environment variable: `AGENTIC_STATE_DIR`
- Expected format `{{PATH}}` or unset
- Unset follows the mode: repl and web `.agentic` in the current directory, embedded `Engine` / one shot run nothing (in-memory database, temporary workspaces)
- `db_path` and `workspace_root` win when set

### `session_durable`
Keep sessions (workspaces, journals) across a shutdown or a crash, and rehydrate them on the next start.
- Environment variable: `AGENTIC_SESSION_DURABLE`
- Expected format `true`, `false` or unset
- Unset follows the mode: repl and web `true`, otherwise `false`

### `session_ttl`
Seconds an idle session lives before it and its journal are removed.
- Environment variable: `AGENTIC_SESSION_TTL`
- Expected format `{{SECONDS}}` (integer) or unset
- Unset follows the mode: repl and web `86400`, otherwise `3600`
- A running run renews it at every step

### `memory_tools`
Replay journaled tool calls (call and clipped output) to the model, not only the words exchanged.
- Environment variable: `AGENTIC_MEMORY_TOOLS`
- Expected format `true` or `false`, default `true`
- The journal keeps tool calls either way, for display and for `/resume`

### `context_window`
Tokens the leader model's context window holds.
- Environment variable: `AGENTIC_CONTEXT_WINDOW`
- Expected format `{{TOKENS}}` (integer), default `0`: ask the provider (OpenRouter, vLLM, Together, Groq, LM Studio, Ollama), else `128000`
- A context length error lowers it to the window the error names, for the rest of the process
- Ollama truncates a too long prompt silently: set it to the server's `num_ctx` when the model is not loaded yet (unknown: `8192`)

### `context_reserve`
Tokens kept free for the reply, reasoning included.
- Environment variable: `AGENTIC_CONTEXT_RESERVE`
- Expected format `{{TOKENS}}` (integer), default `0`: a fifth of the window, between `4096` and `32768`
- Raise it for a model that thinks at length

### `context_trigger`
Share of the usable budget (window minus reserve) past which the context is compacted.
- Environment variable: `AGENTIC_CONTEXT_TRIGGER`
- Expected format `{{SHARE}}` (float), default `0.85`, `0` turns compaction off
- Checked before every model call, so a long run of tool calls is compacted as it goes; between runs a background compaction starts at 0.9 × this

### `context_target`
Share of the usable budget a compaction brings the context down to.
- Environment variable: `AGENTIC_CONTEXT_TARGET`
- Expected format `{{SHARE}}` (float), default `0.5`
- The gap to `context_trigger` is how far the context grows before the next compaction (and how long a provider's prompt cache holds)

### `context_keep`
Newest tool outputs a compaction never clears.
- Environment variable: `AGENTIC_CONTEXT_KEEP`
- Expected format `{{COUNT}}` (integer), default `4`
- Older outputs are cleared first, oldest first, before anything is summarised; the call stays, its output becomes a stub

### `context_summary`
Fold what a compaction removes into a running summary (the `summary` role, falling back to the leader).
- Environment variable: `AGENTIC_CONTEXT_SUMMARY`
- Expected format `true` or `false`, default `true`
- `false` drops it with a note instead: no model call, nothing remembered of it

### `context_tokenizer`
tiktoken encoding the context is counted with.
- Environment variable: `AGENTIC_CONTEXT_TOKENIZER`
- Expected format `{{ENCODING}}` or empty, default `o200k_base`
- Empty (or a vocabulary that cannot be downloaded) estimates from the byte size; either way the count is calibrated on the usage the provider reports

### `plan_enabled`
Give the leader the `plan` tool and end every model call with a budget note.
- Environment variable: `AGENTIC_PLAN_ENABLED`
- Expected format `true` or `false`, default `true`
- The note: context in use, the slice one step may read, the model's measured read and write speeds and the open plan
- The plan is written through to the store, so `/resume` and a restart continue it

### `fold_enabled`
Give the leader and its workers `branch` and `return`: the steps of a finished sub-task are folded out of that context, its summary stays.
- Environment variable: `AGENTIC_FOLD_ENABLED`
- Expected format `true` or `false`, default `true`
- Needs compaction on (`context_trigger` > 0)
- A worker folds its own context only; it keeps both tools whatever `tools` of `delegate` / `map` names
- The fold is journaled: the next run starts from the folded context

### `slice_share`
Share of the budget one plan step may read.
- Environment variable: `AGENTIC_SLICE_SHARE`
- Expected format `{{SHARE}}` (float), default `0.25`
- A step over it is refused; sent back unchanged it is kept with a warning

### `slice_stall`
Share of `stream_timeout` a cold read of the whole context may take.
- Environment variable: `AGENTIC_SLICE_STALL`
- Expected format `{{SHARE}}` (float), default `0.5`, `0` turns the cap off
- Once the leader's read speed is measured, the budget is capped to what it reads in that time (at least `8192` tokens), so a slow local model compacts before a cold read can stall
- Time to write the longest recent reply (at most `context_reserve`) at the measured write speed is set aside first, at most half of it

### `plan_ask_slices`
Slices a plan may still read before it asks the user to go ahead.
- Environment variable: `AGENTIC_PLAN_ASK_SLICES`
- Expected format `{{COUNT}}` (integer), default `8`, `0` turns it off
- Asked once per plan, with the model's own `question` about the split when it has one; needs `ask_enabled`

### `plan_ask_minutes`
Minutes a plan may take to read (at the measured read speed) before it asks the user.
- Environment variable: `AGENTIC_PLAN_ASK_MINUTES`
- Expected format `{{MINUTES}}` (float), default `10`, `0` turns it off
- Unmeasured model: never passed

### `plan_ask_cost`
Cost of reading a plan once (at `cost_input`) past which it asks the user.
- Environment variable: `AGENTIC_PLAN_ASK_COST`
- Expected format `{{AMOUNT}}` (float, provider currency), default `1.0`, `0` turns it off
- `cost_input` unset: never passed

### `delegate_enabled`
Give the leader the `delegate` tool: one step runs in a fresh context and only its answer comes back.
- Environment variable: `AGENTIC_DELEGATE_ENABLED`
- Expected format `true` or `false`, default `true`
- The worker gets the workspace tools; no plan, no `ask_user`, no `delegate` or `map` of its own
- Also builds `map`: one worker per part of inputs cut by tokens (see `map_parallel`)

### `worker_model`
Model ID of the worker role (`delegate`).
- Environment variable: `AGENTIC_WORKER_MODEL`
- Expected format `{{VENDOR_ID}}/{{MODEL_ID}}` or empty, default empty: the leader
- `worker_api_url` / `worker_api_key` (`AGENTIC_WORKER_API_URL`, `AGENTIC_WORKER_API_KEY`) default to the leader's endpoint

### `worker_instructions`
System prompt of a worker: do one step, answer densely.
- Environment variable: `AGENTIC_WORKER_INSTRUCTIONS`
- Expected format text, default a built-in prompt
- The harness appends `Answer within about N words.` (¾ of `worker_max_tokens`)

### `worker_max_turns`
Model turns one worker may take.
- Environment variable: `AGENTIC_WORKER_MAX_TURNS`
- Expected format `{{COUNT}}` (integer), default `30`
- Past it the step fails and the leader is told so, with the calls made

### `worker_max_tokens`
Tokens of a worker's answer that reach the leader.
- Environment variable: `AGENTIC_WORKER_MAX_TOKENS`
- Expected format `{{TOKENS}}` (integer), default `1000`, `0` uncapped
- A longer answer is clipped (head and tail kept); files the worker wrote are not affected

### `map_parallel`
Workers one `map` runs at once.
- Environment variable: `AGENTIC_MAP_PARALLEL`
- Expected format `{{COUNT}}` (integer), default `1`
- `map` is built with `delegate` (`delegate_enabled`); a local server answers one request at a time, so raise it for a hosted model

### `summary_max_tokens`
Tokens a summary may grow to.
- Environment variable: `AGENTIC_SUMMARY_MAX_TOKENS`
- Expected format `{{TOKENS}}` (integer), default `4096`
- The summarizer gets four times this (at least `8192`) to answer in, so a reasoning model has room to think

### `rules_enabled`
Learn rules from votes (thumbs, `/good`, `/bad`) and append them to the instructions of every run.
- Environment variable: `AGENTIC_RULES_ENABLED`
- Expected format `true` or `false`, default `true`
- Rules are distilled by the `summary` role, which falls back to the leader

### `rules_max_chars`
Size of the learned rules past which a warning is shown.
- Environment variable: `AGENTIC_RULES_MAX_CHARS`
- Expected format `{{CHARS}}` (integer), default `4000`, `0` disables
- Shown after a vote, on page load and when the repl starts; prune with `/rules drop <id>`

### `rules_instructions`
System prompt of the rule distiller.
- Environment variable: `AGENTIC_RULES_INSTRUCTIONS`
- Expected format `{{TEXT}}`; must ask for JSON `{"action": "add" | "edit", "id", "polarity": "do" | "avoid", "rule"}`

### `rules_max_tokens`
Answer budget of the rule distiller.
- Environment variable: `AGENTIC_RULES_MAX_TOKENS`
- Expected format `{{TOKENS}}` (integer), default `400`, `0` leaves it to the provider

### `ask_enabled`
Give the leader the `ask_user` tool, which puts a question (with optional choices) to the user and waits for the answer.
- Environment variable: `AGENTIC_ASK_ENABLED`
- Expected format `true`, `false` or unset
- Unset follows the mode: repl `true` (numbered choices at an `answer>` prompt), web `true` (a question card on the discussion), embedded `Engine` / one shot `--input` run `false` (nobody to answer)
- A question waits for as long as the user takes: the stall guard never fails a run that waits on a person; stop or Ctrl-C cancels it

## Caveman data

Environment variables of `util/caveman_data.py shrink`; its flags win, `.env` is loaded (the shell wins).

**Default: the inference config (`AGENTIC_INFERENCE_URL` / `_KEY` / `_LLM`, e.g. OpenRouter) → `CAVEMAN_*` overrides as a set**

### `CAVEMAN_URL`
Compressor and verifier API URL (`--url`)
- Default `AGENTIC_INFERENCE_URL`
- LAN Ollama: `http://10.0.0.6:11434/v1` (add `--ctx 16384`: Ollama's `num_ctx`, rejected by hosted APIs)

### `CAVEMAN_KEY`
Compressor and verifier API key (`--key`)
- Default `AGENTIC_INFERENCE_KEY`, only while `CAVEMAN_URL` is unset: a key never goes to a host it was not issued for

### `CAVEMAN_LLM`
Compressor model (`--model`)
- Default `AGENTIC_INFERENCE_LLM`, only while `CAVEMAN_URL` is unset; required with it
- LAN Ollama: `huihui_ai/Qwen3.8-abliterated:27b` or `qwen3.8:27b-mlx`

### `CAVEMAN_VERIFIER`
Replay verifier model (`--verifier`)
- Default empty: the compressor
- `--no-replay` skips the verifier
