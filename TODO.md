# ⏬ Agentic — TODO

Everything still open, in one flat list, most important first.
Nothing delivered is recorded here:
the reasoning behind what exists is in [CHANGES.md](CHANGES.md),
and the implementation is documented in [DESIGN.md](DESIGN.md).

---

- [ ] Move the text copy button on the markdown editor to the top right corner of the editor view.
  
- [ ] implement an efficient way for token text that fades in quickly when displayed.

- [ ] Redesign the context usage meter so it is communicated through the main icon (maybe a ring around the icon that fills up like in vscode) 

- [ ] **Work slicing, extended scope.** Each call is timed, the budget is
      capped to what the model reads in time and leaves time to write its
      longest reply, `plan` measures and refuses oversized steps, asks the user
      once about a large plan, is written through to the store and shown on the
      mark, in a strip and by `/plan`, `delegate` runs a step in a fresh
      context and `map` one worker per part, their steps nested under the call,
      and a note recites budget and plan (see CHANGES.md). Still open:
  - [ ] Context folding: `branch` / `return` fold a finished sub-task into its summary
  - [ ] Evals: peak context, success and wall time on long tasks, local and hosted models
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
