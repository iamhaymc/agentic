"""Explicitly selected two-process A2A/Git test; no live inference or forge."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import ipaddress
import json
import logging
import ssl
import socket
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

import app.agentic as A

TOKEN = "e2e-only-token-not-a-real-secret-0123456789012345"


async def invoke(tool, values):
    from agents.tool_context import ToolContext

    payload = json.dumps(values)
    context = ToolContext(
        context=None, tool_name=tool.name, tool_call_id=tool.name, tool_arguments=payload,
    )
    reply = await asyncio.create_task(tool.on_invoke_tool(context, payload))
    if reply.startswith("Error:"):
        raise ValueError(reply)
    return json.loads(reply)


class ScriptedPeer(A.Agent):
    async def turn(self, model_input, config, result, **kwargs):
        checkout = self.checkout(self.sessions.get(result.session_id))
        if config.extras.get("a2a_inbound"):
            (checkout.path / "module.txt").write_text("remote implementation\n", encoding="utf-8")
            result.output = "Implemented module.txt; deterministic fixture, no live model."
            return result
        tools = {
            tool.name: tool
            for tool in await self.build_run_tools(
                self.build_workspace(kwargs["workspace"], config), config, result, checkout,
            )
        }
        state = await invoke(tools["peer_assign"], {
            "peer": self.config.a2a_peers, "prompt": "Implement module",
            "paths": ["module.txt"], "assignment_id": "",
        })
        (checkout.path / "shared.txt").write_text("local implementation\n", encoding="utf-8")
        await checkout.commit("Local parallel work")
        answer = await invoke(tools["peer_status"], {
            "peer": state["peer"], "task_id": state["task_id"], "wait": True,
        })
        if answer["state"] != "TASK_STATE_COMPLETED":
            raise ValueError(json.dumps(answer))
        merge = await invoke(tools["peer_integrate"], {"assignment_id": state["assignment_id"]})
        again = await invoke(tools["peer_integrate"], {"assignment_id": state["assignment_id"]})
        if merge != again:
            raise ValueError("Integration was not idempotent")
        result.output = json.dumps({
            "merge": merge, "workspace": str(checkout.path),
            "module": (checkout.path / "module.txt").read_text(),
            "shared": (checkout.path / "shared.txt").read_text(),
            "parents": await self.repos.git("rev-list", "--parents", "-n", "1", "HEAD", cwd=checkout.path),
        })
        return result


async def worker(args):
    logging.basicConfig(
        filename=str(Path(args.state).with_suffix(".log")), level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
    )
    A.TokenCounter.encodings["o200k_base"] = None
    agent = ScriptedPeer(
        console=False, config_file=False, env=False,
        mode="web" if args.combined else "a2a", a2a_enabled=True,
        a2a_token=TOKEN, a2a_port=0, a2a_peers=args.peer, a2a_timeout=30,
        state_dir=Path(args.state), repo_url=args.repo, context_window=8192,
        context_summary=False,
        port=args.web_port,
    )
    try:
        if args.combined:
            serving = asyncio.create_task(agent.serve())
            try:
                async with asyncio.timeout(10):
                    while agent._a2a is None or agent.a2a.server is None or not agent.a2a.server.started:
                        if serving.done():
                            await serving
                        await asyncio.sleep(0.01)
                Path(args.ready).write_text(agent.a2a.card.supported_interfaces[0].url, encoding="utf-8")
                await asyncio.to_thread(sys.stdin.readline)
            finally:
                serving.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serving
            return
        async with agent.a2a.running():
            Path(args.ready).write_text(agent.a2a.card.supported_interfaces[0].url, encoding="utf-8")
            if args.peer:
                result = await agent.run(A.PASSTHROUGH_TEMPLATE, input_text="Coordinate")
                Path(args.result).write_text(json.dumps({
                    "ok": result.ok, "error": result.error, "output": result.output,
                }), encoding="utf-8")
            await asyncio.to_thread(sys.stdin.readline)
    finally:
        await agent.aclose()


class TwoProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_processes_develop_and_merge_without_touching_the_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = A.RepoManager(A.RepoSpec(author_name="test", author_email="test@example.invalid"))
            bare, seed = root / "origin.git", root / "seed"
            await git.git("init", "--bare", str(bare))
            await git.git("init", "--initial-branch=main", str(seed))
            for name in ("module.txt", "shared.txt"):
                (seed / name).write_text("base\n", encoding="utf-8")
            await git.git("add", ".", cwd=seed)
            await git.git(*git.spec.identity(), "commit", "-m", "seed", cwd=seed)
            await git.git("push", bare.as_uri(), "main", cwd=seed)
            await git.git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
            children = []

            async def launch(name, peer=""):
                ready, result = root / f"{name}.ready", root / f"{name}.result"
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "test.agentic_a2a_e2e", "--worker",
                    "--repo", bare.as_uri(), "--state", str(root / name),
                    "--ready", str(ready), "--result", str(result), "--peer", peer,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(Path(__file__).resolve().parents[1]),
                )
                children.append(process)
                async with asyncio.timeout(20):
                    while not ready.exists():
                        if process.returncode is not None:
                            _, error = await process.communicate()
                            self.fail(error.decode())
                        await asyncio.sleep(0.02)
                url = ready.read_text(encoding="utf-8")
                async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                    response = await client.get(
                        url + "/.well-known/agent-card.json",
                        headers={"Authorization": f"Bearer {TOKEN}"},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                return url, result

            try:
                peer, _ = await launch("worker")
                _, result = await launch("leader", peer)
                async with asyncio.timeout(45):
                    while not result.exists():
                        await asyncio.sleep(0.05)
                completed = json.loads(result.read_text(encoding="utf-8"))
                self.assertTrue(completed["ok"], {
                    **completed,
                    "worker_log": (root / "worker.log").read_text(encoding="utf-8"),
                    "leader_log": (root / "leader.log").read_text(encoding="utf-8"),
                } if not completed["ok"] else completed)
                output = json.loads(completed["output"])
                self.assertEqual(output["module"], "remote implementation\n")
                self.assertEqual(output["shared"], "local implementation\n")
                self.assertEqual(len(output["parents"].split()), 3)
                self.assertEqual((seed / "module.txt").read_text(), "base\n")
                self.assertEqual((seed / "shared.txt").read_text(), "base\n")
                self.assertNotEqual(output["merge"]["sha"], output["merge"]["peer_sha"])
            finally:
                for process in reversed(children):
                    if process.returncode is None:
                        try:
                            _, error = await asyncio.wait_for(process.communicate(b"stop\n"), 10)
                        except TimeoutError:
                            process.kill()
                            _, error = await process.communicate()
                        self.assertEqual(process.returncode, 0, error.decode())

    async def test_https_peer_authenticates_and_verifies_its_certificate(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        from unittest.mock import patch

        counter = patch.object(A.TokenCounter, "encodings", {"o200k_base": None})
        counter.start()
        self.addCleanup(counter.stop)

        class Echo(A.Agent):
            async def turn(self, model_input, config, result, **kwargs):
                result.output = A.prompt_of(model_input)
                return result

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = ec.generate_private_key(ec.SECP256R1())
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.datetime.now(datetime.timezone.utc)
            certificate = (
                x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.SubjectAlternativeName([
                    x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]), critical=False)
                .sign(key, hashes.SHA256())
            )
            cert, private = root / "test.pem", root / "test-key.pem"
            cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            private.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            worker = Echo(
                console=False, env=False, config_file=False, store=A.MemoryStore(),
                workspace_root=root / "worker", a2a_token=TOKEN, a2a_port=0,
                a2a_tls_cert=cert, a2a_tls_key=private, context_window=8192,
                context_summary=False,
            )
            leader = None
            try:
                async with worker.a2a.running():
                    url = worker.a2a.card.supported_interfaces[0].url
                    leader = Echo(
                        console=False, env=False, config_file=False, store=A.MemoryStore(),
                        workspace_root=root / "leader", a2a_token=TOKEN,
                        a2a_peers=url, a2a_ca_file=cert, context_window=8192,
                        context_summary=False,
                    )
                    submitted = await leader.a2a.submit(url, "encrypted peer task")
                    answer = await leader.a2a.wait(url, submitted["task_id"])
                    self.assertEqual(answer["state"], "TASK_STATE_COMPLETED")
                    self.assertEqual(answer["output"], "encrypted peer task")
                    async with httpx.AsyncClient(trust_env=False, timeout=3) as untrusted:
                        with self.assertRaises(httpx.ConnectError):
                            await untrusted.get(url + "/.well-known/agent-card.json")
                    async with httpx.AsyncClient(
                        verify=ssl.create_default_context(cafile=str(cert)), trust_env=False,
                    ) as client:
                        denied = await client.get(url + "/.well-known/agent-card.json")
                        self.assertEqual(denied.status_code, 401)
                    await leader.aclose()
                    leader = None
            finally:
                if leader:
                    await leader.aclose()
                await worker.aclose()

    async def test_web_and_a2a_listeners_start_and_stop_together(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as reserve:
            root = Path(tmp)
            reserve.bind(("127.0.0.1", 0))
            web_port = reserve.getsockname()[1]
            reserve.close()
            ready = root / "combined.ready"
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "test.agentic_a2a_e2e", "--worker", "--combined",
                "--repo", "", "--state", str(root / "state"), "--ready", str(ready),
                "--result", str(root / "unused.result"), "--web-port", str(web_port),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(15):
                    while not ready.exists():
                        if child.returncode is not None:
                            _, error = await child.communicate()
                            self.fail(error.decode())
                        await asyncio.sleep(0.02)
                async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                    async with asyncio.timeout(5):
                        while True:
                            try:
                                health = await client.get(f"http://127.0.0.1:{web_port}/api/health")
                                break
                            except httpx.ConnectError:
                                await asyncio.sleep(0.02)
                    self.assertTrue(health.json()["ok"])
                    card = await client.get(
                        ready.read_text(encoding="utf-8") + "/.well-known/agent-card.json",
                        headers={"Authorization": f"Bearer {TOKEN}"},
                    )
                    self.assertEqual(card.status_code, 200)
            finally:
                if child.returncode is None:
                    try:
                        _, error = await asyncio.wait_for(child.communicate(b"stop\n"), 10)
                    except TimeoutError:
                        child.kill()
                        _, error = await child.communicate()
                    self.assertEqual(child.returncode, 0, error.decode())

    async def test_cli_bind_conflict_reports_one_error_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            script = (
                "from app.agentic import Agent, MemoryStore; "
                f"raise SystemExit(Agent.cli(argv=['--a2a','--a2a-port','{port}'], "
                f"a2a_token={TOKEN!r}, state_dir={tmp!r}, store=MemoryStore(), "
                "config_file=False, env_file=False, env=False, console=False))"
            )
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, error = await asyncio.wait_for(child.communicate(), 10)
            except TimeoutError:
                child.kill()
                await child.communicate()
                raise
            self.assertEqual(child.returncode, 2, error.decode())
            self.assertIn("Cannot bind A2A", error.decode())
            self.assertNotIn("Traceback", error.decode())


if __name__ == "__main__":
    if "--worker" in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument("--worker", action="store_true")
        parser.add_argument("--repo", required=True)
        parser.add_argument("--state", required=True)
        parser.add_argument("--ready", required=True)
        parser.add_argument("--result", required=True)
        parser.add_argument("--peer", default="")
        parser.add_argument("--combined", action="store_true")
        parser.add_argument("--web-port", type=int, default=8765)
        asyncio.run(worker(parser.parse_args()))
    else:
        unittest.main()
