# A2A Support — Research Archive

_Implemented transport and Git coordination; original proposal retained as history._

[DESIGN](DESIGN.md#26-a2a-peer-coordination) — Implemented Design · [OPTIONS](OPTIONS.md#a2a-peers) — Settings · [README](README.md#-run-peers) — Quickstart

Implementation tracking (2026-10-16):

- [x] Minimum Scope: authenticated adapter, literal input, durable context/task state, shared run exclusion.
- [x] Complete Scope: allowlisted peers, bounded task delivery, verified Git assignments and integration.
- [x] Extended Scope: local/LAN quickstart, 99 focused regression tests, four real-listener E2E checks, wheel build/import smoke and Python compilation.

> The draft below is superseded in full, not an implementation contract. In particular: HTTP-server extra does not include uvicorn; contexts map to sessions rather than equal them; failed runs remain failed; ownership is verified at Git handoff; admission is bounded; only A2A 1.0 is advertised.

<details>
<summary>Original proposal (superseded)</summary>

```
An implemention plan for adding A2A support.

An example use case is having two agent processes coordinating effectively
such that they can develope in the same repository without causing issues.

Specification: https://a2a-protocol.org/latest/specification/
Python Library: https://github.com/a2aproject/a2a-python
```

# A2A Support — Implementation Plan

> ❗ One `Agent` process = one A2A agent. Peers coordinate over messages; the
> repository stays the source of truth. Each process already clones per session
> and branches per session, so two processes never share a working tree.

> Key facts: `a2a-sdk` 1.2.2 · card at `/.well-known/agent-card.json` · protocol
> 1.0, 0.3 cards accepted · extra `http-server` (Starlette + uvicorn) · every API
> name below verified against `a2aproject/a2a-python@main`.

## ▶️ Decide (Before Coding)

Each of these is settled here, once, so the phases below have no open questions:

- **A2A carries the assignment; git carries the work.** A peer publishes a branch,
  the integrator merges it. Ownership is per path, stated in the message.
- **Deliver by stream, poll as fallback.** `TaskUpdater.update_status` +
  `add_artifact` over `message/stream`; `peer_status` exists only for a peer whose
  card lacks `capabilities.streaming`.
- **`contextId` is `session_id`.** One A2A context = one harness session, so a
  follow-up continues the same conversation, workspace and checkout.
- **One run per session.** `Agent.live[session_id]` already holds the in-flight
  run; an inbound task takes a per-session lock plus a global `a2a_parallel`
  semaphore (default 1), so nothing fans out unbounded.
- **Peers are configured, never discovered.** `a2a_peers` is an allowlist; empty
  means the peer tools are not registered at all and no card is ever fetched.
- **An inbound run is an embedded run.** `ask_enabled` resolves false for it: a
  peer's task must never block on a human it cannot reach.

## ▶️ Phase (Minimum Scope)

Goal: one process answers a peer's prompt.

- [ ] `pyproject.toml`: add `"a2a-sdk[http-server]>=1.2.2"` — the version whose card
      path and protobuf types this plan assumes — then `uv lock`
- [ ] `AgentConfig` (`agentic.py:539`): `a2a_enabled`, `a2a_host`, `a2a_port`,
      `a2a_url`, `a2a_name`, `a2a_description`, `a2a_token` (see ▶️ Configure)
- [ ] `EventType` (`:935`): `PEER_START` / `PEER_DELTA` / `PEER_END`, one vocabulary
      for both directions
- [ ] New `# A2A` section after `WebServer` (`:10954`): `agent_card(config)` and
      `class A2AExecutor(AgentExecutor)`
- [ ] `A2AExecutor.execute(context, event_queue)` — the SDK signature is
      `(RequestContext, EventQueue)`, not `(card, task, queue)`: build a
      `TaskUpdater`, `start_work()`, `await agent.run(template, session=<contextId>,
      input=<message text>)`, then `complete(new_agent_message(result.output))`
- [ ] Resolve `contextId` → session id once and keep it in `Session.meta["a2a"]`
      (`SessionManager.ensure` / `create`, `:1288`)
- [ ] Serve card and RPC on one Starlette app: `create_agent_card_routes(agent_card(config))`
      plus `create_jsonrpc_routes(...)`, served by `uvicorn.Server.serve()` as its own
      task (1.2.2 has no `a2a.server.apps` module: routes are composed directly)
- [ ] One CLI flag, `--a2a`, in `parse_args` (`:11307`); `mode_of` returns `"a2a"`;
      `Agent.execute` (`:10305`) dispatches it; `--serve` starts it too when
      `a2a_enabled`
- [ ] A port already in use fails with one clear log line, not a traceback
- [ ] BDD (`test/agentic_bdd.py`): card answers on an ephemeral port; a scripted
      `Engine` behind the executor reports `completed` with the scripted answer;
      `cancel` reports `canceled`; the context id is the session id
- [ ] Docs: `OPTIONS.md` keys, `DESIGN.md` section, README quickstart, CHANGES/TODO

## ▶️ Phase (Complete Scope)

Goal: a process can call a peer, and two processes coordinate.

- [ ] `class PeerAgent`: `A2ACardResolver` + `ClientFactory` / `create_client`, one
      lazy `Client` per allowlisted origin, closed on shutdown; card cached with a TTL
- [ ] Four tools via `ToolRegistry.register` (`:6482`), one factory each, withheld
      entirely when `a2a_peers` is empty: `peer_prompt` (send and stream to
      completion), `peer_submit` (returns a `task_id`), `peer_status`, `peer_cancel`
- [ ] Forward peer deltas as events on the local bus with the calling turn's
      `parent` and `depth` (`Engine.publish_step`, `:8902`; the `WorkerRelay`
      pattern at `:7535`), so console and web show peer progress
- [ ] Bound every call by `a2a_timeout` (default 600, aligned with `stream_timeout`);
      a refusal or failure returns an `Error: …` line and never breaks the run
- [ ] Check the origin against `a2a_peers`; compare a bearer with
      `secrets.compare_digest`; the card advertises the scheme when `a2a_token` is set
- [ ] Inbound while busy: wait on the per-session lock, bounded by `a2a_timeout`,
      then `TaskUpdater.reject`
- [ ] Cancel: `TaskUpdater.cancel` + cancel the asyncio task + the open shell child
      (the existing session-stop path); publish `SESSION_CLOSE` when a context ends
- [ ] Never mirror the peer transcript into the task store: the session stays the
      source of truth for history
- [ ] BDD: allowlist denial, unreachable card, timeout, cancel, context reuse,
      a second task on a busy session

## ▶️ Phase (Extended Scope)

Goal: two processes develop one repository without stepping on each other.

- [ ] Assignment shape `{module, paths, branch, base}`; a peer refuses paths outside
      its assignment and the integrator alone merges
- [ ] Integrator protocol: the peer pushes its session branch (`agent/<session_id>`)
      to the shared origin; the integrator fetches, merges once per assignment and
      answers `{branch, sha}`
- [ ] Two processes sharing one `repo_dir` serialize clone / fetch / push behind a
      lock, because git takes index and ref locks per clone directory
- [ ] Push notification config (`create_task_push_notification_config`) as the
      alternative to streaming for a peer that cannot hold a connection
- [ ] Slow e2e (`test/agentic_e2e.py`): two processes on ephemeral ports, A told to
      ask B for module X; assert the merge landed and no path was touched twice
- [ ] `test/agentic_eval.py`: a two-process assignment task
- [ ] Docs: `DESIGN.md` coordination section, README two-process quickstart

## ▶️ Configure (New Keys)

Added to `AgentConfig`, documented in `OPTIONS.md` with the `AGENTIC_` prefix:

- `a2a_enabled` — serve the A2A endpoint (`--a2a`, or a `--serve` that includes it), default `false`
- `a2a_host` / `a2a_port` — listener, default `127.0.0.1` / `8766`; the raw
  `websockets` hub keeps its own `host` / `port` (`8765`) untouched
- `a2a_url` — URL the card advertises; empty falls back to `http://{a2a_host}:{a2a_port}`
- `a2a_name` / `a2a_description` — card identity, defaulting to `name` and the instructions' first line
- `a2a_token` — shared bearer; set requires it on every inbound call
- `a2a_peers` — allowlist of peer origins; empty disables the peer tools
- `a2a_timeout` — seconds one peer call or one inbound wait may take, default `600`
- `a2a_parallel` — concurrent inbound runs, default `1`
- `a2a_card_ttl` — seconds a resolved card is cached, default `300`

## ▶️ Reference (Verified)

Checked against `a2a-python@main` / `a2a-sdk` 1.2.2 — the plan assumes exactly this:

- **Card**: `AGENT_CARD_WELL_KNOWN_PATH = '/.well-known/agent-card.json'`,
  `DEFAULT_RPC_URL = '/'`; legacy `url` / `preferredTransport` / `protocolVersion`
  cards are converted on read, so a 0.3 peer still resolves
- **Protocol**: `PROTOCOL_VERSION_CURRENT = 1.0`, `0.3` accepted; transports
  `JSONRPC` / `HTTP+JSON` / `GRPC`; types are protobuf (`a2a.types.a2a_pb2`)
- **Client**: `A2ACardResolver`, `ClientFactory`, `create_client`, `ClientConfig`,
  `ClientCallContext`, `minimal_agent_card`; `Client.send_message` / `get_task` /
  `list_tasks` / `cancel_task` / `subscribe` / `get_extended_agent_card` / `close`
- **Server**: `AgentExecutor.execute(context, event_queue)` and `cancel`;
  `TaskUpdater.start_work` / `update_status` / `add_artifact` / `complete` /
  `failed` / `reject` / `cancel` / `requires_input` / `new_agent_message`
- **Routes**: `create_agent_card_routes`, `create_jsonrpc_routes`, `create_rest_routes`,
  `add_a2a_routes_to_fastapi`, `DefaultServerCallContextBuilder`
- **Extras**: `http-server` (Starlette + uvicorn, chosen here), `fastapi`, `grpc`,
  `sqlite`, `telemetry`, `all`; `requires-python >=3.10`
- **Corrections to the first draft**: card path is `agent-card.json`, not `agent.json`;
  `execute` takes a request context and an event queue; no `a2a.server.apps` module

## ▶️ Test (BDD First)

Fast unit tests in `test/agentic_bdd.py`, slow ones in `test/agentic_e2e.py`:

- Card and RPC served on an ephemeral port, over a scripted `Engine`
- `completed`, `canceled` and `failed` paths of the executor
- One `contextId` twice: the second message sees the first turn's history
- Peer tools: allowlist denial, unreachable card, `a2a_timeout` expiry, cancel
- Busy session: a second task waits, then is rejected
- Slow e2e: two processes, A assigns module X to B, the merge lands once

## ▶️ Note (Risks)

- **Pin the SDK.** Card path, entry names and card types all moved across 0.2 → 1.x;
  a card test is the tripwire for an accidental upgrade.
- **Task store vs session.** The SDK task store holds delivery state; the harness
  session holds history. Mirroring both duplicates the transcript and doubles the
  context cost.
- **Peer text is untrusted input.** It arrives as a tool result, never as
  instructions, and never as a path outside the workspace.
- **Two clocks.** `a2a_timeout` bounds a peer call, `stream_timeout` bounds
  silence inside a run; the `model.wait` heartbeat keeps the two apart.
- **Sandbox.** An inbound task runs under the same `outside_read` / `outside_write` /
  `shell_sandbox` gates as a local one: a peer gains no reach the local user lacks.

</details>
