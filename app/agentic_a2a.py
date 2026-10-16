"""Authenticated A2A transport and verified, isolated Git handoffs.

All token holders are one trusted team. Remote text is always literal; remote
runs get neither peer tools nor broader local workspace permissions.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import socket
import ssl
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities, AgentCard, AgentExtension, AgentInterface, AgentSkill, CancelTaskRequest,
    GetTaskRequest, HTTPAuthSecurityScheme, ListTasksRequest, ListTasksResponse,
    Message, Part, Role, SecurityRequirement, SecurityScheme, SendMessageRequest,
    StreamResponse, SubscribeToTaskRequest, Task, TaskState, TaskStatus,
)
from a2a.utils.errors import (
    A2AError, InvalidParamsError, PushNotificationNotSupportedError,
    TaskNotCancelableError, TaskNotFoundError, UnsupportedOperationError,
)
from a2a.utils.task import apply_history_length, validate_history_length
from google.protobuf.json_format import MessageToDict, ParseDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

if __package__:
    from . import agentic as A
else:
    import agentic as A


log = logging.getLogger(__name__)
TERMINAL = frozenset((
    TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_REJECTED,
))
STOPPED = TERMINAL | {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
MAX_BODY = 256 * 1024
GIT_EXTENSION = "https://github.com/iamhaymc/agentic/a2a/git-v1"
Update = Callable[[StreamResponse], Awaitable[None]]


class PeerError(ValueError):
    """An explicit peer, delivery or integration failure."""


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https") or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment
    ):
        raise PeerError("Peer URL must be an HTTP(S) origin without credentials, query or fragment")
    host = parsed.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    port = parsed.port
    suffix = f":{port}" if port is not None and port != (443 if parsed.scheme == "https" else 80) else ""
    return f"{parsed.scheme}://{host}{suffix}"


def loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def repository(url: str) -> str:
    return url.rstrip("/").removesuffix(".git")


def portable_path(path: str) -> str:
    parts = PurePosixPath(path).parts
    if (
        not path or path != path.strip() or any(c in path for c in '<>:"\\|?*')
        or PureWindowsPath(path).is_reserved()
        or any(ord(c) < 32 for c in path) or path.startswith("/")
        or not parts or any(p in (".", "..") or p.casefold() == ".git" for p in parts)
        or str(PurePosixPath(path)) != path
        or any(p.endswith((" ", ".")) for p in parts)
    ):
        raise ValueError(f"Not a portable repository-relative path: {path!r}")
    return path


def overlaps(left: str, right: str) -> bool:
    left, right = left.casefold(), right.casefold()
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


@dataclass(frozen=True)
class Assignment:
    id: str
    repository: str
    base: str
    base_ref: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not ID.fullmatch(self.id) or not SHA.fullmatch(self.base):
            raise ValueError("Assignment requires a bounded id and immutable Git commit SHA")
        if not self.repository or not self.base_ref.startswith("refs/heads/"):
            raise ValueError("Assignment requires a configured repository and published base ref")
        if not self.paths or len(self.paths) > 64:
            raise ValueError("Assignment requires 1..64 explicit owned paths")
        for path in self.paths:
            portable_path(path)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Assignment:
        if set(value) != {"id", "repository", "base", "base_ref", "paths"}:
            raise ValueError("Unsupported assignment shape")
        if not isinstance(value["paths"], list) or not all(isinstance(p, str) for p in value["paths"]):
            raise ValueError("Assignment paths must be a list of strings")
        if not all(isinstance(value[k], str) for k in ("id", "repository", "base", "base_ref")):
            raise ValueError("Assignment fields must be strings")
        return cls(
            id=value["id"], repository=value["repository"], base=value["base"],
            base_ref=value["base_ref"], paths=tuple(value["paths"]),
        )

    def contains(self, path: str) -> bool:
        portable_path(path)
        return any(path.casefold() == p.casefold() or path.casefold().startswith(p.casefold() + "/") for p in self.paths)


class StateLease:
    """Prevent two peer services from adopting the same durable sessions."""

    def __init__(self, path: Path | None) -> None:
        self.file = None
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise PeerError("Peer state is already in use; give each instance a distinct state_dir") from exc
        self.file = handle

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None


class PeerAuth:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.token = app, f"Bearer {token}".encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if not secrets.compare_digest(headers.get(b"authorization", b""), self.token):
                await JSONResponse(
                    {"error": "A valid peer bearer is required"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
            body = bytearray()
            while True:
                part = await receive()
                if part["type"] == "http.disconnect":
                    return
                body.extend(part.get("body", b""))
                if len(body) > MAX_BODY:
                    await JSONResponse({"error": "Peer request too large"}, status_code=413)(scope, receive, send)
                    return
                if not part.get("more_body", False):
                    break
            delivered = False

            async def replay() -> Any:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
        else:
            await self.app(scope, receive, send)


class DurableTasks(TaskStore):
    NS = "a2a.tasks"

    def __init__(self, store: A.Store, ttl: float) -> None:
        self.store, self.ttl = store, ttl
        self.pruned = 0.0
        for key, record in store.list(self.NS):
            task = self.decode(record)
            if task.status.state not in TERMINAL:
                task.status.state = TaskState.TASK_STATE_FAILED
                task.status.message.CopyFrom(Message(
                    role=Role.ROLE_AGENT, parts=[Part(text="Peer restarted; work was interrupted, not replayed")],
                ))
                task.status.timestamp.GetCurrentTime()
                self.write(task)

    @staticmethod
    def decode(record: dict[str, Any]) -> Task:
        return Task.FromString(base64.b64decode(record["task"], validate=True))

    def write(self, task: Task) -> None:
        self.store.set(self.NS, task.id, {
            "task": base64.b64encode(task.SerializeToString()).decode("ascii"),
            "updated": time.time(),
        })

    async def save(self, task: Task, context: ServerCallContext) -> None:
        self.write(task)
        if time.time() - self.pruned > 60:
            self.pruned = time.time()
            for key, record in self.store.list(self.NS):
                if record["updated"] < time.time() - self.ttl and self.decode(record).status.state in TERMINAL:
                    self.store.delete(self.NS, key)
            for key, record in self.store.list("a2a.messages"):
                if record["created"] < time.time() - self.ttl:
                    self.store.delete("a2a.messages", key)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        record = self.store.get(self.NS, task_id)
        return self.decode(record) if record is not None else None

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        snapshot = InMemoryTaskStore()
        for _, record in self.store.list(self.NS):
            await snapshot.save(self.decode(record), context)
        return await snapshot.list(params, context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        self.store.delete(self.NS, task_id)


class PeerHandler(DefaultRequestHandler):
    """Deduplicate non-streaming submissions before any work can start."""

    def __init__(self, service: A2AService, executor: AgentExecutor, card: AgentCard) -> None:
        super().__init__(executor, service.tasks, card)
        self.service = service
        self.submitting = asyncio.Lock()

    async def on_message_send(self, params: SendMessageRequest, context: ServerCallContext) -> Message | Task:
        validate_history_length(params.configuration)
        if params.message.task_id:
            raise UnsupportedOperationError(message="Tasks are immutable units of work; use contextId without taskId for a new turn")
        if params.configuration.HasField("task_push_notification_config"):
            raise PushNotificationNotSupportedError
        key = params.message.message_id
        if not ID.fullmatch(key):
            raise InvalidParamsError("messageId must contain 1..128 letters, digits, hyphens or underscores")
        fingerprint = hashlib.sha256(
            params.message.SerializeToString(deterministic=True)
            + params.metadata.SerializeToString(deterministic=True)
        ).hexdigest()
        blocking = not params.configuration.return_immediately
        async with self.submitting:
            previous = self.service.store.get("a2a.messages", key)
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise InvalidParamsError("messageId was reused with different content")
                task = await self.service.tasks.get(previous.get("task_id", ""), context)
                if task is None:
                    raise InvalidParamsError("Submission outcome is unavailable; do not automatically replay its work")
            else:
                if params.message.context_id and self.service.session_for(params.message.context_id) is None:
                    raise InvalidParamsError("Unknown or expired contextId")
                self.service.store.set("a2a.messages", key, {
                    "fingerprint": fingerprint, "created": time.time(),
                })
                request = SendMessageRequest()
                request.CopyFrom(params)
                request.configuration.return_immediately = True
                task = await super().on_message_send(request, context)
                if not isinstance(task, Task):
                    return task

                self.service.store.set("a2a.messages", key, {
                    "fingerprint": fingerprint, "task_id": task.id, "created": time.time(),
                })
        if blocking and task.status.state not in STOPPED:
            async with asyncio.timeout(self.service.timeout + 5):
                try:
                    async for _ in super().on_subscribe_to_task(SubscribeToTaskRequest(id=task.id), context):
                        pass
                except (UnsupportedOperationError, TaskNotFoundError):
                    latest = await self.service.tasks.get(task.id, context)
                    if latest is None or latest.status.state not in STOPPED:
                        raise
                latest = await self.service.tasks.get(task.id, context)
                if latest is None:
                    raise InvalidParamsError("Task expired while waiting")
                task = latest
        return apply_history_length(task, params.configuration)

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext,
    ) -> AsyncIterator[Any]:
        request = SendMessageRequest()
        request.CopyFrom(params)
        request.configuration.return_immediately = True
        task = await self.on_message_send(request, context)
        yield task
        if not isinstance(task, Task) or task.status.state in STOPPED:
            return
        try:
            async for event in super().on_subscribe_to_task(SubscribeToTaskRequest(id=task.id), context):
                yield apply_history_length(event, params.configuration) if isinstance(event, Task) else event
        except (UnsupportedOperationError, TaskNotFoundError):
            latest = await self.service.tasks.get(task.id, context)
            if latest is None or latest.status.state not in STOPPED:
                raise
            yield apply_history_length(latest, params.configuration)


class PeerExecutor(AgentExecutor):
    def __init__(self, service: A2AService) -> None:
        self.service = service

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        service = self.service
        if context.task_id is None or context.context_id is None:
            raise InvalidParamsError("Task and context identifiers are required")
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        full = len(service.executions) >= service.parallel
        current = asyncio.current_task()
        assert current is not None
        if not full:
            service.executions[context.task_id] = current
        initial = context.current_task or Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            history=[context.message] if context.message is not None else [],
        )
        initial.status.timestamp.GetCurrentTime()
        await event_queue.enqueue_event(initial)
        if full:
            await updater.reject(updater.new_agent_message([Part(text="Peer is at capacity; retry later with a new messageId")]))
            return
        assignment = None
        unsubscribe = None
        try:
            await updater.submit()
            message = context.message
            if message is None or message.role != Role.ROLE_USER or any(
                p.WhichOneof("content") != "text" for p in message.parts
            ):
                raise PeerError("Only literal text message parts are supported")
            text = context.get_user_input()
            if not text.strip():
                raise PeerError("Peer prompt cannot be empty")
            raw = context.metadata.get("agentic_assignment")
            if raw is not None:
                if not isinstance(raw, dict):
                    raise PeerError("Assignment metadata must be an object")
                assignment = Assignment.from_dict(raw)
            session = service.session_for(context.context_id)
            if session is None:
                session = service.agent.sessions.create(client="a2a", a2a_context=context.context_id)
                service.store.set("a2a.contexts", context.context_id, session.id)
            elif session.meta.get("a2a_assignment"):
                raise PeerError("An assignment context is immutable; start a new task without contextId")
            async with service.agent.session_run(session), asyncio.timeout(service.timeout):
                handoff = None
                if assignment:
                    prior = service.store.get("a2a.assignments", assignment.id)
                    digest = hashlib.sha256(json.dumps(
                        {"assignment": asdict(assignment), "text": text}, sort_keys=True,
                    ).encode()).hexdigest()
                    if prior is not None:
                        if prior["digest"] != digest:
                            raise PeerError("Assignment id was reused with different content")
                        if prior["state"] != "completed":
                            raise PeerError(f"Assignment already {prior['state']}; inspect its task rather than replaying")
                        await service.finish(updater, prior["output"], prior["handoff"])
                        return
                    service.store.set("a2a.assignments", assignment.id, {
                        "digest": digest, "state": "working", "session": session.id,
                    })
                    session.meta["a2a_assignment"] = assignment.id
                    service.agent.sessions.update(session)
                    log.info("Preparing A2A assignment %s", assignment.id)
                    await service.prepare_assignment(session, assignment)
                    text += (
                        "\n\nRepository assignment: " + json.dumps(asdict(assignment))
                        + "\nWork only on the assigned paths in your private checkout. "
                        "Read other files as needed; run focused tests when available. "
                        "Do not push or open a pull request: the harness validates and publishes "
                        "your commit. The caller alone integrates it. Report tests and limitations."
                    )
                elif service.agent.repos.configured:
                    if await service.agent.prepare_session(session) is None:
                        raise PeerError("Configured repository could not be cloned")
                await updater.start_work()
                last = 0.0

                async def progress(event: A.Event) -> None:
                    nonlocal last
                    if event.session_id != session.id or time.monotonic() - last < 1:
                        return
                    if event.type not in (
                        A.EventType.BLOCK_DELTA, A.EventType.TOOL_PROGRESS,
                        A.EventType.TOOL_START, A.EventType.MODEL_WAIT,
                    ):
                        return
                    last = time.monotonic()
                    notice = str(event.data.get("text") or event.data.get("name") or "Working")[-500:]
                    await updater.update_status(
                        TaskState.TASK_STATE_WORKING,
                        updater.new_agent_message([Part(text=notice)]),
                    )

                unsubscribe = service.agent.events.on(A.EventType.ALL, progress)
                log.info("Running A2A task %s in session %s", context.task_id, session.id)
                result = await service.agent.run(
                    A.PASSTHROUGH_TEMPLATE, session=session, input_text=text,
                    ask_enabled=False, outside_read=False, outside_write=False, a2a_inbound=True,
                )
                if not result.ok:
                    raise PeerError(result.error or "Peer run failed")
                if assignment:
                    log.info("Publishing A2A assignment %s", assignment.id)
                    handoff = await service.publish_assignment(session, assignment)
                    record = service.store.get("a2a.assignments", assignment.id)
                    service.store.set("a2a.assignments", assignment.id, {
                        **record, "state": "completed", "handoff": handoff, "output": result.output,
                    })
                await service.finish(updater, result.output, handoff)
                log.info("Completed A2A task %s", context.task_id)
        except asyncio.CancelledError:
            if assignment:
                service.assignment_failed(assignment.id, "canceled")
            await updater.cancel(updater.new_agent_message([Part(text="Peer task canceled")]))
            raise
        except A.SessionBusyError as exc:
            log.info("A2A task %s rejected: %s", context.task_id, exc)
            await updater.reject(updater.new_agent_message([Part(text=str(exc))]))
        except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
            if assignment:
                service.assignment_failed(assignment.id, "failed")
            reason = str(exc) or type(exc).__name__
            log.warning("A2A task %s failed: %s", context.task_id, reason)
            await updater.failed(updater.new_agent_message([Part(text=reason)]))
        finally:
            if unsubscribe:
                unsubscribe()
            service.executions.pop(context.task_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.task_id is None:
            raise InvalidParamsError("Task identifier is required")
        task = self.service.executions.get(context.task_id or "")
        if task is None or task.done():
            raise TaskNotCancelableError(message="Task has already stopped")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class A2AService:
    def __init__(self, agent: A.Agent, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.agent, self.store = agent, agent.store
        config = agent.config
        try:
            config.a2a_token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise PeerError("a2a_token must be ASCII") from exc
        if len(config.a2a_token) < 32 or any(c.isspace() for c in config.a2a_token):
            raise PeerError("Set a2a_token to a shared random bearer of at least 32 ASCII characters")
        if (
            config.a2a_parallel < 1 or config.a2a_parallel > 32 or config.a2a_timeout <= 0
            or config.a2a_task_ttl <= 0 or not 0 <= config.a2a_port <= 65535
            or not math.isfinite(config.a2a_timeout) or not math.isfinite(config.a2a_task_ttl)
        ):
            raise PeerError("Invalid A2A concurrency, timeout, retention or port")
        self.parallel, self.timeout = config.a2a_parallel, config.a2a_timeout
        self.peers = {self.check_url(u.strip(), origin_only=True) for u in config.a2a_peers.split(",") if u.strip()}
        if bool(config.a2a_tls_cert) != bool(config.a2a_tls_key):
            raise PeerError("Set both a2a_tls_cert and a2a_tls_key")
        if not loopback(config.a2a_host) and not (config.a2a_tls_cert or config.a2a_insecure):
            raise PeerError("Non-loopback listeners require TLS or explicit a2a_insecure=true")
        host = f"[{config.a2a_host}]" if ":" in config.a2a_host else config.a2a_host
        self.url = config.a2a_url or f"{'https' if config.a2a_tls_cert else 'http'}://{host}:{config.a2a_port}"
        if config.a2a_host in ("0.0.0.0", "::") and not config.a2a_url:
            raise PeerError("Wildcard listeners require a2a_url with the reachable LAN address")
        self.url = self.check_url(self.url, origin_only=True)
        lease = Path(str(config.db_path) + ".a2a.lock") if config.db_path else None
        self.lease = StateLease(lease)
        self.tasks = DurableTasks(self.store, config.a2a_task_ttl)
        self.executions: dict[str, asyncio.Task[Any]] = {}
        self.clients: dict[str, tuple[Client, Client, float]] = {}
        self.cards: dict[str, AgentCard] = {}
        self.http = http_client
        self.owns_http = http_client is None
        self.closed = False
        self.card = self.agent_card()
        self.handler = PeerHandler(self, PeerExecutor(self), self.card)
        self.app = PeerAuth(Starlette(routes=[
            *create_agent_card_routes(self.card),
            *create_jsonrpc_routes(self.handler, "/"),
        ]), config.a2a_token)
        self.server: Any = None
        self.server_task: asyncio.Task[None] | None = None
        self.agent.sessions.listeners.append(self.session_closed)
        for key, record in self.store.list("a2a.assignments"):
            if record["state"] == "working":
                self.store.set("a2a.assignments", key, {**record, "state": "interrupted"})

    def check_url(self, url: str, *, origin_only: bool = False) -> str:
        normalized = origin(url)
        parsed = urlsplit(url)
        if origin_only and parsed.path not in ("", "/"):
            raise PeerError("Configure peer origins, not URLs with a path")
        if parsed.scheme == "http" and not loopback(parsed.hostname or "") and not self.agent.config.a2a_insecure:
            raise PeerError("Non-loopback HTTP requires explicit a2a_insecure=true; prefer TLS")
        return normalized

    def agent_card(self) -> AgentCard:
        return AgentCard(
            name=self.agent.config.name, description="Trusted repository development peer",
            version="1.0.0",
            supported_interfaces=[AgentInterface(url=self.url, protocol_binding="JSONRPC", protocol_version="1.0")],
            capabilities=AgentCapabilities(streaming=True, extensions=[AgentExtension(
                uri=GIT_EXTENSION, description="Version 1: immutable Git assignments and verified handoffs",
            )]),
            default_input_modes=["text/plain"], default_output_modes=["text/plain", "application/json"],
            skills=[AgentSkill(
                id="develop", name="Develop", description="Isolated development and verified Git handoff",
                tags=["development", "git"],
            )],
            security_schemes={"bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer"),
            )},
            security_requirements=[SecurityRequirement(schemes={"bearer": {}})],
        )

    def session_for(self, context_id: str) -> A.Session | None:
        session_id = self.store.get("a2a.contexts", context_id)
        return self.agent.sessions.get(session_id) if session_id else None

    def session_closed(self, session: A.Session) -> None:
        context_id = session.meta.get("a2a_context")
        if context_id:
            self.store.delete("a2a.contexts", context_id)

    def assignment_failed(self, assignment_id: str, state: str) -> None:
        record = self.store.get("a2a.assignments", assignment_id)
        if record and record["state"] != "completed":
            self.store.set("a2a.assignments", assignment_id, {**record, "state": state})

    async def finish(self, updater: TaskUpdater, output: str, handoff: dict[str, Any] | None) -> None:
        await updater.add_artifact([Part(text=output)], name="answer")
        if handoff is not None:
            part = ParseDict({"data": handoff}, Part())
            await updater.add_artifact([part], name="handoff")
        await updater.complete()

    async def client(self, peer: str) -> Client:
        peer = origin(peer)
        if peer not in self.peers:
            raise PeerError("Peer is not in the configured a2a_peers allowlist")
        self.check_url(peer, origin_only=True)
        cached = self.clients.get(peer)
        if cached and cached[2] > time.monotonic():
            return cached[0]
        if self.http is None:
            verify = ssl.create_default_context(cafile=str(self.agent.config.a2a_ca_file)) if self.agent.config.a2a_ca_file else True
            self.http = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.agent.config.a2a_token}"},
                timeout=httpx.Timeout(min(30, self.timeout), connect=min(10, self.timeout)),
                follow_redirects=False, trust_env=False, verify=verify,
            )
        async with asyncio.timeout(min(30, self.timeout)):
            card = await A2ACardResolver(self.http, peer).get_agent_card()
        if not card.supported_interfaces:
            raise PeerError("Peer card has no supported interfaces")
        for interface in card.supported_interfaces:
            if self.check_url(interface.url) != peer:
                raise PeerError("Peer card redirected RPC to an unapproved origin")
        client = ClientFactory(ClientConfig(
            httpx_client=self.http, streaming=True,
        )).create(card)
        submit_client = ClientFactory(ClientConfig(
            httpx_client=self.http, streaming=False,
        )).create(card)
        self.clients[peer] = client, submit_client, time.monotonic() + 300
        self.cards[peer] = card
        return client

    @staticmethod
    def describe(task: Task, peer: str) -> dict[str, Any]:
        output, handoff = "", None
        for artifact in task.artifacts:
            if artifact.name == "answer":
                output = "".join(p.text for p in artifact.parts if p.WhichOneof("content") == "text")
            elif artifact.name == "handoff":
                for part in artifact.parts:
                    if part.WhichOneof("content") == "data":
                        handoff = MessageToDict(part.data)
        return {
            "peer": peer, "task_id": task.id, "context_id": task.context_id,
            "state": TaskState.Name(task.status.state), "output": output,
            "message": "".join(p.text for p in task.status.message.parts),
            **({"handoff": handoff} if handoff is not None else {}),
        }

    async def submit(
        self, peer: str, text: str, *, context_id: str = "",
        request_id: str = "", assignment: Assignment | None = None,
    ) -> dict[str, Any]:
        peer = origin(peer)
        await self.client(peer)
        request_id = request_id or uuid.uuid4().hex
        if not ID.fullmatch(request_id) or not text.strip():
            raise PeerError("A bounded request_id and nonempty prompt are required")
        request = SendMessageRequest(message=Message(
            role=Role.ROLE_USER, message_id=request_id, context_id=context_id, parts=[Part(text=text)],
        ))
        request.configuration.return_immediately = True
        if assignment:
            ParseDict({"agentic_assignment": asdict(assignment)}, request.metadata)
        # Submission must use SendMessage, not SendStreamingMessage: its return
        # mode is well defined even when the peer supports streams.
        submit_client = self.clients[peer][1]
        try:
            async with asyncio.timeout(min(30, self.timeout)):
                async for event in submit_client.send_message(request):
                    if event.HasField("task"):
                        return {**self.describe(event.task, peer), "request_id": request_id}
                    if event.HasField("message"):
                        return {
                            "peer": peer, "request_id": request_id, "state": "TASK_STATE_COMPLETED",
                            "output": "".join(p.text for p in event.message.parts),
                        }
        except (httpx.HTTPError, A2AError, TimeoutError) as exc:
            raise PeerError(
                f"Submission {request_id} outcome is uncertain; recover only by sending the identical "
                f"prompt and context with request_id={request_id}: {exc}"
            ) from exc
        raise PeerError(f"Submission {request_id} returned no task or message; do not blindly resubmit")

    async def status(self, peer: str, task_id: str) -> dict[str, Any]:
        client = await self.client(peer)
        async with asyncio.timeout(min(30, self.timeout)):
            task = await client.get_task(GetTaskRequest(id=task_id, history_length=0))
        return self.describe(task, origin(peer))

    async def cancel(self, peer: str, task_id: str) -> dict[str, Any]:
        client = await self.client(peer)
        async with asyncio.timeout(min(30, self.timeout)):
            task = await client.cancel_task(CancelTaskRequest(id=task_id))
        state = self.describe(task, origin(peer))
        self.update_outbound(peer, task_id, state)
        return state

    def update_outbound(self, peer: str, task_id: str, state: dict[str, Any]) -> None:
        for key, record in self.store.list("a2a.outbound"):
            if record.get("peer") == origin(peer) and record.get("task_id") == task_id:
                self.store.set("a2a.outbound", key, {**record, "remote": state})

    async def wait(self, peer: str, task_id: str, update: Update | None = None) -> dict[str, Any]:
        client = await self.client(peer)
        async with asyncio.timeout(self.timeout):
            state = await self.status(peer, task_id)
            if state["state"] not in {TaskState.Name(s) for s in STOPPED}:
                try:
                    async for event in client.subscribe(SubscribeToTaskRequest(id=task_id)):
                        if update:
                            await update(event)
                except (httpx.HTTPError, A2AError, NotImplementedError) as exc:
                    log.warning("Peer stream interrupted for %s; retrieving task state: %s", task_id, exc)
            while True:
                state = await self.status(peer, task_id)
                self.update_outbound(peer, task_id, state)
                if state["state"] in {TaskState.Name(s) for s in STOPPED}:
                    return state
                await asyncio.sleep(1)

    async def prepare_assignment(self, session: A.Session, assignment: Assignment) -> None:
        if repository(self.agent.repos.spec.clone_url) != repository(assignment.repository):
            raise PeerError("Assignment repository does not match this peer's configured repo_url")
        log.info("Cloning checkout for assignment %s", assignment.id)
        checkout = await self.agent.prepare_session(session)
        if checkout is None:
            raise PeerError("Assignment checkout could not be created")
        git, spec = self.agent.repos.git, self.agent.repos.spec
        log.info("Fetching immutable base for assignment %s", assignment.id)
        await git("check-ref-format", assignment.base_ref)
        await git(
            "fetch", "--no-tags", spec.authenticated_url(), assignment.base_ref,
            cwd=checkout.path,
        )
        if await git("rev-parse", "FETCH_HEAD", cwd=checkout.path) != assignment.base:
            raise PeerError("Published assignment base moved; refusing a different commit")
        await git("checkout", "-B", checkout.branch, assignment.base, cwd=checkout.path)
        log.info("Prepared assignment %s", assignment.id)

    async def changed_paths(self, checkout: A.Checkout, base: str) -> list[str]:
        git = self.agent.repos.git
        changed = await git("diff", "--no-renames", "--name-only", "-z", base, cwd=checkout.path)
        untracked = await git("ls-files", "--others", "--exclude-standard", "-z", cwd=checkout.path)
        return sorted({p for p in (changed + "\0" + untracked).split("\0") if p})

    async def validate_diff(self, checkout: A.Checkout, assignment: Assignment, sha: str = "") -> None:
        git = self.agent.repos.git
        if sha:
            raw = await git("diff", "--no-renames", "--name-only", "-z", assignment.base, sha, cwd=checkout.path)
            paths = [p for p in raw.split("\0") if p]
        else:
            paths = await self.changed_paths(checkout, assignment.base)
        denied = [p for p in paths if not assignment.contains(p)]
        if denied:
            raise PeerError("Out-of-assignment changes: " + ", ".join(denied))
        entries = await git(
            *(("ls-tree", "-r", "-z", sha) if sha else ("ls-files", "--stage", "-z")),
            cwd=checkout.path,
        )
        if any(
            e.startswith(("120000 ", "160000 ")) and e.partition("\t")[2] in paths
            for e in entries.split("\0")
        ) or (not sha and any((checkout.path / p).is_symlink() for p in paths)):
            raise PeerError("Assignment handoffs do not support symlinks or submodules")

    async def publish_assignment(self, session: A.Session, assignment: Assignment) -> dict[str, Any]:
        checkout = self.agent.checkout(session)
        if checkout is None:
            raise PeerError("Assignment lost its checkout")
        git = self.agent.repos.git
        if await git("branch", "--show-current", cwd=checkout.path) != checkout.branch:
            raise PeerError("Assignment changed branches")
        await git("merge-base", "--is-ancestor", assignment.base, "HEAD", cwd=checkout.path)
        await self.validate_diff(checkout, assignment)
        if await git("status", "--porcelain", cwd=checkout.path):
            await checkout.commit(f"Complete peer assignment {assignment.id}")
        sha = await git("rev-parse", "HEAD", cwd=checkout.path)
        await self.validate_diff(checkout, assignment, sha)
        await checkout.push()
        return {
            "assignment_id": assignment.id, "repository": assignment.repository,
            "base": assignment.base, "branch": checkout.branch, "sha": sha,
            "paths": list(assignment.paths),
        }

    async def assign(
        self, peer: str, prompt: str, paths: list[str], session_id: str,
        *, assignment_id: str = "",
    ) -> dict[str, Any]:
        session = self.agent.sessions.get(session_id)
        checkout = self.agent.checkout(session)
        if session is None or checkout is None:
            raise PeerError("A repository checkout is required")
        async with self.agent.session_run(session), self.agent.repos.transaction(checkout.path):
            return await self._assign(peer, prompt, paths, session_id, assignment_id=assignment_id)

    async def _assign(
        self, peer: str, prompt: str, paths: list[str], session_id: str,
        *, assignment_id: str = "",
    ) -> dict[str, Any]:
        peer = self.check_url(peer, origin_only=True)
        if peer not in self.peers:
            raise PeerError("Peer is not in the configured a2a_peers allowlist")
        await self.client(peer)
        if not any(e.uri == GIT_EXTENSION for e in self.cards[peer].capabilities.extensions):
            raise PeerError("Peer does not advertise the Git assignment extension")
        session = self.agent.sessions.get(session_id)
        checkout = self.agent.checkout(session)
        if checkout is None:
            raise PeerError("A repository checkout is required")
        assignment_id = assignment_id or uuid.uuid4().hex
        previous = self.store.get("a2a.outbound", assignment_id)
        if previous:
            if previous["peer"] != peer or previous["prompt"] != prompt or previous["assignment"]["paths"] != paths:
                raise PeerError("Assignment id was reused with different content")
            if previous["session"] != session_id:
                raise PeerError("Assignment belongs to another integrator session")
            assignment = Assignment.from_dict(previous["assignment"])
            if previous.get("task_id"):
                return {**await self.status(peer, previous["task_id"]), "assignment_id": assignment_id}
        else:
            git = self.agent.repos.git
            if await git("status", "--porcelain", cwd=checkout.path):
                raise PeerError("Commit or discard local changes before assigning work")
            base = await git("rev-parse", "HEAD", cwd=checkout.path)
            assignment = Assignment(
                id=assignment_id, repository=self.agent.repos.spec.clone_url, base=base,
                base_ref=f"refs/heads/agentic/base/{assignment_id}", paths=tuple(paths),
            )
            with A.store_transaction(self.store):
                for _, pending in self.store.list("a2a.outbound"):
                    if pending.get("integrated") or pending.get("remote", {}).get("state") in (
                        "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
                    ):
                        continue
                    old = Assignment.from_dict(pending["assignment"])
                    if repository(old.repository) == repository(assignment.repository) and any(
                        overlaps(a, b) for a in old.paths for b in assignment.paths
                    ):
                        raise PeerError("Paths are already reserved by assignment " + old.id)
                self.store.set("a2a.outbound", assignment_id, {
                    "peer": peer, "prompt": prompt, "session": session_id,
                    "assignment": {**asdict(assignment), "paths": paths},
                })
        try:
            await self.agent.repos.git(
                "push", self.agent.repos.spec.authenticated_url(),
                f"{assignment.base}:{assignment.base_ref}", cwd=checkout.path,
            )
            state = await self.submit(peer, prompt, request_id=assignment_id, assignment=assignment)
        except (PeerError, A.RepoError, httpx.HTTPError, A2AError, TimeoutError) as exc:
            raise PeerError(
                f"Assignment {assignment_id} outcome is uncertain; retry peer_assign with the same assignment_id: {exc}"
            ) from exc
        record = self.store.get("a2a.outbound", assignment_id)
        self.store.set("a2a.outbound", assignment_id, {**record, **state, "remote": state})
        return {**state, "assignment_id": assignment_id}

    async def integrate(self, assignment_id: str, session_id: str) -> dict[str, Any]:
        record = self.store.get("a2a.outbound", assignment_id)
        if not record or record["session"] != session_id:
            raise PeerError("No assignment owned by this integrator session")
        if record.get("integrated"):
            return record["integrated"]
        session = self.agent.sessions.get(session_id)
        checkout = self.agent.checkout(session)
        if checkout is None:
            raise PeerError("Integrator checkout no longer exists")
        assignment = Assignment.from_dict(record["assignment"])
        state = await self.status(record["peer"], record["task_id"])
        self.update_outbound(record["peer"], record["task_id"], state)
        if state["state"] != "TASK_STATE_COMPLETED" or not isinstance(state.get("handoff"), dict):
            raise PeerError("Assignment has not completed with a Git handoff: " + state["state"])
        handoff = state["handoff"]
        if (
            handoff.get("assignment_id") != assignment.id or handoff.get("base") != assignment.base
            or handoff.get("repository") != assignment.repository or handoff.get("paths") != list(assignment.paths)
            or not isinstance(handoff.get("sha"), str) or not SHA.fullmatch(handoff["sha"])
            or not isinstance(handoff.get("branch"), str)
        ):
            raise PeerError("Peer returned an inconsistent handoff")
        git = self.agent.repos.git
        async with self.agent.session_run(session), self.agent.repos.transaction(checkout.path):
            if await git("status", "--porcelain", cwd=checkout.path):
                raise PeerError("Commit or discard local changes before integrating")
            await git("check-ref-format", "refs/heads/" + handoff["branch"])
            await git(
                "fetch", "--no-tags", self.agent.repos.spec.authenticated_url(),
                "refs/heads/" + handoff["branch"], cwd=checkout.path,
            )
            sha = await git("rev-parse", "FETCH_HEAD", cwd=checkout.path)
            if sha != handoff["sha"]:
                raise PeerError("Peer result branch moved; refusing a different commit")
            await git("merge-base", "--is-ancestor", assignment.base, sha, cwd=checkout.path)
            await git("merge-base", "--is-ancestor", assignment.base, "HEAD", cwd=checkout.path)
            await self.validate_diff(checkout, assignment, sha)
            local = await git("diff", "--no-renames", "--name-only", "-z", assignment.base, "HEAD", cwd=checkout.path)
            if any(assignment.contains(p) for p in local.split("\0") if p):
                # A recovered integration is recognizable even if the process
                # stopped between the Git merge and recording its outcome.
                try:
                    await git("merge-base", "--is-ancestor", sha, "HEAD", cwd=checkout.path)
                except A.RepoError as exc:
                    raise PeerError("Integrator changed assigned paths since dispatch; resolve explicitly") from exc
            try:
                await git(*self.agent.repos.spec.identity(), "merge", "--no-ff", "--no-edit", sha, cwd=checkout.path)
            except A.RepoError as exc:
                if (checkout.path / ".git" / "MERGE_HEAD").exists():
                    await git("merge", "--abort", cwd=checkout.path)
                raise PeerError(f"Integration failed; local branch retained: {exc}") from exc
            result = {
                "assignment_id": assignment.id, "peer_sha": sha,
                "sha": await git("rev-parse", "HEAD", cwd=checkout.path),
                "branch": checkout.branch,
            }
            current = self.store.get("a2a.outbound", assignment_id)
            self.store.set("a2a.outbound", assignment_id, {**current, "integrated": result})
            return result

    def tools_for(self, result: A.RunResult, checkout: A.Checkout | None) -> list[Any]:
        from agents import function_tool

        async def invoke(operation: Awaitable[dict[str, Any]]) -> str:
            try:
                return json.dumps(await operation, ensure_ascii=True)
            except (ValueError, RuntimeError, OSError, httpx.HTTPError, A2AError, TimeoutError) as exc:
                log.warning("Peer tool failed: %s", exc)
                return f"Error: {exc}"

        async def relay(call_id: str, event: StreamResponse) -> None:
            text = ""
            if event.HasField("status_update"):
                text = "".join(p.text for p in event.status_update.status.message.parts)
            await self.agent.tool_reporter(result)(call_id, text)

        @function_tool
        async def peer_submit(peer: str, prompt: str, context_id: str = "", request_id: str = "") -> str:
            """Start a trusted peer's literal prompt; return its task handle. Reuse request_id only to recover the identical submission."""
            return await invoke(self.submit(peer, prompt, context_id=context_id, request_id=request_id))

        async def peer_prompt(ctx: Any, peer: str, prompt: str, context_id: str = "") -> str:
            """Ask a trusted peer and wait for its answer; timeout detaches, it does not cancel remote work."""
            async def call() -> dict[str, Any]:
                state = await self.submit(peer, prompt, context_id=context_id)
                if not state.get("task_id"):
                    return state
                try:
                    return await self.wait(peer, state["task_id"], lambda event: relay(ctx.tool_call_id, event))
                except TimeoutError as exc:
                    raise PeerError(f"Timed out waiting; task is still queryable: {json.dumps(state)}") from exc
            return await invoke(call())

        async def peer_status(ctx: Any, peer: str, task_id: str, wait: bool = False) -> str:
            """Get a peer task's authoritative state, answer and optional Git handoff."""
            async def call() -> dict[str, Any]:
                if wait:
                    try:
                        state = await self.wait(peer, task_id, lambda event: relay(ctx.tool_call_id, event))
                    except TimeoutError as exc:
                        raise PeerError(f"Wait timed out; query or cancel task {task_id} on {peer}") from exc
                else:
                    state = await self.status(peer, task_id)
                self.update_outbound(peer, task_id, state)
                return state
            return await invoke(call())

        @function_tool
        async def peer_cancel(peer: str, task_id: str) -> str:
            """Cancel remote work and wait for confirmation; the conversation is retained."""
            return await invoke(self.cancel(peer, task_id))

        from agents.tool_context import ToolContext

        peer_prompt.__annotations__["ctx"] = ToolContext
        peer_status.__annotations__["ctx"] = ToolContext
        built = [peer_submit, function_tool(peer_prompt), function_tool(peer_status), peer_cancel]
        if checkout is not None and result.session_id:
            session_id = result.session_id

            @function_tool
            async def peer_assign(peer: str, prompt: str, paths: list[str], assignment_id: str = "") -> str:
                """Delegate disjoint repository paths at a committed base; return immediately. Use the same assignment_id to recover an uncertain submission."""
                return await invoke(self.assign(peer, prompt, paths, session_id, assignment_id=assignment_id))

            @function_tool
            async def peer_integrate(assignment_id: str) -> str:
                """Verify the peer's exact commit and owned paths, then merge into this session once. Commit local work first; test the combined result afterward."""
                return await invoke(self.integrate(assignment_id, session_id))

            built.extend([peer_assign, peer_integrate])
        return built

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        import uvicorn

        if self.closed or self.server is not None:
            raise PeerError("A2A service is already running or closed")
        class Server(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self) -> Any:
                yield

        config = self.agent.config
        sock = socket.socket(socket.AF_INET6 if ":" in config.a2a_host else socket.AF_INET)
        try:
            try:
                sock.bind((config.a2a_host, config.a2a_port))
                sock.listen(128)
            except OSError as exc:
                raise PeerError(f"Cannot bind A2A at {config.a2a_host}:{config.a2a_port}: {exc}") from exc
            sock.setblocking(False)
            port = sock.getsockname()[1]
            if not config.a2a_url and config.a2a_port == 0:
                self.card.supported_interfaces[0].url = self.url.rsplit(":", 1)[0] + f":{port}"
            self.server = Server(uvicorn.Config(
                self.app, host=config.a2a_host, port=port, log_level="warning",
                access_log=False, lifespan="off", limit_concurrency=max(16, self.parallel * 8),
                timeout_graceful_shutdown=5,
                ssl_certfile=str(config.a2a_tls_cert) if config.a2a_tls_cert else None,
                ssl_keyfile=str(config.a2a_tls_key) if config.a2a_tls_key else None,
            ))
            task = asyncio.create_task(self.server.serve(sockets=[sock]))
            self.server_task = task
            try:
                async with asyncio.timeout(10):
                    while not self.server.started:
                        if task.done():
                            await task
                            raise PeerError("A2A listener stopped before readiness")
                        await asyncio.sleep(0.01)
                self.agent.sessions.start_sweeper()
                log.warning("A2A ready at %s", self.card.supported_interfaces[0].url)
                yield
            finally:
                self.server.should_exit = True
                await self.aclose()
                await task
        finally:
            sock.close()

    async def serve(self, work: Callable[[], Awaitable[Any]] | None = None) -> None:
        async with self.running():
            assert self.server_task is not None
            if work is None:
                await asyncio.shield(self.server_task)
                return
            local = asyncio.ensure_future(work())
            try:
                done, _ = await asyncio.wait((local, self.server_task), return_when=asyncio.FIRST_COMPLETED)
                if self.server_task in done:
                    await self.server_task
                    raise PeerError("A2A listener stopped while the application was running")
                await local
            finally:
                local.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await local

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.server is not None:
            self.server.should_exit = True
        try:
            active = list(self.executions.values())
            for task in active:
                task.cancel()
            if active:
                outcomes = await asyncio.gather(*active, return_exceptions=True)
                for outcome in outcomes:
                    if isinstance(outcome, Exception):
                        log.error("A2A shutdown failed: %s", outcome)
            if self.http is not None and self.owns_http:
                await self.http.aclose()
        finally:
            self.clients.clear()
            self.cards.clear()
            self.agent.sessions.listeners.remove(self.session_closed)
            self.lease.close()
