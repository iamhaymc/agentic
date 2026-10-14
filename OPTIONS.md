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

### `inference_vlm`
Inference model ID (vision)
- Environment variable: `AGENTIC_INFERENCE_VLM`
- Expected format `{{VENDOR_ID}}/{{MODEL_ID}}`
- OpenRouter ID: `deepseek/deepseek-v4-flash-0731`
- Ollama ID: `huihui_ai/Qwen3.8-abliterated:27b`

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
