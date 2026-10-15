# ⏬ Agentic — TODO

Everything still open, in one flat list, most important first.
Nothing delivered is recorded here:
the reasoning behind what exists is in [CHANGES.md](CHANGES.md),
and the implementation is documented in [DESIGN.md](DESIGN.md).

---

- [ ] **Context compaction, extended scope.** Compaction is token aware,
      tiered and runs before every model call (see CHANGES.md). Still open:
  - [ ] Paged history replay: `hello` sends the newest N turns, older ones load on scroll (an endless journal is replayed whole today)
  - [ ] Pre-summarise in the background at a soft watermark, so a blocking compaction inside a run is rare
  - [ ] Exact tokenizers for open models (HF `tokenizers`) when available offline
  - [ ] Compaction quality evals (`test/agentic_eval.py`): recall of facts and files after N compactions
  - [ ] Clip a giant user message (a pasted file) the way a giant tool output is clipped
  - [ ] OpenAI Responses path with server side compaction when the endpoint is api.openai.com

- [ ] **Questions, extended scope.** `ask_user` covers one question with optional choices,
      answered by the session that asked. Still open: multi-select and structured answers
      (a number, a path, a yes/no), an optional `ask_timeout` after which the tool answers for the user,
      and a way for a one shot `--input` run to answer on stdin.

- [ ] **Decision models, as a role.** `SystemOneClient` asks Ollama
      `clef-flash` / `clef` typed questions (`Noul` / `Choice` / `Score`) and
      the `decision` role resolves from `inference_ldm` (see DESIGN.md §12). A
      decision model never drives the loop: it is a specialist behind a tool,
      like vision, and confidence, not the prompt, gates acting on its answer.
      Still open:
  - Minimum Scope (the leader decides through a tool):
    - [ ] `ModelPool.decide(spec, state, questions, …)` over one cached `SystemOneClient` per endpoint, with `policy` / `usage` / `on_retry`; no window or `num_ctx` probes for this role
    - [ ] `Decisioner` type beside `Asker` / `Delegator`, and `Engine.build_decision(config)` mirroring `build_vision` (`None` when the role is off)
    - [ ] `decide` tool in `ToolRegistry.build` (`decision=` delegate), offered only with a decision model; `decision_enabled` withholds it
    - [ ] `render_answers`: one line per question, probabilities highest first, `conf 0.95`; an empty answer says so
    - [ ] A failing decision model returns an `Error: …` line and never breaks the leader
  - Complete Scope (the harness keeps the typed answers):
    - [ ] `decision.end` event carrying the answers, and `Engine.decisions` (the run's answers by call id)
    - [ ] `decision_confidence`: below it the rendered line adds "unsure, decide for yourself" (`0` accepts all)
    - [ ] `build_distiller` on the decision role: a `Choice` keep / discard plus a `Score` of how general the rule is
    - [ ] DESIGN.md §21 (Extending): the decision role and the `decide` tool
  - Extended Scope (the harness judges with it):
    - [ ] `Compactor`: a `Noul` on whether a segment is worth summarising; `Planner`: a `Score` against `AskLimits`
    - [ ] Guardrail before writes and shell commands (70–500 ms, input-only pricing)
    - [ ] Clef images: `view_image` feeds `decide` (`images=`) instead of a vision model
    - [ ] `Decider`: a sibling of `Engine` (pool, policy, events; no `stream()`, tools or blocks) for a consumer that wants decisions without a loop
  - Tests:
    - [ ] `render_answers`: all three types, the confidence floor, an empty answer
    - [ ] The tool is absent without a decision model and present with one
    - [ ] A 429 is retried and a 401 is not (the client tests cover 503 and 404)
    - [ ] Eval (`test/agentic_eval.py`): a scripted decision responder, so the loop is asserted offline
    - [ ] Eval: one live run against Ollama `clef-flash` and one against TypeSafe `jev`, latency and cost per call

- [ ] **Rule review.** A vote keeps whatever rule the model distils. Show the
      rule for a one click keep or discard, let a second vote undo the first,
      and list, edit and drop rules from the page rather than only `/rules`.

- [ ] **Observability.** Diagnostics are `print` calls and a swallowed
      subscriber exception; SDK tracing is disabled outright. Add structured
      logging over the event bus with correlation by session and run, counters
      and timings for runs, tools and model calls, and an opt-in exporter so a
      deployment can see what the harness is doing.

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

- [ ] **Seatbelt on a real Mac.** The macOS profile is asserted as text only;
      run the sandbox tests on macOS (CI runner) and tune the read allow-list
      (`/private/var/folders`, Homebrew prefixes) against real commands.

- [ ] **Several processes on one state directory.** Sessions, journals and
      rules are cached per process, so a repl and a server sharing `.agentic/`
      overwrite each other's journals and miss each other's sessions and rules
      until a restart. Take a lock on the state directory (and say who holds
      it), or read through the store and merge on write.

- [ ] **Forge portability and a richer git workflow.** Pull requests are GitHub
      REST only, clones are shallow by default and the checkout can only commit,
      push and open a pull request. Add GitLab and Gitea (merge requests), let
      the model fetch, diff, rebase and update an existing pull request, handle
      conflicts and protected branches explicitly, and report what was rejected
      instead of failing the run.

- [ ] **Authentication and authorization on the web layer.** The server binds
      without any identity: whoever reaches the port gets a session with shell,
      file and git tools and the configured credentials. It needs a token or
      session cookie on both the HTTP and websocket handshake, ownership of
      sessions per principal, an origin check on the socket, and rate limiting
      before it is exposed anywhere but localhost.

- [ ] **Web UI cleanup**
  - [ ] Move the text copy button on the markdown editor to the top right corner of the editor view.
  - [ ] implement an efficient way for token text that fades in quickly when displayed.
  - [ ] Redesign the context usage meter so it is communicated through the main icon (maybe a ring around the icon that fills up like in vscode)

- [ ] **Repl ergonomics.** The terminal reads plain lines: there is no history
      across restarts, no editing beyond what the platform's `input()` gives,
      no command completion, no multi-line composition and no way to attach a
      file the way the web client can. Add a history file, tab completion over
      the command registry and the workspace, an explicit multi-line mode and an
      `/attach` command.

- [ ] **Editor, extended scope.** The markdown editor highlights, continues lists
      and indents. Still open: formatting shortcuts (Ctrl+B / Ctrl+I / Ctrl+K over the
      selection), a rendered preview beside or instead of the source, and pasting or
      dropping files and images into the editor as attachments.

- [ ] **Tests for the web layer and the client.** The hub handlers (prompt,
      cancel, answer, feedback, resume, reattach) are asserted over fake
      sockets, and the browser pass now checks a reload mid run, a vote and a
      resume. Still open: server tests over a real websocket, attachment limits
      end to end, error paths and most of `app/agentic_ui.js` (markdown, hints,
      themes) under assertion rather than photographs.

- [ ] **Caveman SFT, runs.** The pipeline shrinks, mixes, trains and evals
      (see DESIGN.md §24); what is left needs a GPU, money or a served student:
  - [ ] Train on a rented GPU: `util/caveman_train.py` on Qwen3.5 4B, then 9B (the local GTX 1070 only smoke-tests)
  - [ ] Shrink at full scale: `fetch --parquet --limit 0`, `mix`, `shrink --planned --traces 8` per level; ~1.1M turns, each once: ~3 days at 4 × 4 for all levels (50 traces in 2.9 min)
  - [ ] Long turns: 10% of turns hold 29% of the prose and run at `lite`; try `full` on long-only turns, keep `lite` after a failure
  - [ ] Benchmarks per level: Harbor and BFCL send no budget tag; a chat template per level or a tag-injecting proxy
  - [ ] A real 4B verifier: OpenRouter serves no Qwen3.5 4B (9B: 91% pass; a 3B of another family: 76%, noise); serve one and `replay --verifier-url`
  - [ ] Eval on the student's own history (rollout), not the teacher's verbose turns
  - [ ] Rows past `--max-len` 16k tokens are cut; a chunked loss would fit 32k on one GPU
