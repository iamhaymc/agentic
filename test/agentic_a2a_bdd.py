"""Offline A2A contracts; HTTP stays inside ASGI, inference is scripted."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

import app.agentic as A
from app.agentic_a2a import A2AService, Assignment, PeerError


TOKEN = "test-only-peer-token-not-a-real-secret-0123456789"


class ScriptedAgent(A.Agent):
    async def turn(self, model_input, config, result, **kwargs):
        self.seen = getattr(self, "seen", [])
        self.seen.append(model_input)
        if getattr(self, "gate", None) is not None:
            self.started.set()
            await self.gate.wait()
        if getattr(self, "failure", False):
            raise ValueError("scripted inference failure")
        result.output = A.prompt_of(model_input)
        return result


class A2ATest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.object(A.TokenCounter, "encodings", {"o200k_base": None})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.agent = ScriptedAgent(
            console=False, config_file=False, env=False, store=A.MemoryStore(),
            workspace_root=Path(self.tmp.name), a2a_token=TOKEN,
            context_window=8192, context_summary=False,
        )
        self.service = A2AService(self.agent)
        self.agent._a2a = self.service
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.service.app),
            base_url="http://127.0.0.1:8766",
            headers={"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"},
        )
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.agent.aclose)

    async def rpc(self, method, params):
        response = await self.client.post(
            "/", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn("error", body, body)
        return body["result"]

    async def send(self, text, *, context="", immediate=False, message_id=""):
        message = {
            "messageId": message_id or uuid.uuid4().hex,
            "role": "ROLE_USER", "parts": [{"text": text}],
        }
        if context:
            message["contextId"] = context
        return await self.rpc(
            "SendMessage",
            {"message": message, "configuration": {"returnImmediately": immediate}},
        )

    async def test_card_advertises_only_implemented_capabilities(self):
        response = await self.client.get("/.well-known/agent-card.json")
        self.assertEqual(response.status_code, 200)
        card = response.json()
        self.assertTrue(card["capabilities"]["streaming"])
        self.assertFalse(card["capabilities"].get("pushNotifications", False))
        self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")
        self.assertIn("bearer", card["securitySchemes"])

    async def test_every_route_requires_the_token(self):
        for path, method in (("/.well-known/agent-card.json", "GET"), ("/", "POST")):
            response = await self.client.request(method, path, headers={"Authorization": ""})
            self.assertEqual(response.status_code, 401)

    async def test_literal_text_never_reads_a_host_file(self):
        private = Path(self.tmp.name) / "private.txt"
        private.write_text("do not disclose this", encoding="utf-8")
        result = await self.send(str(private))
        task = result["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], str(private))
        self.assertNotIn("do not disclose", json.dumps(result))

    async def test_context_reuses_history_but_is_not_a_session_identifier(self):
        first = (await self.send("first"))["task"]
        second = (await self.send("second", context=first["contextId"]))["task"]
        self.assertEqual(first["contextId"], second["contextId"])
        session = self.service.session_for(first["contextId"])
        self.assertNotEqual(session.id, first["contextId"])
        self.assertEqual(len(self.agent.sessions.list()), 1)
        self.assertIn("first", str(self.agent.seen[-1]))

    async def test_unknown_context_is_not_silently_replaced(self):
        response = await self.client.post("/", json={
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"message": {
                "messageId": uuid.uuid4().hex, "role": "ROLE_USER",
                "contextId": "unknown", "parts": [{"text": "hello"}],
            }},
        })
        self.assertIn("error", response.json())
        self.assertEqual(self.agent.sessions.list(), [])

    async def test_failed_run_is_not_reported_as_completed(self):
        self.agent.failure = True
        task = (await self.send("fail"))["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_FAILED")
        self.assertIn("scripted inference failure", json.dumps(task))

    async def test_duplicate_message_does_not_repeat_work(self):
        key = uuid.uuid4().hex
        first = (await self.send("once", message_id=key))["task"]
        second = (await self.send("once", message_id=key))["task"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.agent.seen), 1)

    async def test_cancel_stops_the_run_without_destroying_the_context(self):
        self.agent.gate = asyncio.Event()
        self.agent.started = asyncio.Event()
        task = (await self.send("wait", immediate=True))["task"]
        await asyncio.wait_for(self.agent.started.wait(), 2)
        canceled = await self.rpc("CancelTask", {"id": task["id"]})
        self.assertEqual(canceled["status"]["state"], "TASK_STATE_CANCELED")
        self.assertIsNotNone(self.service.session_for(task["contextId"]))
        self.assertFalse(self.agent.live)

    async def test_admission_is_bounded_instead_of_building_a_wait_queue(self):
        self.service.parallel = 1
        self.agent.gate = asyncio.Event()
        self.agent.started = asyncio.Event()
        first = (await self.send("wait", immediate=True))["task"]
        await asyncio.wait_for(self.agent.started.wait(), 2)
        second = (await self.send("reject"))["task"]
        self.assertEqual(second["status"]["state"], "TASK_STATE_REJECTED")
        await self.rpc("CancelTask", {"id": first["id"]})

    async def test_common_run_boundary_rejects_an_overlapping_local_run(self):
        self.agent.gate = asyncio.Event()
        self.agent.started = asyncio.Event()
        task = (await self.send("wait", immediate=True))["task"]
        await asyncio.wait_for(self.agent.started.wait(), 2)
        session = self.service.session_for(task["contextId"])
        with self.assertRaisesRegex(RuntimeError, "already active"):
            await self.agent.run(A.PASSTHROUGH_TEMPLATE, session=session, input_text="local")
        await self.rpc("CancelTask", {"id": task["id"]})

    async def test_unlisted_peer_is_rejected_without_fetching_a_card(self):
        with self.assertRaisesRegex(PeerError, "allowlist"):
            await self.service.submit("http://192.168.1.99:8766", "hello")

    def test_assignment_rejects_ambiguous_or_escaping_paths(self):
        for paths in ([], ["../secret"], ["/absolute"], [".git/config"], ["a\\b"], ["a/../b"]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                Assignment(
                    id=uuid.uuid4().hex, repository="https://example.test/repo.git",
                    base="a" * 40, base_ref="refs/heads/main", paths=tuple(paths),
                )

    async def test_inbound_runs_cannot_delegate_to_another_peer_or_ask_a_human(self):
        self.agent.config = self.agent.config.merge(
            ask_enabled=True, outside_read=True, outside_write=True,
        )
        captured = []
        original = self.agent.turn

        async def inspect_run(model_input, config, result, **kwargs):
            captured.append(config)
            return await original(model_input, config, result, **kwargs)

        self.agent.turn = inspect_run
        await self.send("hello")
        self.assertFalse(captured[0].ask_enabled)
        self.assertFalse(captured[0].outside_read)
        self.assertFalse(captured[0].outside_write)
        self.assertTrue(captured[0].extras["a2a_inbound"])

    async def test_streaming_uses_the_same_validation_and_deduplication(self):
        key = uuid.uuid4().hex
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "SendStreamingMessage",
            "params": {"message": {
                "messageId": key, "role": "ROLE_USER", "parts": [{"text": "stream"}],
            }},
        }
        first = await self.client.post("/", json=request)
        second = await self.client.post("/", json=request)
        self.assertEqual(first.status_code, 200)
        self.assertIn("text/event-stream", first.headers["content-type"])
        self.assertIn("TASK_STATE_COMPLETED", first.text)
        self.assertIn("TASK_STATE_COMPLETED", second.text)
        self.assertEqual(len(self.agent.seen), 1)

    async def test_reusing_a_message_id_with_different_content_is_rejected(self):
        key = uuid.uuid4().hex
        await self.send("one", message_id=key)
        response = await self.client.post("/", json={
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
                "messageId": key, "role": "ROLE_USER", "parts": [{"text": "two"}],
            }},
        })
        self.assertIn("error", response.json())
        self.assertEqual(len(self.agent.seen), 1)

    async def test_request_size_is_bounded_before_protocol_parsing(self):
        response = await self.client.post("/", content=b"x" * (256 * 1024 + 1))
        self.assertEqual(response.status_code, 413)

    async def test_busy_context_is_rejected_and_original_task_can_be_canceled(self):
        first = (await self.send("establish"))["task"]
        self.agent.gate = asyncio.Event()
        self.agent.started = asyncio.Event()
        running = (await self.send("wait", context=first["contextId"], immediate=True))["task"]
        await asyncio.wait_for(self.agent.started.wait(), 2)
        busy = (await self.send("busy", context=first["contextId"]))["task"]
        self.assertEqual(busy["status"]["state"], "TASK_STATE_REJECTED")
        await self.rpc("CancelTask", {"id": running["id"]})

    async def test_total_deadline_stops_a_run_even_if_its_model_is_waiting(self):
        self.service.timeout = 0.02
        self.agent.gate = asyncio.Event()
        self.agent.started = asyncio.Event()
        task = (await self.send("wait"))["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_FAILED")
        self.assertFalse(self.agent.live)

    async def test_task_and_context_survive_a_restart_without_replaying_work(self):
        state = Path(self.tmp.name) / "durable"

        def durable_agent():
            return ScriptedAgent(
                console=False, config_file=False, env=False, state_dir=state,
                mode="a2a", a2a_enabled=True, a2a_token=TOKEN,
                context_window=8192, context_summary=False,
            )

        first = durable_agent()
        key = uuid.uuid4().hex
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"message": {
                "messageId": key, "role": "ROLE_USER", "parts": [{"text": "durable"}],
            }},
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first.a2a.app), base_url="http://127.0.0.1:8766",
            headers={"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"},
        ) as client:
            response = (await client.post("/", json=request)).json()
        await first.aclose()
        second = durable_agent()
        self.addAsyncCleanup(second.aclose)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second.a2a.app), base_url="http://127.0.0.1:8766",
            headers={"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"},
        ) as client:
            repeated = (await client.post("/", json=request)).json()
        self.assertEqual(response["result"]["task"]["id"], repeated["result"]["task"]["id"])
        self.assertEqual(repeated["result"]["task"]["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertFalse(hasattr(second, "seen"))
        self.assertIsNotNone(second.a2a.session_for(response["result"]["task"]["contextId"]))

    async def test_remote_plaintext_is_an_explicit_opt_in(self):
        self.agent.config = self.agent.config.merge(a2a_host="0.0.0.0", a2a_url="http://192.168.1.5:8766")
        with self.assertRaisesRegex(PeerError, "TLS"):
            A2AService(self.agent)

    async def test_followup_to_a_running_task_cannot_replace_its_execution(self):
        self.agent.gate = asyncio.Event()
        self.agent.started = asyncio.Event()
        first = (await self.send("wait", immediate=True))["task"]
        await asyncio.wait_for(self.agent.started.wait(), 2)
        response = await self.client.post("/", json={
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
                "messageId": uuid.uuid4().hex, "role": "ROLE_USER", "taskId": first["id"],
                "contextId": first["contextId"], "parts": [{"text": "replace"}],
            }},
        })
        self.assertIn("error", response.json())
        self.assertEqual(len(self.service.executions), 1)
        canceled = await self.rpc("CancelTask", {"id": first["id"]})
        self.assertEqual(canceled["status"]["state"], "TASK_STATE_CANCELED")

    async def test_state_lease_prevents_two_peer_services_adopting_the_same_database(self):
        from app.agentic_a2a import StateLease

        path = Path(self.tmp.name) / "peer.lock"
        first = StateLease(path)
        try:
            with self.assertRaisesRegex(PeerError, "state is already in use"):
                StateLease(path)
        finally:
            first.close()
        second = StateLease(path)
        second.close()

    async def test_peer_configuration_resolves_from_group_and_environment(self):
        config = A.AgentConfig().with_data({
            "a2a": {"enabled": True, "port": 8767, "parallel": 3, "timeout": 42.5},
        }).with_env({"AGENTIC_A2A_TOKEN": TOKEN})
        self.assertTrue(config.a2a_enabled)
        self.assertEqual(config.a2a_port, 8767)
        self.assertEqual(config.a2a_parallel, 3)
        self.assertEqual(config.a2a_timeout, 42.5)
        self.assertNotEqual(config.to_dict()["a2a_token"], TOKEN)

    async def test_cli_exposes_peer_modes_without_changing_the_default_repl(self):
        args = A.parse_args([
            "--a2a", "--a2a-port", "8767", "--peer", "https://peer.home:8768",
            "--repo", "owner/repository",
        ])
        self.assertEqual(A.mode_of(args), "a2a")
        self.assertEqual(args.a2a_port, 8767)
        self.assertEqual(args.peer, ["https://peer.home:8768"])
        self.assertEqual(args.repo, "owner/repository")
        self.assertEqual(A.mode_of(A.parse_args(["--serve", "--a2a"])), "web")
        self.assertEqual(A.mode_of(A.parse_args([])), "repl")

    def test_ephemeral_port_survives_origin_normalization(self):
        from app.agentic_a2a import origin

        self.assertEqual(origin("https://127.0.0.1:0"), "https://127.0.0.1:0")
        self.assertEqual(origin("http://[::1]:0"), "http://[::1]:0")

    async def test_history_length_is_respected_for_blocking_and_duplicate_responses(self):
        key = uuid.uuid4().hex
        params = {
            "message": {
                "messageId": key, "role": "ROLE_USER", "parts": [{"text": "history"}],
            },
            "configuration": {"historyLength": 0},
        }
        for _ in range(2):
            task = (await self.rpc("SendMessage", params))["task"]
            self.assertFalse(task.get("history"))


class Router(httpx.AsyncBaseTransport):
    def __init__(self):
        self.apps = {}

    async def handle_async_request(self, request):
        service = self.apps[str(request.url).split("/", 3)[2]]
        return await httpx.ASGITransport(app=service.app).handle_async_request(request)


class PeerClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.router = Router()
        self.http = httpx.AsyncClient(
            transport=self.router, headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.addAsyncCleanup(self.http.aclose)
        self.agents = []
        self.services = []
        for index, port in enumerate((8766, 8767)):
            agent = ScriptedAgent(
                console=False, config_file=False, env=False, store=A.MemoryStore(),
                workspace_root=Path(self.tmp.name) / str(index), a2a_token=TOKEN,
                a2a_port=port, a2a_peers=f"http://127.0.0.1:{8767 if index == 0 else 8766}",
                context_window=8192, context_summary=False,
            )
            service = A2AService(agent, http_client=self.http)
            agent._a2a = service
            self.router.apps[f"127.0.0.1:{port}"] = service
            self.agents.append(agent)
            self.services.append(service)
            self.addAsyncCleanup(agent.aclose)
        self.patch = patch.object(A.TokenCounter, "encodings", {"o200k_base": None})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_two_peers_submit_wait_and_continue_the_same_conversation(self):
        leader = self.services[0]
        state = await leader.submit("http://127.0.0.1:8767", "first")
        answer = await leader.wait(state["peer"], state["task_id"])
        self.assertEqual(answer["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(answer["output"], "first")
        again = await leader.submit(state["peer"], "second", context_id=state["context_id"])
        await leader.wait(again["peer"], again["task_id"])
        self.assertIn("first", str(self.agents[1].seen[-1]))

    async def test_card_cannot_send_credentials_to_another_origin(self):
        self.services[1].card.supported_interfaces[0].url = "http://127.0.0.1:9999/"
        with self.assertRaisesRegex(PeerError, "unapproved origin"):
            await self.services[0].submit("http://127.0.0.1:8767", "hello")

    async def test_child_tool_tasks_share_their_runs_logical_ownership(self):
        session = self.agents[0].sessions.create()
        async with self.agents[0].session_run(session):
            async def tool():
                async with self.agents[0].session_run(session):
                    return True
            self.assertTrue(await asyncio.create_task(tool()))

    async def test_model_facing_tools_have_valid_schemas_and_work_via_sdk_invocation(self):
        from agents.tool_context import ToolContext

        leader = self.agents[0]
        session = leader.sessions.create()
        result = A.RunResult(session_id=session.id)
        tools = {tool.name: tool for tool in self.services[0].tools_for(result, None)}
        self.assertEqual(set(tools), {"peer_submit", "peer_prompt", "peer_status", "peer_cancel"})
        tool = tools["peer_prompt"]
        self.assertNotIn("ctx", tool.params_json_schema["properties"])
        payload = json.dumps({"peer": "http://127.0.0.1:8767", "prompt": "sdk", "context_id": ""})
        context = ToolContext(context=None, tool_name=tool.name, tool_call_id="1", tool_arguments=payload)
        reply = json.loads(await tool.on_invoke_tool(context, payload))
        self.assertEqual(reply["state"], "TASK_STATE_COMPLETED", reply)
        self.assertEqual(reply["output"], "sdk")


class GitHandoffTest(PeerClientTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.git = A.RepoManager(A.RepoSpec(author_name="test", author_email="test@example.invalid"))
        self.bare = Path(self.tmp.name) / "origin.git"
        self.seed = Path(self.tmp.name) / "seed"
        await self.git.git("init", "--bare", str(self.bare))
        await self.git.git("init", "--initial-branch=main", str(self.seed))
        (self.seed / "module.txt").write_text("base\n", encoding="utf-8")
        (self.seed / "shared.txt").write_text("shared\n", encoding="utf-8")
        await self.git.git("add", ".", cwd=self.seed)
        await self.git.git(*self.git.spec.identity(), "commit", "-m", "seed", cwd=self.seed)
        await self.git.git("push", self.bare.as_uri(), "main", cwd=self.seed)
        await self.git.git("--git-dir", str(self.bare), "symbolic-ref", "HEAD", "refs/heads/main")
        for agent in self.agents:
            agent.config = agent.config.merge(repo_url=self.bare.as_uri(), repo_depth=1)
            agent.repos.spec = A.RepoSpec.from_config(agent.config)
        self.session = self.agents[0].sessions.create()
        self.checkout = await self.agents[0].prepare_session(self.session)
        self.edits = {"module.txt": "peer work\n"}
        original = self.agents[1].turn

        async def edit(model_input, config, result, **kwargs):
            checkout = self.agents[1].checkout(self.agents[1].sessions.get(result.session_id))
            for path, text in self.edits.items():
                destination = checkout.path / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text, encoding="utf-8")
            return await original(model_input, config, result, **kwargs)

        self.agents[1].turn = edit

    async def assign(self):
        state = await self.services[0].assign(
            "http://127.0.0.1:8767", "Implement module", ["module.txt"], self.session.id,
        )
        answer = await self.services[0].wait(state["peer"], state["task_id"])
        return state, answer

    async def test_exact_commit_is_verified_and_integrated_once(self):
        state, answer = await self.assign()
        self.assertEqual(answer["state"], "TASK_STATE_COMPLETED", answer)
        self.assertEqual((self.checkout.path / "module.txt").read_text(), "base\n")
        async with self.agents[0].session_run(self.session):
            merged = await asyncio.create_task(self.services[0].integrate(state["assignment_id"], self.session.id))
        self.assertEqual((self.checkout.path / "module.txt").read_text(), "peer work\n")
        self.assertEqual(await self.services[0].integrate(state["assignment_id"], self.session.id), merged)
        self.assertEqual(merged["peer_sha"], answer["handoff"]["sha"])
        self.assertEqual((self.seed / "module.txt").read_text(), "base\n")

    async def test_out_of_scope_edits_are_failed_and_never_published(self):
        self.edits["shared.txt"] = "unauthorized\n"
        state, answer = await self.assign()
        self.assertEqual(answer["state"], "TASK_STATE_FAILED")
        self.assertIn("Out-of-assignment", answer["message"])
        with self.assertRaisesRegex(PeerError, "has not completed"):
            await self.services[0].integrate(state["assignment_id"], self.session.id)
        self.assertEqual((self.checkout.path / "shared.txt").read_text(), "shared\n")

    async def test_overlapping_assignments_are_rejected_until_integrated(self):
        await self.assign()
        with self.assertRaisesRegex(PeerError, "reserved"):
            await self.assign()

    async def test_local_changes_to_assigned_paths_require_explicit_resolution(self):
        state, _ = await self.assign()
        (self.checkout.path / "module.txt").write_text("local work\n", encoding="utf-8")
        await self.checkout.commit("local")
        with self.assertRaisesRegex(PeerError, "changed assigned paths"):
            await self.services[0].integrate(state["assignment_id"], self.session.id)
        self.assertEqual((self.checkout.path / "module.txt").read_text(), "local work\n")

    async def test_duplicate_assignment_does_not_repeat_a_peer_run(self):
        state, _ = await self.assign()
        again = await self.services[0].assign(
            state["peer"], "Implement module", ["module.txt"], self.session.id,
            assignment_id=state["assignment_id"],
        )
        self.assertEqual(again["task_id"], state["task_id"])
        self.assertEqual(len(self.agents[1].seen), 1)

    async def test_moving_a_published_result_branch_is_detected(self):
        state, answer = await self.assign()
        peer_session = self.services[1].session_for(answer["context_id"])
        checkout = self.agents[1].checkout(peer_session)
        (checkout.path / "module.txt").write_text("later work\n", encoding="utf-8")
        await checkout.commit("later")
        await checkout.push()
        with self.assertRaisesRegex(PeerError, "branch moved"):
            await self.services[0].integrate(state["assignment_id"], self.session.id)


if __name__ == "__main__":
    unittest.main()
