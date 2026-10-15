# ⏬ Agentic — TODO

Everything still open, in one flat list, most important first.
Nothing delivered is recorded here:
the reasoning behind what exists is in [CHANGES.md](CHANGES.md),
and the implementation is documented in [DESIGN.md](DESIGN.md).

---

- [ ] **Decision models, agentic use.** `SystemOneClient` calls `POST /v1/systemone`
      (Ollama `clef-flash` / `clef`) with typed `Noul` / `Choice` / `Score`
      questions, and the `decision` role resolves from `inference_ldm` (see
      CHANGES.md). Still open, one path for every use: `Agent.build_decision`
      returns a `decide(state, questions)` delegate (or `None` when the role is
      off) that publishes `decision.*` events, is accounted on the run, cached
      by `(model, state, questions)`, gated by confidence thresholds, and falls
      back to today's leader path on any failure, so a decision never fails a run.
  - Minimum Scope (the leader can decide, both UIs show it):
    - [ ] `ModelPool.decision(spec)`: one `SystemOneClient` per endpoint; `clear()` closes them
    - [ ] `Agent.build_decision(config)`: the `decide` delegate, mirroring `build_vision`
    - [ ] `decide` tool: `state` (text or workspace paths) + `questions` (wire JSON) → compact answers; offered only when the role is on
    - [ ] `decision.start` / `decision.end` events (questions, answers, ms); repl prints `⚖ team: technical 0.95`, web shows a chip with probability bars and ticks the mark
    - [ ] `/decision <model|off>` command (like `/vision`); banner and `/config` show the role
    - [ ] Live test against Ollama `clef-flash`, skipped when the server is unreachable
  - Complete Scope (harness-side gates, each behind a flag and a threshold):
    - [ ] Tool gate: before `shell`, an outside write or a push, ask "follows from the request?" and "destructive?"; run above the threshold, `ask_user` between, refuse below with the reason
    - [ ] Prompt routing: one `Choice` per user prompt (answer / plan / delegate) and one for the role (leader or worker model), before the leader's first call
    - [ ] `ask_user` answers: map free text onto the offered options; low confidence keeps the text
    - [ ] Implicit feedback: a `Noul` on the next user message (satisfied / correcting) proposes a rule without a thumb
    - [ ] Evals (`test/agentic_eval.py`) per gate: agreement with the leader, latency, leader tokens saved
  - Extended Scope:
    - [ ] Images: attachments and `view_image` data URLs as `images`, for visual gates
    - [ ] Compaction relevance: `Score` old tool outputs against the goal, clear the least relevant first
    - [ ] Tool subset: a `Choice` over tool names per turn for large registries
    - [ ] Rule dedupe: "same as an existing rule?" before a learned rule is kept
    - [ ] Decision journal: question, answer and outcome stored to tune thresholds
    - [ ] Hosted endpoints: TypeSafe `jev` (same wire, bearer key) and Cloudflare Workers AI `/ai/run` (answers wrapped in `result`)

- [ ] Move the text copy button on the markdown editor to the top right corner of the editor view.
  
- [ ] implement an efficient way for token text that fades in quickly when displayed.

- [ ] Redesign the context usage meter so it is communicated through the main icon (maybe a ring around the icon that fills up like in vscode) 

- [ ] **Work slicing, follow-ups.** Each call is timed, the budget is
      capped to what the model reads in time and leaves time to write its
      longest reply, `plan` measures and refuses oversized steps, asks the user
      once about a large plan, is written through to the store and shown on the
      mark, in a strip and by `/plan`, `delegate` runs a step in a fresh
      context and `map` one worker per part, their steps nested under the call,
      `branch` / `return` fold a finished sub-task, a note recites budget and
      plan, and `test/agentic_eval.py` measures it all (see CHANGES.md). Still
      open:
  - [ ] Run the evals (`test/agentic_eval.py`) on a local and a hosted model and record a baseline (only the scripted policy has run)
  - [ ] Fold a worker's own context too (workers get no `branch` / `return` today)
  - [ ] Replay a worker's steps after a reload (they are not journaled: a reload shows the `delegate` answer alone)

- [ ] **Context compaction, extended scope.** Compaction is token aware,
      tiered and runs before every model call (see CHANGES.md). Still open:
  - [ ] Paged history replay: `hello` sends the newest N turns, older ones load on scroll (an endless journal is replayed whole today)
  - [ ] Pre-summarise in the background at a soft watermark, so a blocking compaction inside a run is rare
  - [ ] Exact tokenizers for open models (HF `tokenizers`) when available offline
  - [ ] Compaction quality evals (`test/agentic_eval.py`): recall of facts and files after N compactions
  - [ ] Clip a giant user message (a pasted file) the way a giant tool output is clipped
  - [ ] OpenAI Responses path with server side compaction when the endpoint is api.openai.com

- [ ] **Authentication and authorization on the web layer.** The server binds
      without any identity: whoever reaches the port gets a session with shell,
      file and git tools and the configured credentials. It needs a token or
      session cookie on both the HTTP and websocket handshake, ownership of
      sessions per principal, an origin check on the socket, and rate limiting
      before it is exposed anywhere but localhost.

- [ ] **Concurrency limits and fairness.** Nothing bounds how many sessions,
      workspaces, subprocesses or runs exist at once; a handful of clients can
      exhaust the disk or the CPU of the host. Cap concurrent runs and live
      sessions, bound total workspace size, and apply backpressure on the hub
      instead of accepting work that cannot be served.

- [ ] **Stronger tool sandboxing.** The shell is now held to `outside_read` /
      `outside_write` by Landlock or bubblewrap (Linux) and Seatbelt (macOS),
      and withheld where none exists. Still open: a Windows backend
      (AppContainer, or a low integrity token over a labelled workspace) so the
      shell is not withheld there, network isolation (Landlock ABI 4 net rules,
      `--unshare-net`, a Seatbelt `network*` deny), a scrubbed environment and
      an allow/deny list of commands.

- [ ] **Several processes on one state directory.** Sessions, journals and
      rules are cached per process, so a repl and a server sharing `.agentic/`
      overwrite each other's journals and miss each other's sessions and rules
      until a restart. Take a lock on the state directory (and say who holds
      it), or read through the store and merge on write.

- [ ] **Observability.** Diagnostics are `print` calls and a swallowed
      subscriber exception; SDK tracing is disabled outright. Add structured
      logging over the event bus with correlation by session and run, counters
      and timings for runs, tools and model calls, and an opt-in exporter so a
      deployment can see what the harness is doing.

- [ ] **Tests for the web layer and the client.** The hub handlers (prompt,
      cancel, answer, feedback, resume, reattach) are asserted over fake
      sockets, and the browser pass now checks a reload mid run, a vote and a
      resume. Still open: server tests over a real websocket, attachment limits
      end to end, error paths and most of `app/agentic_ui.js` (markdown, hints,
      themes) under assertion rather than photographs.

- [ ] **Questions, extended scope.** `ask_user` covers one question with optional choices, answered by the session that asked. Still open: multi-select and structured answers (a number, a path, a yes/no), an optional `ask_timeout` after which the tool answers for the user, and a way for a one shot `--input` run to answer on stdin.

- [ ] **Repl ergonomics.** The terminal reads plain lines: there is no history
      across restarts, no editing beyond what the platform's `input()` gives,
      no command completion, no multi-line composition and no way to attach a
      file the way the web client can. Add a history file, tab completion over
      the command registry and the workspace, an explicit multi-line mode and an
      `/attach` command.

- [ ] **Forge portability and a richer git workflow.** Pull requests are GitHub
      REST only, clones are shallow by default and the checkout can only commit,
      push and open a pull request. Add GitLab and Gitea (merge requests), let
      the model fetch, diff, rebase and update an existing pull request, handle
      conflicts and protected branches explicitly, and report what was rejected
      instead of failing the run.

- [ ] **Seatbelt on a real Mac.** The macOS profile is asserted as text only;
      run the sandbox tests on macOS (CI runner) and tune the read allow-list
      (`/private/var/folders`, Homebrew prefixes) against real commands.

- [ ] **Editor, extended scope.** The markdown editor highlights, continues lists
      and indents. Still open: formatting shortcuts (Ctrl+B / Ctrl+I / Ctrl+K over the
      selection), a rendered preview beside or instead of the source, and pasting or
      dropping files and images into the editor as attachments.

- [ ] **Rule review.** A vote keeps whatever rule the model distils. Show the
      rule for a one click keep or discard, let a second vote undo the first,
      and list, edit and drop rules from the page rather than only `/rules`.
