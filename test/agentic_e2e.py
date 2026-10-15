#!/usr/bin/env python3
"""End to end capture of the web UI.

Starts the server with a scripted agent behind it, drives the chat page with
Playwright and writes the screenshots [`DESIGN.md`](DESIGN.md) shows into
`assets/`. Three frames are taken per device — the page after launch, the page
during a request and the page after the response — on a desktop display and on
a phone in portrait and in landscape.

    python e2e.py                       # every device, into assets/
    python e2e.py --device phone-portrait --out /tmp/shots

Nothing here reaches a provider: `ScriptedAgent` replaces the model stream with
a fixed answer that pauses halfway through, so every capture is offline, free
and the same picture every time.

Requires `playwright` and a Chromium build (`pip install -e .[e2e]` then
`playwright install chromium`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import app.agentic as A

ROOT = Path(__file__).resolve().parent

#: Where the images DESIGN.md links to are written.
ASSETS = ROOT / "test/assets"

#: Chromium builds that are looked at when Playwright cannot find its own.
BROWSER_CANDIDATES = (
    "/opt/pw-browsers/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


# ---------------------------------------------------------------------------
# Script
#
# What the captured conversation says. The answer is split in two so the run
# can stop between the halves and be photographed mid stream.
# ---------------------------------------------------------------------------

PROMPT = "Read agentic.py and summarise what the harness does."

REASONING = """\
The file is one module, so the summary should follow its sections rather than \
its classes. Read it first, then describe the three layers and how to run them.
"""

TOOL_NAME = "read_file"
TOOL_ARGUMENTS = {"path": "agentic.py"}
TOOL_RESULT = "agentic.py — 4656 lines, 16 sections"

#: What the scripted distiller learns from any vote.
RULE = "Show the reasoning before the answer."

#: What the scripted summarizer folds any conversation into (`/compact`).
SUMMARY = """\
## Goal
Understand what `agentic.py` does.
## Progress
Read the module; summarised its three layers."""

ANSWER_HEAD = """\
`agentic.py` is one flat module: the agentic loop, the batteries around it and the application shell, in that order.

- **Engine** — config, events, prompt, tools and models. `Engine.run()` streams the model and publishes the agent, block and tool events this page listens to.
"""

ANSWER_TAIL = """\
- **Agent** — the engine plus storage, sessions, memory, git checkouts, slash commands, the console renderer, the repl and the web layer.
- **Web** — a read only REST surface and a websocket hub that pushes every block to this page while it is being written.

| Entry point | What it gives you |
| --- | --- |
| `agentic --serve` | this page |
| `agentic --repl` | the terminal |
"""


#: A prompt the script answers with a plan: four steps, the first delegated.
SLICE_PROMPT = "Read the whole harness in slices and summarise each layer."

#: The steps of that plan, with the tokens each reads.
SLICE_STEPS = (
    ("Read the engine", "agentic.py:1-2400", 21_400),
    ("Read the agent", "agentic.py:2401-6000", 30_200),
    ("Read the web layer", "agentic.py:6001-9900", 28_800),
    ("Write the summary", "", 0),
)

#: The read and write speeds the script pretends to have measured.
SLICE_SPEED = (180.0, 24.0)

#: What the worker of the delegated first step reads and answers.
SLICE_READ = 'read_text_file(path="agentic.py", offset=1, limit=2400)'
SLICE_ANSWER = "The engine streams one model and its tools."
SLICE_RESULT = f"{SLICE_ANSWER}\n[worker: 1 tool calls in 2s]"


def chunks(text: str, words: int = 3) -> Iterator[str]:
    """Cut text into the small pieces a provider streams it in."""
    parts = re.findall(r"\S+\s*", text)
    for start in range(0, len(parts), words):
        yield "".join(parts[start : start + words])


# ---------------------------------------------------------------------------
# Scripted agent
# ---------------------------------------------------------------------------


class Gate:
    """A rendezvous between the scripted run and the browser driving it.

    The run stops in the middle of its answer and stays there until the capture
    releases it, so the "during request" frame is a decision rather than a race
    with the stream.
    """

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.resumed = threading.Event()

    def arm(self) -> None:
        """Ready the gate for the next run."""
        self.reached.clear()
        self.resumed.clear()

    async def hold(self) -> None:
        """Called by the run: announce the pause and wait to be let go."""
        self.reached.set()
        while not self.resumed.is_set():
            await asyncio.sleep(0.02)

    def wait(self, timeout: float = 30.0) -> bool:
        """Called by the capture: block until the run is paused."""
        return self.reached.wait(timeout)

    def release(self) -> None:
        """Called by the capture: let the rest of the answer through."""
        self.resumed.set()


class ScriptedAgent(A.Agent):
    """An agent whose model is a script: no provider, no key, no variance.

    Only the three seams that reach outward are replaced — the tool list, the
    SDK agent and the stream — so everything the capture photographs (sessions,
    memory, the event bridge, the hub, the page) is the real thing.
    """

    def __init__(
        self,
        *args: Any,
        gate: Gate | None = None,
        pace: float = 0.03,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.gate = gate or Gate()
        self.pace = pace

    def build_tools(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def build_sdk_agent(self, config: A.AgentConfig, tools: list[Any]) -> Any:
        return {"config": config, "tools": tools}

    def build_summarizer(self, config: A.AgentConfig) -> Any:
        """Fold any segment into the same summary, a moment later."""

        async def summarize(previous: str, segment: str, focus: str = "") -> str:
            await asyncio.sleep(self.pace * 10)
            return SUMMARY

        return summarize

    def build_distiller(self, config: A.AgentConfig) -> Any:
        """Turn every vote into the same rule, a moment later."""

        async def distill(messages: list[dict[str, str]]) -> str:
            await asyncio.sleep(self.pace * 10)
            return json.dumps({"action": "add", "polarity": "do", "rule": RULE})

        return distill

    async def stream(
        self,
        sdk_agent: Any,
        model_input: str | list[dict[str, Any]],
        config: A.AgentConfig,
        result: A.RunResult,
    ) -> A.RunResult:
        """Publish the scripted reasoning, tool call and answer as the real stream would."""
        if result.context is not None:  # the SDK filters the input of every call
            items = [{"role": "user", "content": model_input}] if isinstance(model_input, str) else model_input
            await result.context.fit(list(items))
        if SLICE_PROMPT in A.prompt_of(model_input):
            await self.slice(config, result)
        else:
            await self.think(result)
            await self.call_tool(result)
        block = A.Block(
            id=f"{result.session_id}-answer", kind="output", session_id=result.session_id
        )
        result.blocks.append(block)
        await self.events.publish(
            A.EventType.BLOCK_START,
            session_id=result.session_id,
            id=block.id,
            kind=block.kind,
            role=block.role,
        )
        await self.write(block, ANSWER_HEAD)
        await self.gate.hold()
        await self.write(block, ANSWER_TAIL)
        block.done = True
        await self.events.publish(
            A.EventType.BLOCK_END,
            session_id=result.session_id,
            id=block.id,
            kind=block.kind,
            text=block.text,
        )
        result.output = block.text
        return result

    async def think(self, result: A.RunResult) -> None:
        """One reasoning block, so the page shows what the model thought."""
        block = A.Block(
            id=f"{result.session_id}-reasoning",
            kind="reasoning",
            session_id=result.session_id,
        )
        result.blocks.append(block)
        await self.events.publish(
            A.EventType.BLOCK_START,
            session_id=result.session_id,
            id=block.id,
            kind=block.kind,
            role=block.role,
        )
        await self.write(block, REASONING)
        block.done = True
        await self.events.publish(
            A.EventType.BLOCK_END,
            session_id=result.session_id,
            id=block.id,
            kind=block.kind,
            text=block.text,
        )

    async def slice(self, config: A.AgentConfig, result: A.RunResult) -> None:
        """A planned run: the speeds measured, a plan of four steps, the first
        one delegated and marked done, as the plan tool and a worker would."""
        speed = self.throughput(config)
        speed.prefill, speed.decode = SLICE_SPEED
        if result.context is not None:
            await result.context.fit(list(result.context.items))
        plan = A.Plan([
            A.PlanStep(title, [spec] if spec else [], sizes=[(spec, size)] if spec else [])
            for title, spec, size in SLICE_STEPS
        ])
        await self.publish_plan(config, result, plan)
        plan.steps[0].status = "active"
        await self.publish_plan(config, result, plan)
        await self.call_tool(
            result, "delegate", {"task": SLICE_STEPS[0][0], "step": 1}, SLICE_RESULT, "slice",
            during=self.work, journal=True,
        )
        plan.steps[0].status = "done"
        await self.publish_plan(config, result, plan)

    async def work(self, result: A.RunResult, parent: str) -> None:
        """A worker's steps, nested under the call that delegated them and
        kept for its turn, as the relay does: one read, then its answer."""
        nest = {"parent": parent, "depth": 1}
        step = functools.partial(self.publish_step, result)
        read = A.ToolCall(id=f"{parent}-w0", name="read_text_file", arguments={})
        await step(A.EventType.TOOL_START, id=read.id, kind="tool", name=read.name, text=SLICE_READ, **nest)
        await asyncio.sleep(self.pace * 4)
        await step(
            A.EventType.TOOL_END, id=read.id, kind="tool", name=read.name, ok=True,
            result="[lines 1-2400 of 9900]", duration=0.004,
            text=f"{SLICE_READ} -> ok in 4 ms\n[lines 1-2400 of 9900]", **nest,
        )
        answer = f"{parent}-w1"
        await step(A.EventType.BLOCK_START, id=answer, kind="output", role="assistant", **nest)
        for chunk in chunks(SLICE_ANSWER):
            await step(A.EventType.BLOCK_DELTA, id=answer, kind="output", text=chunk, **nest)
            await asyncio.sleep(self.pace)
        await step(A.EventType.BLOCK_END, id=answer, kind="output", text=SLICE_ANSWER, **nest)

    async def publish_plan(self, config: A.AgentConfig, result: A.RunResult, plan: A.Plan) -> None:
        """Keep the plan for the session and publish it, as `Planner` does."""
        self.plans.set(result.session_id, plan)
        limits = result.context.limits() if result.context is not None else None
        await self.events.publish(
            A.EventType.PLAN_UPDATE,
            session_id=result.session_id,
            plan=plan.view(limits, self.throughput(config)),
        )

    async def call_tool(
        self,
        result: A.RunResult,
        name: str = TOOL_NAME,
        arguments: dict[str, Any] | None = None,
        output: str = TOOL_RESULT,
        suffix: str = "tool",
        during: Any = None,
        journal: bool = False,
    ) -> None:
        """One tool call, started and finished, so the page shows a tool block;
        with `journal` its turn is kept as the real stream keeps it, taking
        the steps of a worker `during` ran."""
        call = A.ToolCall(
            id=f"{result.session_id}-{suffix}",
            name=name,
            call_id=f"call-{suffix}",
            arguments=TOOL_ARGUMENTS if arguments is None else arguments,
        )
        result.tools.append(call)
        # the block of the call, as the real stream keeps one for a reconnect
        block = A.Block(id=call.id, kind="tool", session_id=result.session_id, text=call.report())
        result.blocks.append(block)
        await self.events.publish(
            A.EventType.TOOL_START,
            session_id=result.session_id,
            id=call.id,
            kind="tool",
            name=call.name,
            call_id=call.call_id,
            arguments=call.arguments,
            text=call.report(),
        )
        await asyncio.sleep(self.pace * 4)
        if during is not None:
            await during(result, call.id)
        call.finish(output)
        block.text, block.done = call.report(), True
        await self.events.publish(
            A.EventType.TOOL_END,
            session_id=result.session_id,
            id=call.id,
            kind="tool",
            name=call.name,
            call_id=call.call_id,
            arguments=call.arguments,
            ok=call.ok,
            result=call.result,
            duration=call.duration,
            text=call.report(),
        )
        if journal:
            items = [
                {"type": "function_call", "call_id": call.call_id, "name": call.name,
                 "arguments": json.dumps(call.arguments)},
                {"type": "function_call_output", "call_id": call.call_id, "output": output},
            ]
            meta = {"name": call.name, "ok": call.ok}
            await self.checkpoint(
                result, A.Turn("tool", call.report(), id=call.id, run=result.run_id, items=items, meta=meta)
            )

    async def write(self, block: A.Block, text: str) -> None:
        """Send text the way a provider does: a few words at a time."""
        for chunk in chunks(text):
            block.text += chunk
            await self.events.publish(
                A.EventType.BLOCK_DELTA,
                session_id=block.session_id,
                id=block.id,
                kind=block.kind,
                text=chunk,
            )
            await asyncio.sleep(self.pace)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def free_port(host: str = "127.0.0.1") -> int:
    """A port the server can have."""
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


class Server:
    """The real web server on a background thread, with a scripted agent behind it."""

    def __init__(
        self,
        gate: Gate,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        pace: float = 0.03,
    ) -> None:
        self.gate = gate
        self.host = host
        self.port = port or free_port(host)
        self.pace = pace
        self.workspaces = Path(tempfile.mkdtemp(prefix="agent-e2e-"))
        self.thread = threading.Thread(target=self._main, name="agent-e2e", daemon=True)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[Any] | None = None
        self.error: BaseException | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 30.0) -> "Server":
        """Start the thread and wait until the page is being served."""
        self.thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.error is not None:
                raise RuntimeError(f"the server failed to start: {self.error}")
            with contextlib.suppress(OSError, urllib.error.URLError):
                with urllib.request.urlopen(f"{self.url}/api/health", timeout=1) as body:
                    if json.load(body).get("ok"):
                        return self
            time.sleep(0.1)
        raise TimeoutError(f"the server did not answer on {self.url}")

    def stop(self, timeout: float = 10.0) -> None:
        """Cancel the server and wait for the thread to unwind."""
        if self.loop is not None and self.task is not None:
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(timeout)

    def _main(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # pragma: no cover - reported to the caller
            self.error = exc

    async def _serve(self) -> None:
        agent = ScriptedAgent(
            gate=self.gate,
            pace=self.pace,
            console=False,
            env=False,
            config_file=False,
            host=self.host,
            port=self.port,
            workspace_root=self.workspaces,
            session_sweep_interval=0,
            # offline and the same every time: no window lookup, no tokenizer
            # download, and a window small enough for the gauge to show
            context_window=4096,
            context_reserve=1024,
            context_tokenizer="",
        )
        server = A.WebServer(agent, host=self.host, port=self.port)
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.ensure_future(server.serve())
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        finally:
            await agent.aclose()


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

PHONE_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


@dataclass(frozen=True)
class Device:
    """A viewport to photograph the page in."""

    name: str
    width: int
    height: int
    scale: float = 1.0
    mobile: bool = False
    user_agent: str = ""

    def context(self) -> dict[str, Any]:
        """The Playwright context options for this device."""
        options: dict[str, Any] = {
            "viewport": {"width": self.width, "height": self.height},
            "device_scale_factor": self.scale,
            "is_mobile": self.mobile,
            "has_touch": self.mobile,
            "color_scheme": "dark",
        }
        if self.user_agent:
            options["user_agent"] = self.user_agent
        return options


#: An average desktop window and an average phone, held both ways.
DEVICES = (
    Device("desktop", 1440, 900),
    Device("phone-portrait", 390, 844, scale=2.0, mobile=True, user_agent=PHONE_AGENT),
    Device("phone-landscape", 844, 390, scale=2.0, mobile=True, user_agent=PHONE_AGENT),
)

#: The three moments of a conversation, in the order they are captured.
MOMENTS = ("launch", "request", "response")


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def find_browser(explicit: str = "") -> str:
    """The Chromium executable to drive, or an empty string for Playwright's own."""
    for candidate in (explicit, os.environ.get("AGENTIC_E2E_BROWSER", "")):
        if candidate:
            if not Path(candidate).exists():
                raise FileNotFoundError(f"no such browser: {candidate}")
            return candidate
    return ""


def fallback_browser() -> str:
    """A Chromium on this machine, when Playwright has not installed one."""
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def launch(playwright: Any, browser_path: str = "") -> Any:
    """Launch Chromium, falling back to a build already on the machine."""
    arguments = ["--no-sandbox", "--hide-scrollbars", "--force-color-profile=srgb"]
    if browser_path:
        return playwright.chromium.launch(executable_path=browser_path, args=arguments)
    try:
        return playwright.chromium.launch(args=arguments)
    except Exception:
        spare = fallback_browser()
        if not spare:
            raise
        return playwright.chromium.launch(executable_path=spare, args=arguments)


def capture(browser: Any, device: Device, url: str, gate: Gate, out: Path) -> list[Path]:
    """Drive one conversation on one device, photographing the three moments."""
    written: list[Path] = []
    context = browser.new_context(**device.context())
    page = context.new_page()
    try:
        page.goto(url, wait_until="load")
        page.wait_for_selector('#app[data-state="online"]')
        page.wait_for_function("() => !document.getElementById('input').disabled")
        written.append(shoot(page, out, device, "launch"))

        gate.arm()
        page.fill("#input", PROMPT)
        page.click("#send")
        if not gate.wait():
            raise TimeoutError("the run never reached the hold point")
        page.wait_for_function("() => !document.getElementById('stop').hidden")
        page.wait_for_function(
            "() => document.querySelector('.block[data-kind=\"output\"]"
            "[data-streaming=\"true\"]')"
        )
        settle(page)
        assert_streaming(page)
        written.append(shoot(page, out, device, "request"))

        gate.release()
        page.wait_for_function("() => document.getElementById('stop').hidden")
        page.wait_for_function(
            "() => !document.querySelector('.block[data-streaming=\"true\"]')"
        )
        settle(page)
        written.append(shoot(page, out, device, "response"))
    finally:
        context.close()
    return written


def assert_streaming(page: Any) -> None:
    """Fail the capture unless the page is painting the answer as it arrives.

    Held at the gate the page must already show the reasoning and the first
    half of the answer, and only the first half: a client that waited for the
    block to end would show nothing here.
    """
    text = page.evaluate(
        """() => {
          const body = (kind) =>
            (document.querySelector(`.block[data-kind="${kind}"] .block-body`) || {})
              .textContent || "";
          return { reasoning: body("reasoning"), output: body("output") };
        }"""
    )
    if not text["reasoning"].strip():
        raise AssertionError("the reasoning block was never shown")
    written = text["output"].strip()
    if not written:
        raise AssertionError("no part of the answer was painted while it streamed")
    if len(written) >= len(ANSWER_HEAD) + len(ANSWER_TAIL):
        raise AssertionError("the whole answer was painted at once, not as it arrived")


def recover(browser: Any, device: Device, url: str, gate: Gate) -> None:
    """Assert the page survives what a person does to it: a reload mid run
    reattaches to the run, a vote teaches a rule that a reload keeps, and a
    stopped run offers resume and resumes."""
    context = browser.new_context(**device.context())
    page = context.new_page()
    output = '.block[data-kind="output"]'

    def online() -> None:
        page.wait_for_selector('#app[data-state="online"]')
        page.wait_for_function("() => !document.getElementById('input').disabled")

    def expect(ok: Any, what: str) -> None:
        if not ok:
            raise AssertionError(f"recovery: {what}")

    try:
        page.goto(url, wait_until="load")
        online()
        gate.arm()
        page.fill("#input", PROMPT)
        page.click("#send")
        expect(gate.wait(), "the run never reached the hold point")
        page.reload()
        online()
        page.wait_for_function("() => !document.getElementById('stop').hidden")
        half = page.text_content(f"{output} .block-body") or ""
        expect("is one flat module" in half, "the half written answer was not revived")
        gate.release()
        page.wait_for_function("() => document.getElementById('stop').hidden")
        page.wait_for_function(
            "() => !document.querySelector('.block[data-streaming=\"true\"]')"
        )
        answer = page.text_content(f"{output} .block-body") or ""
        expect("read only REST" in answer, "the reattached run did not finish streaming")

        page.click(f'{output} .vote[data-vote="up"]')
        page.wait_for_function(f"() => document.body.textContent.includes({json.dumps(RULE)})")
        expect(page.get_attribute(output, "data-vote") == "up", "the vote was not pressed")
        page.reload()
        online()
        expect(page.get_attribute(output, "data-vote") == "up", "the vote did not survive a reload")

        gate.arm()
        page.fill("#input", "second request")
        page.click("#send")
        expect(gate.wait(), "the second run never reached the hold point")
        page.wait_for_function("() => !document.getElementById('stop').hidden")
        page.click("#stop")
        page.wait_for_function("() => document.getElementById('stop').hidden")
        page.reload()
        online()
        resume = page.locator('.block[data-kind="prompt"] .resume')
        expect(resume.count() == 1, "the stopped run did not offer resume")
        gate.arm()
        resume.click()
        expect(gate.wait(), "resume did not start a run")
        gate.release()
        page.wait_for_function("() => document.getElementById('stop').hidden")
    finally:
        context.close()


#: What the editor check types: a heading, a list item to continue, emphasis and code.
DRAFT = "# Plan\n\n- read **agentic.py** and `ui.js`"


def edit(browser: Any, device: Device, url: str, gate: Gate) -> None:
    """Assert the markdown editor: the lift carries the draft up, the paint
    under the textarea holds the same characters highlighted, a list item
    continues, the draft survives a reload, the drop carries it back down and
    a submit from the editor sends it, clears it and drops to the discussion."""
    context = browser.new_context(**device.context())
    page = context.new_page()
    view = "() => document.getElementById('app').dataset.view"
    draft = "() => localStorage.getItem('agentic.draft')"

    def online() -> None:
        page.wait_for_selector('#app[data-state="online"]')
        page.wait_for_function("() => !document.getElementById('input').disabled")

    def expect(ok: Any, what: str) -> None:
        if not ok:
            raise AssertionError(f"editor: {what}")

    try:
        page.goto(url, wait_until="load")
        online()
        page.fill("#input", "# Plan")
        page.click("#lift")
        expect(page.evaluate(view) == "editor", "the lift did not open the editor")
        expect(page.input_value("#editor-text") == "# Plan", "the box text did not move up")

        page.fill("#editor-text", DRAFT)
        paint = page.text_content("#editor-paint") or ""
        expect(paint == DRAFT, "the painted layer does not hold the typed characters")
        for kind in ("head", "list", "strong", "code"):
            expect(page.locator(f"#editor-paint .md-{kind}").count(), f"no .md-{kind} span")
        page.press("#editor-text", "End")
        page.press("#editor-text", "Enter")
        expect(page.input_value("#editor-text").endswith("\n- "), "the list did not continue")
        page.press("#editor-text", "Enter")
        expect(page.input_value("#editor-text") == DRAFT + "\n", "an empty item did not end the list")
        expect(page.evaluate(draft) == DRAFT + "\n", "the draft was not stored")

        page.reload()
        online()
        expect(page.input_value("#input") == DRAFT + "\n", "the draft did not survive a reload")
        page.click("#lift")
        page.fill("#editor-text", DRAFT)
        page.click("#drop")
        expect(page.evaluate(view) == "discussion", "the drop did not close the editor")
        expect(page.input_value("#input") == DRAFT, "the editor text did not move down")

        page.click("#lift")
        gate.arm()
        page.press("#editor-text", "Control+Enter")
        expect(gate.wait(), "a submit from the editor did not start a run")
        expect(page.evaluate(view) == "discussion", "a submit did not drop the editor")
        expect(page.input_value("#input") == "", "a submit did not clear the box")
        expect(page.evaluate(draft) is None, "a submit did not clear the stored draft")
        gate.release()
        page.wait_for_function("() => document.getElementById('stop').hidden")
    finally:
        context.close()


def compact(browser: Any, device: Device, url: str, gate: Gate, out: Path) -> Path:
    """Assert the context gauge and `/compact`: a run fills the vial, the
    command folds the conversation into a marker block that holds the summary
    and that a reload replays; returns a capture of it."""
    context = browser.new_context(**device.context())
    page = context.new_page()
    marker = '.block[data-kind="compact"]'

    def online() -> None:
        page.wait_for_selector('#app[data-state="online"]')
        page.wait_for_function("() => !document.getElementById('input').disabled")

    def expect(ok: Any, what: str) -> None:
        if not ok:
            raise AssertionError(f"compact: {what}")

    try:
        page.goto(url, wait_until="load")
        online()
        page.fill("#input", PROMPT)
        page.click("#send")
        page.wait_for_function("() => document.getElementById('app').dataset.gauge")
        page.wait_for_function("() => document.getElementById('stop').hidden")
        fill = page.evaluate("() => document.getElementById('app').style.getPropertyValue('--fill')")
        expect(0 < float(fill or 0) < 1, f"the gauge holds no fill ({fill!r})")
        expect("context" in (page.get_attribute("#brand", "title") or ""), "the mark does not say the context use")
        page.fill("#input", "/compact the module layers")
        page.press("#input", "Enter")
        page.wait_for_selector(f'{marker}[data-streaming="false"]')
        label = page.text_content(f"{marker} .label") or ""
        expect(label.startswith("compacted"), f"the marker is labelled {label!r}")
        expect(page.get_attribute(marker, "data-collapsed") == "true", "the marker is not folded")
        page.click(f"{marker} .block-toggle")
        body = page.text_content(f"{marker} .block-body") or ""
        expect("Goal" in body and "three layers" in body, "the summary is not in the marker")
        settle(page)
        shot = shoot(page, out, device, "compact")
        page.reload()
        online()
        expect(page.locator(marker).count() == 1, "a reload did not replay the marker")
        return shot
    finally:
        context.close()


def slicing(browser: Any, device: Device, url: str, gate: Gate, out: Path) -> Path:
    """Assert the plan, the speeds and a worker's steps: a planned run rings
    the mark with one arc per step, the strip says the progress and opens into
    the steps with what is left and how fast the model reads, the mark's label
    says both, the delegated step shows the worker's read folded and its answer
    nested in the call, and a reload keeps the plan and the worker's steps;
    returns a capture of the open plan."""
    context = browser.new_context(**device.context())
    page = context.new_page()

    def online() -> None:
        page.wait_for_selector('#app[data-state="online"]')
        page.wait_for_function("() => !document.getElementById('input').disabled")

    def expect(ok: Any, what: str) -> None:
        if not ok:
            raise AssertionError(f"slicing: {what}")

    try:
        page.goto(url, wait_until="load")
        online()
        expect(page.locator("#plan").is_hidden(), "a plan shows before there is one")
        page.fill("#input", SLICE_PROMPT)
        page.click("#send")
        page.wait_for_function("() => document.getElementById('plan-count').textContent === '1/4'")
        page.wait_for_function("() => document.getElementById('stop').hidden")
        expect(page.text_content("#plan-now") == SLICE_STEPS[1][0], "the strip names the wrong step")
        expect(page.locator("#mark-plan path").count() == 4, "the mark has no arc per step")
        expect(page.locator('#mark-plan [data-status="done"]').count() == 1, "no arc is done")
        nest = '.block[data-kind="tool"] > .block-nest'
        expect(page.locator(f"{nest} > .block").count() == 2, "the worker's steps are not nested")
        step = f'{nest} > .block[data-kind="tool"]'
        expect(page.get_attribute(step, "data-collapsed") == "true", "a finished step is not folded")
        answer = f'{nest} > .block[data-kind="output"]'
        expect(SLICE_ANSWER in (page.text_content(f"{answer} .block-body") or ""), "no nested answer")
        expect(page.locator(f"{answer} .vote").count() == 0, "a worker's answer offers a vote")
        settle(page)
        shoot(page, out, device, "worker")
        title = page.get_attribute("#brand", "title") or ""
        expect("plan 1 of 4 done" in title, f"the mark does not say the plan ({title!r})")
        expect("reads ~180 · writes ~24 tokens/s" in title, f"the mark does not say the speed ({title!r})")
        page.click("#plan")
        page.wait_for_selector("#plan-panel:not([hidden])")
        expect(page.locator("#plan-list li").count() == 4, "the panel does not list four steps")
        current = page.text_content('#plan-list li[data-current="true"]') or ""
        expect(SLICE_STEPS[1][0] in current, f"the current step is {current!r}")
        foot = page.text_content("#plan-foot") or ""
        expect("tokens left" in foot and "reads ~180" in foot, f"the panel foot says {foot!r}")
        settle(page)
        shot = shoot(page, out, device, "slicing")
        page.keyboard.press("Escape")
        expect(page.locator("#plan-panel").is_hidden(), "Escape did not close the panel")
        page.reload()
        online()
        expect(page.text_content("#plan-count") == "1/4", "a reload did not bring the plan back")
        expect(page.locator(f"{nest} > .block").count() == 2, "a reload lost the worker's steps")
        expect(page.get_attribute(step, "data-collapsed") == "true", "a replayed step is not folded")
        expect(SLICE_ANSWER in (page.text_content(f"{answer} .block-body") or ""), "a reload lost the answer")
        expect(page.locator(f"{answer} .vote").count() == 0, "a replayed worker answer offers a vote")
        return shot
    finally:
        context.close()


def settle(page: Any, milliseconds: int = 250) -> None:
    """Let the last paint land before the shutter."""
    page.wait_for_timeout(milliseconds)


def shoot(page: Any, out: Path, device: Device, moment: str) -> Path:
    """Write one viewport screenshot and return where it went."""
    path = out / f"{device.name}-{moment}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path))
    return path


def run(devices: Iterable[Device], out: Path, args: argparse.Namespace) -> list[Path]:
    """Serve the page, capture every device, and return the images written."""
    from playwright.sync_api import sync_playwright

    gate = Gate()
    server = Server(gate, host=args.host, port=args.port, pace=args.pace).start()
    written: list[Path] = []
    try:
        with sync_playwright() as playwright:
            browser = launch(playwright, find_browser(args.browser))
            try:
                for device in devices:
                    print(f"[e2e] {device.name} {device.width}x{device.height}")
                    written.extend(capture(browser, device, server.url, gate, out))
                print("[e2e] recovery: reattach, vote, resume")
                recover(browser, DEVICES[1], server.url, gate)
                print("[e2e] editor: lift, highlight, draft, drop, submit")
                edit(browser, DEVICES[1], server.url, gate)
                print("[e2e] context: gauge, /compact, marker")
                written.append(compact(browser, DEVICES[0], server.url, gate, out))
                print("[e2e] slicing: plan ring, plan strip, speeds, nested worker")
                for device in DEVICES[:2]:
                    written.append(slicing(browser, device, server.url, gate, out))
            finally:
                browser.close()
    finally:
        server.stop()
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the supported arguments."""
    parser = argparse.ArgumentParser(
        prog="e2e", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--out", default=str(ASSETS), help=f"where the images go (default: {ASSETS})"
    )
    parser.add_argument(
        "--device",
        action="append",
        choices=[device.name for device in DEVICES],
        help="capture only this device (repeatable, default: all of them)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="the address to serve on")
    parser.add_argument(
        "--port", type=int, default=0, help="the port to serve on (0 picks a free one)"
    )
    parser.add_argument(
        "--pace", type=float, default=0.03, help="seconds between streamed chunks"
    )
    parser.add_argument(
        "--browser",
        default="",
        help="a Chromium executable (default: the one Playwright installed)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: capture every device and report what was written."""
    args = parse_args(argv)
    wanted = set(args.device or [device.name for device in DEVICES])
    devices = [device for device in DEVICES if device.name in wanted]
    out = Path(args.out).expanduser().resolve()
    try:
        written = run(devices, out, args)
    except Exception as exc:
        print(f"[e2e] failed: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"[e2e] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
