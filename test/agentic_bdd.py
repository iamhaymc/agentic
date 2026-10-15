"""Unit tests for the agent harness.

Run with::

    python -m unittest test -v
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
import unittest.mock
from pathlib import Path

import app.agentic as A

_find_env_file = A.find_env_file
_probe_window = A.ModelPool.probe_window
_probe_ctx = A.ModelPool.probe_ctx


def setUpModule():
    # Keep the developer's own .env out of the tests: a search without explicit
    # directories finds nothing, while explicit lookups still work.
    patcher = unittest.mock.patch.object(
        A,
        "find_env_file",
        lambda directories=None: None if directories is None else _find_env_file(directories),
    )
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    # Nothing reaches the network: the tokenizer estimates (its vocabulary is
    # a download) and no provider is asked for its context window.
    for patch in (
        unittest.mock.patch.object(A.TokenCounter, "encodings", {"o200k_base": None}),
        unittest.mock.patch.object(A.ModelPool, "probe_window", _no_window),
        unittest.mock.patch.object(A.ModelPool, "probe_ctx", _no_window),
    ):
        patch.start()
        unittest.addModuleCleanup(patch.stop)


async def _no_window(self, spec):
    return 0


def run(coro):
    return asyncio.run(coro)


def python_command(code: str) -> str:
    """A shell command running `code` with this interpreter, on any platform."""
    return f'"{sys.executable}" -c "{code}"'


def sleeper_command(pid_file: Path, seconds: int = 20) -> str:
    """A command whose interpreter writes its pid and then outlives any test."""
    return python_command(
        "import os, pathlib, time; "
        f"pathlib.Path('{pid_file.as_posix()}').write_text(str(os.getpid())); "
        f"time.sleep({seconds})"
    )


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return f" {pid} " in listed
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ConfigTest(unittest.TestCase):
    def test_merge_keeps_known_fields_and_collects_extras(self):
        config = A.AgentConfig().merge(inference_llm="m", genre="noir", ignored=None)
        self.assertEqual(config.inference_llm, "m")
        self.assertEqual(config.extras["genre"], "noir")
        self.assertNotIn("ignored", config.extras)

    def test_extras_are_readable_as_attributes(self):
        config = A.AgentConfig(extras={"genre": "noir"})
        self.assertEqual(config.genre, "noir")
        with self.assertRaises(AttributeError):
            _ = config.missing

    def test_env_layer_coerces_types(self):
        env = {"AGENTIC_PORT": "9100", "AGENTIC_QUIET": "true", "AGENTIC_INFERENCE_LLM": "env-model"}
        config = A.AgentConfig().with_env(env)
        self.assertEqual(config.port, 9100)
        self.assertTrue(config.quiet)
        self.assertEqual(config.inference_llm, "env-model")

    def test_env_layer_coerces_paths(self):
        config = A.AgentConfig().with_env({"AGENTIC_THEME": "/tmp/theme.json"})
        self.assertEqual(config.theme, Path("/tmp/theme.json"))

    def test_to_dict_redacts_secrets(self):
        config = A.AgentConfig(inference_key="sk-abcdefghijklmnop")
        self.assertEqual(config.to_dict()["inference_key"], "sk-abcde...mnop")
        self.assertEqual(config.to_dict(redact=False)["inference_key"], "sk-abcdefghijklmnop")

    def test_to_dict_redacts_role_secrets(self):
        config = A.AgentConfig(vision_api_key="sk-abcdefghijklmnop")
        self.assertEqual(config.to_dict()["vision_api_key"], "sk-abcde...mnop")

    def test_mask_secret(self):
        self.assertEqual(A.mask_secret("short"), "*****")
        self.assertEqual(A.mask_secret(""), "")

    def test_defaults_fill_only_unset_fields(self):
        self.assertIsNone(A.AgentConfig().outside_read)
        self.assertTrue(A.AgentConfig().with_defaults(outside_read=True).outside_read)
        config = A.AgentConfig(outside_read=False).with_defaults(outside_read=True)
        self.assertFalse(config.outside_read)

    def test_env_layer_sets_the_outside_flags(self):
        config = A.AgentConfig().with_env(
            {"AGENTIC_OUTSIDE_READ": "false", "AGENTIC_OUTSIDE_WRITE": "true"}
        )
        self.assertIs(config.outside_read, False)
        self.assertIs(config.outside_write, True)


class ModelSpecTest(unittest.TestCase):
    def test_leader_spec_uses_the_primary_settings(self):
        config = A.AgentConfig(inference_llm="m", inference_url="http://url", inference_key="k")
        spec = config.model_spec()
        self.assertEqual((spec.role, spec.name, spec.api_url, spec.api_key),
                         ("leader", "m", "http://url", "k"))

    def test_vision_is_unset_by_default(self):
        self.assertIsNone(A.AgentConfig().model_spec(A.VISION_ROLE))

    def test_vision_inherits_the_leader_endpoint(self):
        config = A.AgentConfig(inference_url="http://url", inference_key="k", inference_vlm="v")
        spec = config.model_spec(A.VISION_ROLE)
        self.assertEqual((spec.name, spec.api_url, spec.api_key), ("v", "http://url", "k"))

    def test_vision_may_use_its_own_endpoint(self):
        config = A.AgentConfig(
            inference_vlm="v", vision_api_url="http://other", vision_api_key="k2"
        )
        spec = config.model_spec(A.VISION_ROLE)
        self.assertEqual((spec.api_url, spec.api_key), ("http://other", "k2"))

    def test_vision_model_appears_in_the_banner(self):
        keys = [k for k, _ in A.AgentConfig(inference_vlm="v").banner_items()]
        self.assertIn("Vision Model", keys)
        self.assertNotIn("Vision Model", [k for k, _ in A.AgentConfig().banner_items()])


class ModelPoolTest(unittest.TestCase):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            self.chat = self

        @property
        def completions(self):
            return self

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            message = type("M", (), {"content": " a cat "})()
            return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

    class Pool(A.ModelPool):
        def __init__(self):
            super().__init__()
            self.built = 0

        def client(self, spec):
            client = self._clients.get(spec.endpoint)
            if client is None:
                self.built += 1
                client = ModelPoolTest.FakeClient(base_url=spec.api_url)
                self._clients[spec.endpoint] = client
            return client

    def setUp(self):
        self.pool = self.Pool()

    def test_one_client_per_endpoint(self):
        leader = A.ModelSpec("leader", "m", "http://url", "k")
        vision = A.ModelSpec("vision", "v", "http://url", "k")
        other = A.ModelSpec("vision", "v", "http://other", "k")
        self.assertIs(self.pool.client(leader), self.pool.client(vision))
        self.assertIsNot(self.pool.client(leader), self.pool.client(other))
        self.assertEqual(self.pool.built, 2)

    def test_describe_image_sends_the_data_url_and_returns_text(self):
        spec = A.ModelSpec("vision", "v", "http://url", "k")
        answer = run(self.pool.describe_image(spec, "data:image/png;base64,AA", "what?"))
        self.assertEqual(answer, "a cat")
        payload = self.pool.client(spec).calls[0]
        self.assertEqual(payload["model"], "v")
        content = payload["messages"][1]["content"]
        self.assertEqual(content[0]["text"], "what?")
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,AA")

    def test_clear_drops_cached_clients(self):
        spec = A.ModelSpec("leader", "m", "http://url", "k")
        first = self.pool.client(spec)
        self.pool.clear()
        self.assertIsNot(first, self.pool.client(spec))

    def test_a_configured_num_ctx_is_sent_as_an_option(self):
        spec = A.ModelSpec("vision", "v", "http://url", "k", num_ctx=65536)
        run(self.pool.describe_image(spec, "data:image/png;base64,AA"))
        payload = self.pool.client(spec).calls[0]
        self.assertEqual(payload["extra_body"], {"options": {"num_ctx": 65536}})

    def test_without_a_num_ctx_nothing_extra_is_sent(self):
        spec = A.ModelSpec("vision", "v", "http://url", "k")
        run(self.pool.describe_image(spec, "data:image/png;base64,AA"))
        self.assertIsNone(self.pool.client(spec).calls[0]["extra_body"])

    def test_an_unset_num_ctx_is_looked_up_once(self):
        calls = []

        async def probe(spec):
            calls.append(spec)
            return 262144

        self.pool.probe_ctx = probe
        spec = A.ModelSpec("leader", "m", "http://url", "k")
        self.assertEqual(run(self.pool.num_ctx(spec)), 262144)
        self.assertEqual(run(self.pool.num_ctx(spec)), 262144)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.pool.extra_body(spec), {"options": {"num_ctx": 262144}})

    def test_a_failed_num_ctx_lookup_sends_none(self):
        async def probe(spec):
            raise RuntimeError("offline")

        self.pool.probe_ctx = probe
        spec = A.ModelSpec("leader", "m", "http://url", "k")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run(self.pool.num_ctx(spec)), 0)
        self.assertIsNone(self.pool.extra_body(spec))

    def test_ollama_show_gives_the_trained_context_length(self):
        import httpx

        def handler(request):
            self.assertEqual(request.url.path, "/api/show")
            body = {"model_info": {"general.architecture": "qwen35", "qwen35.context_length": 262144}}
            return httpx.Response(200, json=body)

        self.assertEqual(self.show(handler), 262144)

    def test_a_server_without_ollama_show_gives_none(self):
        import httpx

        self.assertEqual(self.show(lambda request: httpx.Response(404)), 0)
        self.assertEqual(self.show(lambda request: httpx.Response(200, text="<html>")), 0)

    def show(self, handler):
        import httpx

        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with unittest.mock.patch.object(
            httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw)
        ):
            return run(_probe_ctx(self.pool, A.ModelSpec("leader", "m", "http://host:11434/v1")))

    def test_the_config_carries_num_ctx_to_every_role(self):
        config = A.AgentConfig(inference_ctx=32768, inference_vlm="v")
        self.assertEqual(config.model_spec(A.LEADER_ROLE).num_ctx, 32768)
        self.assertEqual(config.model_spec(A.VISION_ROLE).num_ctx, 32768)

    def test_the_environment_sets_inference_ctx(self):
        config = A.AgentConfig().with_env({"AGENTIC_INFERENCE_CTX": "65536"})
        self.assertEqual(config.inference_ctx, 65536)

    def test_the_leader_sends_num_ctx_in_its_model_settings(self):
        engine = A.Engine(inference_ctx=16384, env=False, config_file=False)
        agent = engine.build_sdk_agent(engine.config, [])
        self.assertEqual(agent.model_settings.extra_body, {"options": {"num_ctx": 16384}})
        bare = A.Engine(env=False, config_file=False)
        self.assertIsNone(bare.build_sdk_agent(bare.config, []).model_settings.extra_body)


class RateLimitError(Exception):
    """Named the way a provider SDK names it, which is how it is classified."""


class BadRequestError(Exception):
    pass


class Status(Exception):
    """An error that only says what the transport said: a status code."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = type("Response", (), {"status_code": status})()


def no_sleep():
    """Patch `asyncio.sleep` and collect the delays a retry would have waited."""
    delays: list[float] = []

    async def sleep(seconds):
        delays.append(seconds)

    return unittest.mock.patch("asyncio.sleep", sleep), delays


class ErrorTaxonomyTest(unittest.TestCase):
    """Every failure of a run is reported as one of a handful of kinds."""

    def test_provider_errors_classify_by_class_name(self):
        self.assertEqual(A.classify_error(RateLimitError("slow down")), "rate_limit")
        self.assertEqual(A.classify_error(BadRequestError("nope")), "invalid_request")

    def test_builtin_failures_classify(self):
        self.assertEqual(A.classify_error(TimeoutError()), "timeout")
        self.assertEqual(A.classify_error(ConnectionError()), "connection")
        self.assertEqual(A.classify_error(OSError("socket")), "connection")
        self.assertEqual(A.classify_error(asyncio.CancelledError()), "cancelled")

    def test_unknown_failures_are_internal(self):
        self.assertEqual(A.classify_error(ValueError("boom")), "internal")

    def test_a_status_code_classifies_an_unknown_class(self):
        self.assertEqual(A.classify_error(Status(429)), "rate_limit")
        self.assertEqual(A.classify_error(Status(503)), "server")
        self.assertEqual(A.classify_error(Status(504)), "timeout")
        self.assertEqual(A.classify_error(Status(401)), "auth")
        self.assertEqual(A.classify_error(Status(422)), "invalid_request")

    def test_a_model_error_keeps_its_kind(self):
        error = A.ModelError("stalled", A.ErrorKind.TIMEOUT, role="vision")
        self.assertEqual(A.classify_error(error), "timeout")
        self.assertEqual(error.role, "vision")

    def test_only_some_kinds_are_worth_another_attempt(self):
        self.assertIn(A.ErrorKind.RATE_LIMIT, A.ErrorKind.TRANSIENT)
        self.assertIn(A.ErrorKind.SERVER, A.ErrorKind.TRANSIENT)
        self.assertNotIn(A.ErrorKind.AUTH, A.ErrorKind.TRANSIENT)
        self.assertNotIn(A.ErrorKind.INTERNAL, A.ErrorKind.TRANSIENT)


class RetryPolicyTest(unittest.TestCase):
    """One timeout per attempt, bounded retries, exponential backoff, jitter."""

    def test_from_config_reads_the_settings(self):
        config = A.AgentConfig(
            request_timeout=5.0,
            stream_timeout=50.0,
            retry_attempts=4,
            retry_backoff=2.0,
            retry_jitter=2.0,
        )
        policy = A.RetryPolicy.from_config(config)
        self.assertEqual(policy.timeout, 5.0)
        self.assertEqual(policy.stall, 50.0)
        self.assertEqual(policy.attempts, 4)
        self.assertEqual(policy.backoff, 2.0)
        self.assertEqual(policy.jitter, 1.0)

    def test_backoff_grows_is_capped_and_is_jittered(self):
        policy = A.RetryPolicy(backoff=1.0, max_backoff=4.0, jitter=0.5)
        for attempt, ceiling in ((1, 1.0), (2, 2.0), (3, 4.0), (9, 4.0)):
            delay = policy.delay(attempt)
            self.assertGreaterEqual(delay, ceiling * 0.5)
            self.assertLessEqual(delay, ceiling)

    def test_no_jitter_is_plain_exponential_backoff(self):
        policy = A.RetryPolicy(backoff=0.25, max_backoff=10.0, jitter=0.0)
        self.assertEqual([policy.delay(n) for n in (1, 2, 3)], [0.25, 0.5, 1.0])

    def test_a_successful_call_is_made_once(self):
        calls = []

        async def operation():
            calls.append(1)
            return "ok"

        policy = A.RetryPolicy(attempts=3, timeout=0)
        self.assertEqual(run(policy.call(operation)), "ok")
        self.assertEqual(len(calls), 1)

    def test_a_transient_failure_is_tried_again(self):
        errors = [Status(503), RateLimitError("slow down")]

        async def operation():
            if errors:
                raise errors.pop(0)
            return "ok"

        patch, delays = no_sleep()
        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=1.0, jitter=0.0)
        with patch:
            self.assertEqual(run(policy.call(operation)), "ok")
        self.assertEqual(delays, [1.0, 2.0])

    def test_retries_are_bounded_and_end_as_a_model_error(self):
        async def operation():
            raise RateLimitError("slow down")

        patch, delays = no_sleep()
        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=0.0)
        with patch, self.assertRaises(A.ModelError) as caught:
            run(policy.call(operation, role="vision"))
        self.assertEqual(caught.exception.kind, "rate_limit")
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(caught.exception.role, "vision")
        self.assertIn("slow down", str(caught.exception))
        self.assertEqual(len(delays), 2)

    def test_a_refusal_is_reported_without_a_second_attempt(self):
        calls = []

        async def operation():
            calls.append(1)
            raise Status(401)

        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=0.0)
        with self.assertRaises(A.ModelError) as caught:
            run(policy.call(operation))
        self.assertEqual(caught.exception.kind, "auth")
        self.assertEqual(len(calls), 1)

    def test_an_unknown_failure_is_raised_unchanged(self):
        async def operation():
            raise ValueError("boom")

        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=0.0)
        with self.assertRaises(ValueError) as caught:
            run(policy.call(operation))
        self.assertEqual(str(caught.exception), "boom")

    def test_an_attempt_is_bounded_by_the_timeout(self):
        async def operation():
            await asyncio.Event().wait()

        policy = A.RetryPolicy(attempts=1, timeout=0.01)
        with self.assertRaises(A.ModelError) as caught:
            run(policy.call(operation))
        self.assertEqual(caught.exception.kind, "timeout")

    def test_an_unbounded_call_is_left_to_time_itself_out(self):
        async def operation():
            await asyncio.sleep(0)
            return "ok"

        policy = A.RetryPolicy(attempts=1, timeout=0.01)
        self.assertEqual(run(policy.call(operation, bounded=False)), "ok")

    def test_a_call_that_cannot_be_resumed_is_not_tried_again(self):
        calls = []

        async def operation():
            calls.append(1)
            raise Status(503)

        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=0.0)
        with self.assertRaises(A.ModelError):
            run(policy.call(operation, resumable=lambda: False))
        self.assertEqual(len(calls), 1)

    def test_cancellation_is_never_retried(self):
        calls = []

        async def operation():
            calls.append(1)
            raise asyncio.CancelledError()

        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=0.0)
        with self.assertRaises(asyncio.CancelledError):
            run(policy.call(operation))
        self.assertEqual(len(calls), 1)

    def test_every_retry_is_reported(self):
        errors = [Status(503)]
        seen: list[dict] = []

        async def operation():
            if errors:
                raise errors.pop(0)
            return "ok"

        async def on_retry(info):
            seen.append(info)

        patch, _ = no_sleep()
        policy = A.RetryPolicy(attempts=2, timeout=0, backoff=0.0)
        with patch:
            run(policy.call(operation, role="leader", on_retry=on_retry))
        self.assertEqual(seen[0]["error_kind"], "server")
        self.assertEqual(seen[0]["attempt"], 1)
        self.assertEqual(seen[0]["attempts"], 2)
        self.assertEqual(seen[0]["role"], "leader")


class UsageTest(unittest.TestCase):
    """Tokens are counted wherever they are spent and priced at the end."""

    def test_either_naming_is_recorded(self):
        usage = A.Usage()
        usage.record({"prompt_tokens": 10, "completion_tokens": 4})
        usage.record(types.SimpleNamespace(input_tokens=5, output_tokens=1, requests=2))
        self.assertEqual(usage.requests, 3)
        self.assertEqual(usage.input_tokens, 15)
        self.assertEqual(usage.output_tokens, 5)
        self.assertEqual(usage.total_tokens, 20)

    def test_nothing_is_recorded_without_a_payload(self):
        self.assertEqual(A.Usage().record(None).requests, 0)

    def test_tokens_are_priced_per_million(self):
        usage = A.Usage(input_tokens=1_000_000, output_tokens=500_000)
        self.assertAlmostEqual(usage.price(3.0, 15.0), 10.5)
        self.assertAlmostEqual(usage.to_dict()["cost"], 10.5)

    def test_an_unpriced_run_still_reports_its_tokens(self):
        usage = A.Usage(input_tokens=7)
        usage.price()
        self.assertEqual(usage.to_dict()["cost"], 0.0)
        self.assertEqual(usage.to_dict()["input_tokens"], 7)

    def test_a_payload_is_never_counted_twice_on_one_sink(self):
        sink = A.Usage()
        A.record_usage({"prompt_tokens": 3}, sink, sink)
        self.assertEqual(sink.input_tokens, 3)
        self.assertEqual(sink.requests, 1)

    def test_usage_of_prefers_the_running_totals_of_the_sdk(self):
        streamed = types.SimpleNamespace(
            context_wrapper=types.SimpleNamespace(usage="totals"), raw_responses=[]
        )
        self.assertEqual(A.usage_of(streamed), "totals")

    def test_usage_of_falls_back_to_the_raw_responses(self):
        streamed = types.SimpleNamespace(
            context_wrapper=None,
            raw_responses=[
                types.SimpleNamespace(usage={"input_tokens": 2, "output_tokens": 1}),
                types.SimpleNamespace(usage={"prompt_tokens": 3, "completion_tokens": 4}),
            ],
        )
        self.assertEqual(
            A.usage_of(streamed),
            {"requests": 2, "input_tokens": 5, "output_tokens": 5},
        )

    def test_usage_of_reports_nothing_when_the_sdk_keeps_nothing(self):
        self.assertIsNone(A.usage_of(types.SimpleNamespace()))


class ModelPoolResilienceTest(unittest.TestCase):
    """Specialist calls are bounded, retried and accounted like the leader."""

    class FailingClient:
        def __init__(self, errors=(), usage=None):
            self.errors = list(errors)
            self.usage = usage
            self.calls = []
            self.chat = self

        @property
        def completions(self):
            return self

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if self.errors:
                raise self.errors.pop(0)
            message = type("M", (), {"content": "done"})()
            return type(
                "R",
                (),
                {"choices": [type("C", (), {"message": message})()], "usage": self.usage},
            )()

    def pool(self, client, **policy):
        pool = A.ModelPool(A.RetryPolicy(timeout=0, backoff=0.0, **policy))
        pool._clients[("http://url", "k")] = client
        return pool

    def test_a_transient_failure_is_tried_again(self):
        client = self.FailingClient(errors=[Status(503)])
        pool = self.pool(client, attempts=2)
        spec = A.ModelSpec("vision", "v", "http://url", "k")
        patch, _ = no_sleep()
        with patch:
            answer = run(pool.complete(spec, [{"role": "user", "content": "hi"}]))
        self.assertEqual(answer, "done")
        self.assertEqual(len(client.calls), 2)

    def test_an_exhausted_call_reports_the_role_and_the_kind(self):
        client = self.FailingClient(errors=[Status(503), Status(503)])
        pool = self.pool(client, attempts=2)
        spec = A.ModelSpec("vision", "v", "http://url", "k")
        patch, _ = no_sleep()
        with patch, self.assertRaises(A.ModelError) as caught:
            run(pool.complete(spec, []))
        self.assertEqual(caught.exception.kind, "server")
        self.assertEqual(caught.exception.role, "vision")

    def test_tokens_are_recorded_on_the_pool_and_the_caller(self):
        client = self.FailingClient(usage={"prompt_tokens": 9, "completion_tokens": 3})
        pool = self.pool(client, attempts=1)
        spec = A.ModelSpec("vision", "v", "http://url", "k")
        sink = A.Usage()
        run(pool.complete(spec, [], usage=sink))
        self.assertEqual(sink.input_tokens, 9)
        self.assertEqual(sink.output_tokens, 3)
        self.assertEqual(pool.usage.input_tokens, 9)
        self.assertEqual(pool.usage.requests, 1)

    def test_the_sdk_is_not_allowed_to_retry_behind_the_policy(self):
        pool = A.ModelPool(A.RetryPolicy(timeout=42.0, stall=0.0))
        client = pool.client(A.ModelSpec("leader", "m", "http://url", "k"))
        self.assertEqual(client.max_retries, 0)
        self.assertEqual(client.timeout.connect, 42.0)
        self.assertEqual(client.timeout.read, 42.0)

    def test_the_transport_outlasts_a_silent_stream(self):
        # A provider that holds a tool call back until it is complete (Ollama)
        # sends nothing while the model writes it; the socket must not give up
        # before the stall guard does.
        pool = A.ModelPool(A.RetryPolicy(timeout=42.0, stall=600.0))
        client = pool.client(A.ModelSpec("leader", "m", "http://url", "k"))
        self.assertEqual(client.timeout.connect, 42.0)
        self.assertEqual(client.timeout.read, 600.0)

    def test_an_endpoint_without_a_key_gets_a_placeholder(self):
        environ = dict(os.environ)
        environ.pop("OPENAI_API_KEY", None)
        with unittest.mock.patch.dict(os.environ, environ, clear=True):
            pool = A.ModelPool()
            client = pool.client(A.ModelSpec("leader", "m", "http://url", ""))
            self.assertEqual(client.api_key, "no-key")

    def test_a_keyless_endpoint_keeps_the_environment_fallback(self):
        environ = dict(os.environ)
        environ["OPENAI_API_KEY"] = "from-env"
        with unittest.mock.patch.dict(os.environ, environ, clear=True):
            pool = A.ModelPool()
            client = pool.client(A.ModelSpec("leader", "m", "http://url", ""))
            self.assertEqual(client.api_key, "from-env")

    def test_an_unbounded_policy_leaves_the_transport_alone(self):
        pool = A.ModelPool(A.RetryPolicy(timeout=0, stall=0))
        client = pool.client(A.ModelSpec("leader", "m", "http://url", "k"))
        self.assertEqual(client.max_retries, 0)
        self.assertNotEqual(client.timeout, 0)

    def test_the_totals_survive_a_clear(self):
        client = self.FailingClient(usage={"prompt_tokens": 2})
        pool = self.pool(client, attempts=1)
        run(pool.complete(A.ModelSpec("vision", "v", "http://url", "k"), []))
        pool.clear()
        self.assertEqual(pool.usage.input_tokens, 2)


class EngineResilienceTest(unittest.TestCase):
    """A run bounds its model calls, classifies what failed and prices it."""

    class RetryEngine(A.Engine):
        """An engine whose stream fails a given number of times first."""

        errors: list = []
        partial: bool = False

        def build_sdk_agent(self, config, tools):
            return {}

        async def stream(self, sdk_agent, model_input, config, result):
            self.attempts = getattr(self, "attempts", 0) + 1
            if self.partial:
                result.blocks.append(A.Block(id="b0", kind="output"))
            if self.errors:
                raise self.errors.pop(0)
            result.usage.record({"prompt_tokens": 100, "completion_tokens": 10})
            result.output = "ok"
            return result

    def engine(self, errors=(), partial=False, **overrides):
        engine = self.RetryEngine(
            env=False, retry_backoff=0.0, request_timeout=0, **overrides
        )
        engine.errors = list(errors)
        engine.partial = partial
        self.addCleanup(engine.close)
        return engine

    def test_a_stalled_leader_is_tried_again_before_anything_is_shown(self):
        engine = self.engine(errors=[Status(503)])
        patch, _ = no_sleep()
        with patch:
            result = run(engine.run("hello"))
        self.assertTrue(result.ok)
        self.assertEqual(engine.attempts, 2)

    def test_a_stream_that_already_produced_output_is_not_replayed(self):
        engine = self.engine(errors=[Status(503)], partial=True)
        result = run(engine.run("hello"))
        self.assertFalse(result.ok)
        self.assertEqual(engine.attempts, 1)
        self.assertEqual(result.error_kind, "server")

    def test_a_failure_carries_its_kind(self):
        engine = self.engine(errors=[Status(401)])
        result = run(engine.run("hello"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "auth")
        self.assertFalse(result.transient)
        self.assertIn("auth", result.error)

    def test_an_exhausted_retry_is_reported_as_transient(self):
        engine = self.engine(errors=[Status(503), Status(503), Status(503)])
        patch, _ = no_sleep()
        with patch:
            result = run(engine.run("hello"))
        self.assertEqual(result.error_kind, "server")
        self.assertTrue(result.transient)
        self.assertEqual(engine.attempts, 3)

    def test_a_defect_in_the_harness_is_not_disguised_as_a_provider_fault(self):
        engine = self.engine(errors=[ValueError("boom")])
        result = run(engine.run("hello"))
        self.assertEqual(result.error, "boom")
        self.assertEqual(result.error_kind, "internal")
        self.assertEqual(engine.attempts, 1)

    def test_every_retry_is_published(self):
        engine = self.engine(errors=[Status(503)])
        seen: list[A.Event] = []
        engine.events.on(A.EventType.MODEL_RETRY, seen.append)
        patch, _ = no_sleep()
        with patch:
            run(engine.run("hello"))
        self.assertEqual(seen[0].data["error_kind"], "server")
        self.assertEqual(seen[0].data["role"], A.LEADER_ROLE)

    def test_a_retry_is_counted_against_the_run(self):
        engine = self.engine(errors=[Status(503)])
        patch, _ = no_sleep()
        with patch:
            result = run(engine.run("hello"))
        self.assertEqual(result.usage.retries, 1)

    def test_the_end_of_a_run_carries_its_tokens_and_its_cost(self):
        engine = self.engine(cost_input=3.0, cost_output=15.0)
        ended: list[dict] = []
        engine.events.on(A.EventType.AGENT_END, lambda e: ended.append(e.data))
        result = run(engine.run("hello"))
        self.assertEqual(result.usage.total_tokens, 110)
        self.assertAlmostEqual(result.usage.cost, 100 * 3.0 / 1e6 + 10 * 15.0 / 1e6)
        self.assertEqual(ended[0]["usage"]["total_tokens"], 110)
        self.assertAlmostEqual(ended[0]["usage"]["cost"], result.usage.cost)
        self.assertIsNone(ended[0]["error_kind"])

    def test_a_specialist_called_by_a_tool_is_accounted_on_the_run(self):
        class Accounting(self.RetryEngine):
            async def stream(self, sdk_agent, model_input, config, result):
                usage = A.current_usage()
                usage.record({"prompt_tokens": 40, "completion_tokens": 2})
                result.output = "ok"
                return result

        engine = Accounting(env=False)
        self.addCleanup(engine.close)
        result = run(engine.run("hello"))
        self.assertEqual(result.usage.input_tokens, 40)

    def test_cancellation_is_reported_as_cancellation(self):
        class Stuck(self.RetryEngine):
            async def stream(self, sdk_agent, model_input, config, result):
                raise asyncio.CancelledError()

        engine = Stuck(env=False)
        self.addCleanup(engine.close)
        with self.assertRaises(asyncio.CancelledError):
            run(engine.run("hello"))


class StallGuardTest(unittest.TestCase):
    """The leader may go quiet, but not for longer than the run allows."""

    def setUp(self):
        self.engine = A.Engine(env=False)
        self.addCleanup(self.engine.close)

    async def stream_of(self, *events, delay=0.0):
        for event in events:
            if delay:
                await asyncio.sleep(delay)
            yield event

    def test_an_event_arrives_within_the_budget(self):
        events = self.stream_of("first", "second").__aiter__()
        self.assertEqual(run(self.engine.next_event(events, 1.0)), "first")

    def test_a_silent_provider_fails_the_attempt(self):
        events = self.stream_of("late", delay=5.0).__aiter__()
        with self.assertRaises(A.ModelError) as caught:
            run(self.engine.next_event(events, 0.01))
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(caught.exception.role, A.LEADER_ROLE)

    def test_the_end_of_a_stream_is_not_a_failure(self):
        events = self.stream_of().__aiter__()
        with self.assertRaises(StopAsyncIteration):
            run(self.engine.next_event(events, 1.0))

    def test_a_budget_of_zero_waits_forever(self):
        events = self.stream_of("first").__aiter__()
        self.assertEqual(run(self.engine.next_event(events, 0.0)), "first")

    def test_a_running_tool_extends_the_budget(self):
        policy = A.RetryPolicy(timeout=5.0, stall=30.0)
        config = A.AgentConfig(shell_timeout=120)
        self.assertEqual(self.engine.stall_timeout(policy, config, False), 30.0)
        self.assertEqual(self.engine.stall_timeout(policy, config, True), 150.0)

    def test_the_budget_is_the_stream_timeout_not_the_request_timeout(self):
        policy = A.RetryPolicy.from_config(
            A.AgentConfig(request_timeout=120.0, stream_timeout=600.0)
        )
        self.assertEqual(self.engine.stall_timeout(policy, A.AgentConfig()), 600.0)

    def test_an_unbounded_policy_never_stalls(self):
        policy = A.RetryPolicy(stall=0.0)
        self.assertEqual(self.engine.stall_timeout(policy, A.AgentConfig(), True), 0.0)

    def test_a_sign_of_life_restarts_the_budget(self):
        events = self.stream_of("late", delay=0.3).__aiter__()
        self.assertEqual(run(self.engine.next_event(events, 0.1, time.time)), "late")

    def test_a_stale_sign_of_life_does_not(self):
        events = self.stream_of("late", delay=5.0).__aiter__()
        with self.assertRaises(A.ModelError):
            run(self.engine.next_event(events, 0.05, lambda: time.time() - 60))

    def test_an_abandoned_stream_is_cancelled(self):
        calls = []
        A.Engine.cancel_stream(types.SimpleNamespace(cancel=lambda: calls.append(1)))
        A.Engine.cancel_stream(types.SimpleNamespace())
        self.assertEqual(calls, [1])

    def test_a_silent_model_reports_how_long_it_has_waited(self):
        waits: list[float] = []

        async def on_wait(elapsed):
            waits.append(elapsed)

        events = self.stream_of("late", delay=0.25).__aiter__()
        event = run(self.engine.next_event(events, 0.0, heartbeat=0.05, on_wait=on_wait))
        self.assertEqual(event, "late")
        self.assertGreaterEqual(len(waits), 2)
        self.assertEqual(waits, sorted(waits))
        self.assertGreater(waits[0], 0.0)

    def test_no_heartbeat_without_a_period(self):
        waits: list[float] = []

        async def on_wait(elapsed):
            waits.append(elapsed)

        events = self.stream_of("late", delay=0.1).__aiter__()
        run(self.engine.next_event(events, 1.0, heartbeat=0.0, on_wait=on_wait))
        self.assertEqual(waits, [])

    def test_a_heartbeat_does_not_extend_the_budget(self):
        async def on_wait(_elapsed):
            return None

        events = self.stream_of("late", delay=5.0).__aiter__()
        with self.assertRaises(A.ModelError):
            run(self.engine.next_event(events, 0.1, heartbeat=0.02, on_wait=on_wait))

    def test_a_failing_wait_listener_never_breaks_the_stream(self):
        async def on_wait(_elapsed):
            raise RuntimeError("listener is broken")

        events = self.stream_of("late", delay=0.1).__aiter__()
        with contextlib.redirect_stderr(io.StringIO()):
            event = run(self.engine.next_event(events, 1.0, heartbeat=0.02, on_wait=on_wait))
        self.assertEqual(event, "late")


class ToolProgressTest(unittest.TestCase):
    """A running tool reports what it does, and that it is still doing it."""

    def setUp(self):
        self.engine = A.Engine(env=False)
        self.addCleanup(self.engine.close)
        self.result = A.RunResult(session_id="s1")
        self.call = A.ToolCall(id="b1", name="run_shell_command", call_id="c1")
        self.result.tools.append(self.call)
        self.seen: list[A.Event] = []
        self.engine.events.on(A.EventType.TOOL_PROGRESS, self.seen.append)

    def test_progress_is_published_on_the_block_of_the_call(self):
        run(self.engine.tool_reporter(self.result)("c1", "hello"))
        event = self.seen[0]
        self.assertEqual(event.session_id, "s1")
        self.assertEqual(
            (event.data["id"], event.data["name"], event.data["call_id"], event.data["text"]),
            ("b1", "run_shell_command", "c1", "hello"),
        )
        self.assertIn("duration", event.data)

    def test_progress_is_a_sign_of_life(self):
        self.call.seen_at -= 100
        before = self.engine.alive_at(self.result)
        run(self.engine.tool_reporter(self.result)("c1", ""))
        self.assertGreater(self.engine.alive_at(self.result), before)

    def test_finished_calls_are_not_alive(self):
        self.call.finish("done")
        self.assertEqual(self.engine.alive_at(self.result), 0.0)

    def test_progress_without_an_open_call_is_dropped(self):
        self.call.finish("done")
        run(self.engine.tool_reporter(self.result)("c9", "late"))
        self.assertEqual(self.seen, [])

    def test_the_workspace_beats_at_the_rate_of_the_config(self):
        with tempfile.TemporaryDirectory() as root:
            ws = self.engine.build_workspace(root, A.AgentConfig(shell_heartbeat=2.5))
            self.assertEqual(ws.shell_heartbeat, 2.5)

    def test_the_workspace_reaches_outside_only_when_the_config_says(self):
        with tempfile.TemporaryDirectory() as root:
            ws = self.engine.build_workspace(root, A.AgentConfig())
            self.assertFalse(ws.outside_read)
            self.assertFalse(ws.outside_write)
            ws = self.engine.build_workspace(
                root, A.AgentConfig(outside_read=True, outside_write=True)
            )
            self.assertTrue(ws.outside_read)
            self.assertTrue(ws.outside_write)
            self.assertIsNone(ws.sandbox)  # nothing is off limits: nothing to confine


class QuestionTest(unittest.TestCase):
    """The model asks the person a question and waits for the answer."""

    def setUp(self):
        self.engine = A.Engine(env=False)
        self.addCleanup(self.engine.close)
        self.config = A.AgentConfig(ask_enabled=True)
        self.result = A.RunResult(session_id="s1")
        self.call = A.ToolCall(id="b1", name="ask_user", call_id="c1")
        self.result.tools.append(self.call)
        self.seen: list[A.Event] = []
        self.engine.events.on(A.EventType.QUESTION_START, self.seen.append)
        self.engine.events.on(A.EventType.QUESTION_END, self.seen.append)

    async def opened(self, task):
        """Wait until `task` has published its question, and return its id."""
        while not self.engine.questions:
            self.assertFalse(task.done())
            await asyncio.sleep(0.005)
        return next(iter(self.engine.questions))

    def test_only_a_mode_that_can_answer_gets_an_asker(self):
        self.assertIsNone(self.engine.build_asker(A.AgentConfig(), self.result))
        self.assertIsNotNone(self.engine.build_asker(self.config, self.result))

    def test_an_answer_resolves_the_question(self):
        async def drive():
            asker = self.engine.build_asker(self.config, self.result)
            task = asyncio.create_task(asker("c1", "Which one?", ["red", "blue"]))
            question = await self.opened(task)
            self.assertTrue(self.call.asking)
            self.assertTrue(self.engine.answer(question, "blue", session_id="s1"))
            return question, await task

        question, answer = run(drive())
        self.assertEqual((question, answer), ("b1", "blue"))
        self.assertEqual(self.engine.questions, {})
        self.assertFalse(self.call.asking)
        start, end = self.seen
        self.assertEqual(start.type, "question.start")
        self.assertEqual(
            (start.data["id"], start.data["call_id"], start.data["question"], start.data["options"]),
            ("b1", "c1", "Which one?", ["red", "blue"]),
        )
        self.assertEqual(end.type, "question.end")
        self.assertEqual((end.data["answer"], end.data["ok"]), ("blue", True))

    def test_a_question_waits_on_a_person_not_on_the_stall_guard(self):
        self.call.seen_at -= 1000
        self.call.asking = True
        self.assertGreater(self.engine.alive_at(self.result), time.time() - 1)

    def test_an_unknown_question_cannot_be_answered(self):
        self.assertFalse(self.engine.answer("nope", "x"))

    def test_a_question_belongs_to_its_session(self):
        async def drive():
            asker = self.engine.build_asker(self.config, self.result)
            task = asyncio.create_task(asker("c1", "Which one?", []))
            question = await self.opened(task)
            refused = self.engine.answer(question, "mine", session_id="other")
            accepted = self.engine.answer(question, "theirs", session_id="s1")
            return refused, accepted, await task

        self.assertEqual(run(drive()), (False, True, "theirs"))

    def test_a_cancelled_question_is_closed(self):
        async def drive():
            asker = self.engine.build_asker(self.config, self.result)
            task = asyncio.create_task(asker("c1", "Which one?", []))
            await self.opened(task)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        run(drive())
        self.assertEqual(self.engine.questions, {})
        self.assertFalse(self.call.asking)
        self.assertEqual((self.seen[-1].type, self.seen[-1].data["ok"]), ("question.end", False))

    def test_a_question_without_a_call_gets_an_id_of_its_own(self):
        self.result.tools.clear()

        async def drive():
            asker = self.engine.build_asker(self.config, self.result)
            task = asyncio.create_task(asker(None, "Which one?", []))
            question = await self.opened(task)
            self.engine.answer(question, "ok")
            await task
            return question

        question = run(drive())
        self.assertTrue(question.startswith(f"s1-{self.result.run_id}-q"))


class InputResolutionTest(unittest.TestCase):
    def test_raw_text_is_returned_as_is(self):
        self.assertEqual(A.resolve_input("just text"), "just text")
        self.assertEqual(A.resolve_input(""), "")

    def test_file_path_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.md"
            path.write_text("from file", encoding="utf-8")
            self.assertEqual(A.resolve_input(str(path)), "from file")

    def test_path_object_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.md"
            path.write_text("from path", encoding="utf-8")
            self.assertEqual(A.resolve_input(path), "from path")
            self.assertEqual(A.resolve_input(Path(tmp) / "missing.md"), str(Path(tmp) / "missing.md"))

    def test_agent_assigns_resolved_text_to_config_input(self):
        agent = A.Agent(console=False, store=A.MemoryStore())
        self.addCleanup(agent.close)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.md"
            path.write_text("story seed", encoding="utf-8")
            config = agent.resolve_config(input=str(path))
        self.assertEqual(config.input, "story seed")
        self.assertTrue(config.input_source.endswith("prompt.md"))


class EventBusTest(unittest.TestCase):
    def test_sync_and_async_subscribers_receive_events(self):
        bus = A.EventBus()
        seen: list[str] = []
        bus.on("a", lambda e: seen.append(f"sync:{e.data['v']}"))

        async def handler(event):
            seen.append(f"async:{event.data['v']}")

        bus.on("a", handler)
        bus.on(A.EventType.ALL, lambda e: seen.append(f"all:{e.type}"))
        run(bus.publish("a", v=1))
        self.assertEqual(seen, ["sync:1", "async:1", "all:a"])

    def test_unsubscribe_and_failing_subscriber(self):
        bus = A.EventBus()
        seen: list[str] = []
        off = bus.on("a", lambda e: seen.append("first"))
        bus.on("a", lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.on("a", lambda e: seen.append("last"))
        off()
        run(bus.publish("a"))
        self.assertEqual(seen, ["last"])  # failure never breaks the chain


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.prompts = A.PromptRenderer()

    def test_renders_config_input(self):
        config = A.AgentConfig(input="the seed")
        rendered = self.prompts.render("Use: {{ config.input }}", config)
        self.assertEqual(rendered, "Use: the seed")

    def test_renders_extras_and_context(self):
        config = A.AgentConfig(extras={"genre": "noir"})
        rendered = self.prompts.render("{{ config.genre }}/{{ tone }}", config, tone="dry")
        self.assertEqual(rendered, "noir/dry")

    def test_strict_undefined_raises(self):
        from jinja2 import UndefinedError

        with self.assertRaises(UndefinedError):
            self.prompts.render("{{ nope }}", A.AgentConfig())

    def test_render_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.md"
            path.write_text("Hi {{ config.input }}", encoding="utf-8")
            self.assertEqual(
                self.prompts.render_file(path, A.AgentConfig(input="there")), "Hi there"
            )


class StorageTest(unittest.TestCase):
    def test_sqlite_store_roundtrip(self):
        store = A.SqliteStore()
        self.addCleanup(store.close)
        store.set("ns", "k", {"a": 1})
        self.assertEqual(store.get("ns", "k"), {"a": 1})
        store.set("ns", "k", {"a": 2})  # upsert
        self.assertEqual(store.list("ns"), [("k", {"a": 2})])
        store.delete("ns", "k")
        self.assertIsNone(store.get("ns", "k"))
        self.assertEqual(store.list("ns"), [])

    def test_sqlite_store_persists_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "agent.db"
            store = A.SqliteStore(path)
            store.set("ns", "k", "v")
            store.close()
            reopened = A.SqliteStore(path)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.get("ns", "k"), "v")

    def test_stores_satisfy_the_protocol(self):
        self.assertIsInstance(A.MemoryStore(), A.Store)
        self.assertIsInstance(A.LruCache(2), A.Cache)

    def test_lru_cache_evicts_least_recently_used(self):
        cache = A.LruCache(maxsize=2)
        cache.set("a", 1)
        cache.set("b", 2)
        self.assertEqual(cache.get("a"), 1)  # refresh 'a'
        cache.set("c", 3)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("c"), 3)
        cache.delete("c")
        self.assertIsNone(cache.get("c"))
        cache.clear()
        self.assertEqual(len(cache), 0)


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.store = A.MemoryStore()
        self.sessions = A.SessionManager(ttl=60, store=self.store, cache=A.LruCache(4))
        self.addCleanup(self.sessions.close_all)

    def test_create_isolates_a_workspace(self):
        first = self.sessions.create()
        second = self.sessions.create()
        self.assertTrue(first.workspace.is_dir())
        self.assertNotEqual(first.workspace, second.workspace)
        self.assertEqual(self.store.get("sessions", first.id)["id"], first.id)

    def test_close_leaves_no_trace(self):
        session = self.sessions.create()
        (session.workspace / "note.txt").write_text("x", encoding="utf-8")
        self.assertTrue(self.sessions.close(session.id))
        self.assertFalse(session.workspace.exists())
        self.assertIsNone(self.store.get("sessions", session.id))
        self.assertFalse(self.sessions.close(session.id))

    def test_expired_sessions_are_purged(self):
        session = self.sessions.create()
        session.expires_at = 0
        self.assertTrue(session.expired)
        self.assertEqual(self.sessions.purge_expired(), [session.id])
        self.assertIsNone(self.sessions.get(session.id))
        self.assertFalse(session.workspace.exists())

    def test_ensure_reuses_a_live_session(self):
        session = self.sessions.create()
        self.assertIs(self.sessions.ensure(session.id).id, session.id)
        self.assertNotEqual(self.sessions.ensure(None).id, session.id)

    def test_keep_workspace_preserves_the_directory(self):
        manager = A.SessionManager(ttl=60, keep_workspace=True)
        session = manager.create()
        self.addCleanup(lambda: __import__("shutil").rmtree(session.workspace, True))
        manager.close(session.id)
        self.assertTrue(session.workspace.exists())

    def test_access_renews_the_lease(self):
        session = self.sessions.create()
        session.expires_at = A.time.time() + 1
        renewed = self.sessions.get(session.id)
        self.assertGreater(renewed.expires_at, A.time.time() + 30)
        self.assertGreater(self.store.get("sessions", session.id)["expires_at"], 0)


class DurableSessionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, True))
        self.store = A.MemoryStore()

    def manager(self, **overrides):
        options = dict(
            root=self.root, ttl=60, store=self.store, prefix="agent", durable=True
        )
        options.update(overrides)
        manager = A.SessionManager(**options)
        self.addCleanup(lambda: manager.close_all(destroy=True))
        return manager

    def test_a_durable_shutdown_keeps_the_workspace_and_the_record(self):
        first = self.manager()
        session = first.create()
        first.close_all()
        self.assertTrue(session.workspace.is_dir())
        self.assertIsNotNone(self.store.get("sessions", session.id))

    def test_live_sessions_are_rehydrated_on_startup(self):
        first = self.manager()
        session = first.create(who="me")
        (session.workspace / "note.txt").write_text("x", encoding="utf-8")
        first.close_all()
        second = self.manager()
        restored = second.get(session.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.workspace, session.workspace)
        self.assertEqual(restored.meta, {"who": "me"})
        self.assertEqual([s.id for s in second.list()], [session.id])

    def test_expired_and_vanished_records_are_reconciled_away(self):
        first = self.manager()
        stale = first.create()
        stale.expires_at = 0
        first.update(stale)
        vanished = first.create()
        __import__("shutil").rmtree(vanished.workspace)
        first.close_all()
        second = self.manager()
        self.assertEqual(second.list(), [])
        self.assertIsNone(self.store.get("sessions", stale.id))
        self.assertIsNone(self.store.get("sessions", vanished.id))
        self.assertFalse(stale.workspace.exists())

    def test_unreadable_records_are_dropped(self):
        self.store.set("sessions", "broken", {"nope": True})
        self.assertEqual(self.manager().list(), [])
        self.assertIsNone(self.store.get("sessions", "broken"))

    def test_directories_without_an_owner_are_reaped(self):
        manager = self.manager()
        session = manager.create()
        orphan = self.root / "agent-orphan-1"
        orphan.mkdir()
        foreign = self.root / "other-tool-1"
        foreign.mkdir()
        self.assertEqual(manager.orphans(), [orphan])
        self.assertEqual(manager.reap_orphans(), [orphan])
        self.assertFalse(orphan.exists())
        self.assertTrue(foreign.is_dir())
        self.assertTrue(session.workspace.is_dir())

    def test_kept_workspaces_are_never_reaped(self):
        manager = self.manager(keep_workspace=True)
        orphan = self.root / "agent-orphan-1"
        orphan.mkdir()
        self.assertEqual(manager.reap_orphans(), [])
        self.assertTrue(orphan.is_dir())

    def test_sweep_expires_and_reaps(self):
        manager = self.manager()
        session = manager.create()
        session.expires_at = 0
        orphan = self.root / "agent-orphan-1"
        orphan.mkdir()
        expired, reaped = manager.sweep()
        self.assertEqual(expired, [session.id])
        self.assertEqual(reaped, [orphan])
        self.assertFalse(session.workspace.exists())

    def test_the_sweeper_expires_on_a_timer(self):
        async def scenario():
            manager = self.manager(sweep_interval=0.01)
            session = manager.create()
            session.expires_at = 0
            manager.start_sweeper()
            self.assertIs(manager.start_sweeper(), manager._sweeper)
            for _ in range(200):
                await asyncio.sleep(0.01)
                if not manager.list():
                    break
            await manager.stop_sweeper()
            return session

        session = run(scenario())
        self.assertFalse(session.workspace.exists())

    def test_the_sweeper_is_optional(self):
        async def scenario():
            manager = self.manager(sweep_interval=0)
            self.assertIsNone(manager.start_sweeper())
            await manager.stop_sweeper()

        run(scenario())


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.store = A.MemoryStore()
        self.memory = A.ConversationMemory(self.store)

    def exchange(self, asked, answered, session="s1"):
        return run(
            self.memory.remember(session, [A.Turn("user", asked), A.Turn("assistant", answered)])
        )

    def test_remembered_turns_become_chat_messages(self):
        self.exchange("hello", "hi")
        self.assertEqual(
            self.memory.history("s1"),
            [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
        )

    def test_empty_turns_are_not_remembered(self):
        self.exchange("hello", "   ")
        self.assertEqual(len(self.memory.transcript("s1")), 1)

    def test_transcripts_survive_a_new_memory_over_the_same_store(self):
        self.exchange("hello", "hi")
        reloaded = A.ConversationMemory(self.store)
        self.assertEqual(reloaded.transcript("s1").messages(), self.memory.history("s1"))

    def test_replay_carries_the_block_kind_of_each_role(self):
        self.exchange("hello", "hi")
        self.assertEqual([t["kind"] for t in self.memory.replay("s1")], ["prompt", "output"])

    def test_every_turn_is_kept(self):
        """Nothing is trimmed: the window is the context's business."""
        for n in range(30):
            self.exchange(f"q{n}", f"a{n}")
        self.assertEqual(len(A.ConversationMemory(self.store).transcript("s1")), 60)

    def test_turns_are_numbered_and_stored_one_record_each(self):
        self.exchange("one", "1")
        turns = self.memory.transcript("s1").turns
        self.assertEqual([t.seq for t in turns], [1, 2])
        rows = self.store.list(A.ConversationMemory.turns_namespace("s1"))
        self.assertEqual(sorted(key for key, _ in rows), ["000000000001", "000000000002"])

    def test_a_journal_write_costs_only_the_new_turns(self):
        for n in range(20):
            self.exchange(f"q{n}", f"a{n}")
        writes: list[str] = []
        store_set = self.store.set
        self.store.set = lambda ns, key, value: (writes.append(key), store_set(ns, key, value))
        self.memory.journal("s1", A.Turn("user", "more"))
        self.assertEqual(writes, ["000000000041"])

    def test_persist_rewrites_only_the_turns_named(self):
        self.exchange("one", "1")
        transcript = self.memory.transcript("s1")
        answer = transcript.turns[1]
        answer.meta["vote"] = "up"
        self.memory.persist(transcript, answer)
        reloaded = A.ConversationMemory(self.store).transcript("s1")
        self.assertEqual(reloaded.turns[1].meta, {"vote": "up"})

    def test_a_legacy_transcript_is_rewritten_one_record_per_turn(self):
        self.store.set(
            A.ConversationMemory.NAMESPACE,
            "old",
            {"session": "old", "turns": [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "yo"}]},
        )
        transcript = self.memory.transcript("old")
        self.assertEqual([(t.seq, t.text) for t in transcript.turns], [(1, "hi"), (2, "yo")])
        header = self.store.get(A.ConversationMemory.NAMESPACE, "old")
        self.assertNotIn("turns", header)
        self.assertEqual(len(A.ConversationMemory(self.store).transcript("old")), 2)

    def test_forget_erases_the_transcript(self):
        self.exchange("hello", "hi")
        self.memory.forget("s1")
        self.assertEqual(self.memory.history("s1"), [])
        self.assertIsNone(self.store.get(A.ConversationMemory.NAMESPACE, "s1"))
        self.assertEqual(self.store.list(A.ConversationMemory.turns_namespace("s1")), [])

    def test_forget_works_over_a_store_without_clear(self):
        class Plain(A.MemoryStore):
            clear = None

        store = Plain()
        memory = A.ConversationMemory(store)
        run(memory.remember("s1", [A.Turn("user", "a"), A.Turn("assistant", "b")]))
        memory.forget("s1")
        self.assertEqual(store.list(A.ConversationMemory.turns_namespace("s1")), [])

    def test_disabled_memory_remembers_nothing(self):
        memory = A.ConversationMemory(self.store, enabled=False)
        run(memory.remember("s6", [A.Turn("user", "hello")]))
        self.assertEqual(memory.history("s6"), [])
        self.assertEqual(memory.replay("s6"), [])
        self.assertIsNone(self.store.get(A.ConversationMemory.NAMESPACE, "s6"))


def tool_pair(call_id, output="ok", name="read", args="{}"):
    """The two input items of one answered tool call."""
    return [
        {"type": "function_call", "call_id": call_id, "name": name, "arguments": args},
        {"type": "function_call_output", "call_id": call_id, "output": output},
    ]


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def thought(text):
    return {"type": "reasoning", "id": f"rs-{text}", "summary": [{"type": "summary_text", "text": text}]}


class WindowTest(unittest.TestCase):
    """The model's view of a journal: the newest summary, then the turns it keeps."""

    def setUp(self):
        self.memory = A.ConversationMemory(A.MemoryStore())

    def journal(self, *turns):
        return self.memory.journal("s", *turns)

    def test_without_a_summary_every_turn_is_replayed(self):
        transcript = self.journal(A.Turn("user", "a"), A.Turn("assistant", "b"))
        items, owners = transcript.window()
        self.assertEqual([i["content"] for i in items], ["a", "b"])
        self.assertEqual(owners, transcript.turns)

    def test_a_summary_opens_the_window_and_keeps_from_its_seq(self):
        transcript = self.journal(
            A.Turn("user", "a"), A.Turn("assistant", "b"), A.Turn("user", "c"), A.Turn("assistant", "d")
        )
        self.journal(A.Turn("compact", "SUM", meta={"summary": True, "keep": {"seq": 3}}))
        items = transcript.messages()
        self.assertEqual(items[0], A.summary_message("SUM"))
        self.assertEqual([i["content"] for i in items[1:]], ["c", "d"])

    def test_compactions_are_shown_to_clients_but_never_to_the_model(self):
        transcript = self.journal(A.Turn("user", "a"), A.Turn("compact", "cleared 2 outputs"))
        self.assertEqual(transcript.messages(), [{"role": "user", "content": "a"}])
        self.assertEqual([t["kind"] for t in self.memory.replay("s")], ["prompt", "compact"])

    def test_a_summary_made_mid_run_keeps_from_the_calls_of_a_response(self):
        transcript = self.journal(
            A.Turn("user", "go", run="r1"),
            A.Turn("tool", "t1", run="r1", items=tool_pair("c1")),
            A.Turn("compact", "SUM", run="r1", meta={"summary": True, "keep": {"run": "r1", "calls": ["c2", "c3"]}}),
            A.Turn("assistant", "reading two", run="r1"),
            A.Turn("tool", "t3", run="r1", items=tool_pair("c3")),
            A.Turn("tool", "t2", run="r1", items=tool_pair("c2")),
            A.Turn("assistant", "done", run="r1"),
        )
        self.assertEqual(transcript.boundary(), 3)
        items = transcript.messages()
        self.assertEqual(items[0]["content"], A.summary_message("SUM")["content"])
        self.assertEqual(items[1]["content"], "reading two")
        self.assertEqual([i.get("call_id") for i in items[2:6]], ["c3", "c3", "c2", "c2"])

    def test_a_reference_that_resolves_to_nothing_keeps_what_follows_the_summary(self):
        transcript = self.journal(
            A.Turn("user", "a"),
            A.Turn("compact", "SUM", meta={"summary": True, "keep": {"run": "x", "calls": ["gone"]}}),
            A.Turn("user", "b"),
        )
        self.assertEqual([i["content"] for i in transcript.messages()[1:]], ["b"])

    def test_a_cleared_output_is_replayed_as_its_stub(self):
        transcript = self.journal(
            A.Turn("tool", "t", items=tool_pair("c1", "x" * 500), meta={"cleared": "[stub]"})
        )
        self.assertEqual(transcript.messages()[1]["output"], "[stub]")

    def test_the_newest_summary_wins(self):
        transcript = self.journal(
            A.Turn("user", "a"),
            A.Turn("compact", "OLD", meta={"summary": True, "keep": {"seq": 1}}),
            A.Turn("user", "b"),
            A.Turn("compact", "NEW", meta={"summary": True, "keep": {"seq": 3}}),
            A.Turn("user", "c"),
        )
        items = transcript.messages()
        self.assertEqual(items[0], A.summary_message("NEW"))
        self.assertEqual([i["content"] for i in items[1:]], ["b", "c"])


class TokenCounterTest(unittest.TestCase):
    def setUp(self):
        self.counter = A.TokenCounter(None)

    def test_text_is_estimated_from_its_utf8_size(self):
        self.assertEqual(self.counter.text("abcdefgh"), 2)
        self.assertEqual(self.counter.text("ab"), 1)
        self.assertEqual(self.counter.text(""), 0)
        self.assertEqual(self.counter.text("日本"), 2)  # 6 bytes

    def test_an_item_costs_its_framing_and_its_words(self):
        framing = A.TokenCounter.MESSAGE_TOKENS
        self.assertEqual(self.counter.item(user("abcdefgh")), framing + 2)
        call, output = tool_pair("c1", "x" * 40, name="abcd", args="abcd")
        self.assertEqual(self.counter.item(call), framing + 2)
        self.assertEqual(self.counter.item(output), framing + 10)
        self.assertEqual(self.counter.item(thought("abcd")), framing + 1)

    def test_an_image_costs_a_fixed_amount(self):
        item = {"role": "user", "content": [{"type": "input_image", "image_url": "data:..."}]}
        self.assertEqual(
            self.counter.item(item), A.TokenCounter.MESSAGE_TOKENS + A.TokenCounter.IMAGE_TOKENS
        )

    def test_counts_are_cached_by_content(self):
        calls = []
        text = self.counter.text
        self.counter.text = lambda s: (calls.append(s), text(s))[1]
        self.counter.item(user("same"))
        self.counter.item(user("same"))
        self.assertEqual(len(calls), 1)

    def test_calibration_follows_the_provider(self):
        self.assertEqual(self.counter.calibrate(1000, 1500), 1.5)
        self.assertEqual(self.counter.calibrate(1000, 1500), 1.5)
        self.assertAlmostEqual(self.counter.calibrate(1000, 1000), 1.25)
        self.assertEqual(self.counter.scaled(100), 125)
        self.assertEqual(self.counter.raw(125), 100)

    def test_calibration_ignores_noise_and_is_bounded(self):
        self.assertEqual(self.counter.calibrate(10, 1000), 1.0)
        self.assertEqual(self.counter.calibrate(1000, 0), 1.0)
        self.assertEqual(self.counter.calibrate(1000, 100000), 4.0)

    def test_a_missing_tokenizer_falls_back_to_the_estimate(self):
        counter = A.TokenCounter("no-such-encoding")
        with unittest.mock.patch.object(A.TokenCounter, "encodings", {}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(counter.text("abcdefgh"), 2)


class SafeCutTest(unittest.TestCase):
    def test_a_call_is_never_parted_from_its_output(self):
        items = [user("go"), *tool_pair("c1"), *tool_pair("c2"), assistant("done")]
        self.assertEqual(A.safe_cuts(items), [1, 3, 5])

    def test_parallel_calls_are_one_unit(self):
        items = [user("go"), *[tool_pair("a")[0], tool_pair("b")[0]], tool_pair("a")[1], tool_pair("b")[1], assistant("x")]
        self.assertEqual(A.safe_cuts(items), [1, 5])

    def test_reasoning_stays_with_what_it_led_to(self):
        items = [user("go"), thought("t"), *tool_pair("c1"), thought("u"), assistant("x")]
        self.assertEqual(A.safe_cuts(items), [1, 4])

    def test_calls_stay_with_the_message_that_announced_them(self):
        items = [user("go"), assistant("reading"), *tool_pair("c1"), user("next")]
        self.assertEqual(A.safe_cuts(items), [1, 4])

    def test_a_call_without_an_output_does_not_block_later_cuts(self):
        items = [user("go"), {"type": "web_search_call", "id": "w"}, user("next")]
        self.assertEqual(A.safe_cuts(items), [1, 2])


class ContextBudgetTest(unittest.TestCase):
    def test_the_reserve_is_a_fifth_of_the_window_within_bounds(self):
        self.assertEqual(A.ContextBudget.of(128_000).reserve, 25_600)
        self.assertEqual(A.ContextBudget.of(1_000_000).reserve, 32_768)
        self.assertEqual(A.ContextBudget.of(16_000).reserve, 4096)
        self.assertEqual(A.ContextBudget.of(4096).reserve, 2048)

    def test_the_watermarks_are_shares_of_the_usable_budget(self):
        budget = A.ContextBudget.of(10_000, 2_000, 0.8, 0.5)
        self.assertEqual((budget.usable, budget.high, budget.low), (8000, 6400, 4000))

    def test_values_are_made_sane(self):
        budget = A.ContextBudget.of(0, 0, 2.0, 3.0)
        self.assertEqual(budget.window, A.DEFAULT_CONTEXT_WINDOW)
        self.assertEqual((budget.trigger, budget.target), (1.0, 1.0))

    def test_usage_reports_the_fill(self):
        usage = A.ContextBudget.of(10_000, 2_000).usage(2000)
        self.assertEqual(usage["fill"], 0.25)
        self.assertTrue(usage["estimated"])


class CompactorTest(unittest.TestCase):
    """The tiers, on a budget small enough to count by hand (bytes / 4)."""

    def setUp(self):
        self.summaries: list[tuple[str, str, str]] = []
        # usable 1000, high 800, low 500
        self.budget = A.ContextBudget.of(2000, 1000, 0.8, 0.5)

    async def summarize(self, previous, segment, focus):
        self.summaries.append((previous, segment, focus))
        return f"S{len(self.summaries)}"

    def compactor(self, **overrides):
        options = dict(summarize=self.summarize, keep=2, summary_tokens=256, reasoning=True)
        options.update(overrides)
        return A.Compactor(A.TokenCounter(None), self.budget, **options)

    def big(self, tokens):
        return "x" * (tokens * 4)

    def test_nothing_happens_below_the_high_watermark(self):
        state = A.ContextState()
        items = [user(self.big(100)), assistant(self.big(100))]
        self.assertIsNone(run(self.compactor().compact(items, state)))
        self.assertEqual(self.compactor().view(items, state), items)

    def test_old_reasoning_goes_first_and_the_newest_response_keeps_its_own(self):
        items = [user("go"), thought(self.big(400) + "a"), *tool_pair("c1"), thought(self.big(400) + "b"), *tool_pair("c2"), thought("now"), *tool_pair("c3")]
        state = A.ContextState()
        report = run(self.compactor().compact(items, state))
        self.assertEqual(report.thoughts, 2)
        self.assertEqual(report.summarized, 0)
        sent = self.compactor().view(items, state)
        self.assertEqual(sent[1]["summary"][0]["text"], A.THOUGHT_STUB)
        self.assertEqual(sent[7]["summary"][0]["text"], "now")

    def test_reasoning_a_provider_never_sees_is_not_counted(self):
        items = [user("go"), thought(self.big(900)), *tool_pair("c1")]
        self.assertIsNone(run(self.compactor(reasoning=False).compact(items, A.ContextState())))

    def test_old_outputs_are_cleared_oldest_first_until_the_target(self):
        self.budget = A.ContextBudget.of(2000, 1000, 0.8, 0.6)  # high 800, low 600
        pairs = [tool_pair(f"c{n}", self.big(210)) for n in range(6)]
        items = [user("go"), *[i for p in pairs for i in p]]
        state = A.ContextState()
        report = run(self.compactor(keep=1).compact(items, state))
        self.assertEqual(report.cleared, ["c0", "c1", "c2", "c3"])
        self.assertEqual(report.summarized, 0)
        self.assertLessEqual(report.after, self.budget.low)
        sent = self.compactor().view(items, state)
        self.assertIn("read output cleared to save context", sent[2]["output"])
        self.assertEqual(sent[10]["output"], self.big(210))  # under the target: left alone
        self.assertEqual(sent[12]["output"], self.big(210))  # the newest: spared

    def test_the_newest_outputs_are_spared(self):
        pairs = [tool_pair(f"c{n}", self.big(300)) for n in range(4)]
        items = [user("go"), *[i for p in pairs for i in p]]
        report = run(self.compactor(keep=2).compact(items, A.ContextState()))
        self.assertNotIn("c2", report.cleared)
        self.assertNotIn("c3", report.cleared)

    def test_small_outputs_are_not_worth_clearing(self):
        items = [user(self.big(850)), *tool_pair("c0", "tiny"), *tool_pair("c1"), *tool_pair("c2")]
        report = run(self.compactor().compact(items, A.ContextState()))
        self.assertEqual(report.cleared, [])

    def test_the_oldest_prefix_is_folded_into_the_summary(self):
        items = [user(self.big(200)), assistant(self.big(200)), user(self.big(200)), assistant(self.big(200)), user("last")]
        state = A.ContextState()
        report = run(self.compactor().compact(items, state))
        self.assertEqual(report.summarized, 3)
        self.assertEqual(state.cut, 3)
        sent = self.compactor().view(items, state)
        self.assertEqual(sent[0], A.summary_message("S1"))
        self.assertEqual(sent[1:], items[3:])
        previous, segment, _ = self.summaries[0]
        self.assertEqual(previous, "")
        self.assertTrue(segment.startswith("USER: xxx"))
        self.assertIn("ASSISTANT: xxx", segment)

    def test_a_summary_is_folded_further_never_rebuilt(self):
        compactor = self.compactor()
        state = A.ContextState()
        items = [user(self.big(300)), assistant(self.big(300)), user(self.big(300)), assistant("a")]
        run(compactor.compact(items, state))
        items += [user(self.big(300)), assistant(self.big(300)), user(self.big(300)), assistant("b")]
        run(compactor.compact(items, state))
        self.assertEqual(self.summaries[1][0], "S1")
        self.assertNotIn("USER: " + self.big(300)[:10] + "x" * 1190, self.summaries[1][1][:0])
        self.assertEqual(compactor.view(items, state)[0], A.summary_message("S2"))

    def test_decisions_hold_while_the_input_grows(self):
        compactor = self.compactor()
        state = A.ContextState()
        items = [user(self.big(300)), assistant(self.big(300)), user(self.big(300)), assistant("a")]
        run(compactor.compact(items, state))
        before = compactor.view(items, state)
        items.append(user("next"))
        self.assertIsNone(run(compactor.compact(items, state)))
        self.assertEqual(compactor.view(items, state), [*before, user("next")])

    def test_the_cut_is_found_again_in_a_list_that_moved(self):
        compactor = self.compactor()
        state = A.ContextState()
        items = [user(self.big(300)), assistant(self.big(300)), user(self.big(300)), assistant("a")]
        run(compactor.compact(items, state))
        moved = [user("prepended"), *items]
        self.assertEqual(compactor.view(moved, state)[1:], items[state.cut:])

    def test_the_request_in_flight_is_kept_verbatim_once_summarised(self):
        items = [user("do the thing"), *[i for n in range(6) for i in tool_pair(f"c{n}", self.big(150))]]
        state = A.ContextState()
        report = run(self.compactor(keep=6).compact(items, state, pinned_at=0, pinned="do the thing"))
        self.assertGreater(report.summarized, 0)
        head = self.compactor().view(items, state)[0]["content"]
        self.assertIn(A.PINNED_HEADER, head)
        self.assertTrue(head.rstrip().endswith("do the thing"))

    def test_a_failing_summarizer_leaves_an_extract(self):
        async def broken(previous, segment, focus):
            raise RuntimeError("down")

        items = [user(self.big(300) + " Q"), *tool_pair("c1", self.big(300)), assistant(self.big(300)), user("last")]
        state = A.ContextState()
        with contextlib.redirect_stderr(io.StringIO()):
            report = run(self.compactor(summarize=broken, keep=1).compact(items, state))
        self.assertGreater(report.summarized, 0)
        self.assertIn("Earlier segment (no summary)", state.summary)
        self.assertIn("TOOL CALL read", state.summary)

    def test_without_a_summarizer_the_prefix_is_dropped_with_a_note(self):
        items = [user(self.big(300)), assistant(self.big(300)), user(self.big(300)), assistant("a")]
        state = A.ContextState()
        run(self.compactor(summarize=None).compact(items, state))
        self.assertIn("earlier items were dropped", state.summary)

    def test_a_large_segment_is_folded_a_chunk_at_a_time(self):
        items = [user(self.big(300)), assistant(self.big(300)), user(self.big(300)), assistant("a")]
        run(self.compactor(segment_limit=350).compact(items, A.ContextState()))
        self.assertEqual(len(self.summaries), 3)
        self.assertEqual([s[0] for s in self.summaries], ["", "S1", "S2"])

    def test_forcing_folds_everything_but_the_newest_response(self):
        items = [user("a"), assistant("b"), user("c"), assistant("d")]
        state = A.ContextState()
        report = run(self.compactor().compact(items, state, force=True, focus="the api"))
        self.assertEqual(report.reason, "forced")
        self.assertEqual(state.cut, 3)
        self.assertEqual(self.summaries[0][2], "the api")

    def test_a_giant_newest_output_is_clipped_to_fit(self):
        items = [user("go"), *tool_pair("c1", self.big(3000))]
        state = A.ContextState()
        report = run(self.compactor().compact(items, state))
        self.assertEqual(report.clipped, ["c1"])
        self.assertLessEqual(report.after, self.budget.usable)
        self.assertIn("chars omitted", self.compactor().view(items, state)[-1]["output"])

    def test_a_clipped_output_says_a_repeat_returns_the_same(self):
        items = [user("go"), *tool_pair("c1", self.big(3000))]
        state = A.ContextState()
        report = run(self.compactor().compact(items, state))
        self.assertLessEqual(report.after, self.budget.usable)
        self.assertTrue(self.compactor().view(items, state)[-1]["output"].startswith(A.CLIP_NOTE))

    def test_a_cleared_output_too_large_to_reread_says_so(self):
        compactor = self.compactor()
        self.assertIn("too large to read again whole", compactor.stub(list(tool_pair("c1")), 1, 300))
        self.assertIn("Call the tool again", compactor.stub(list(tool_pair("c1")), 1, 200))

    def test_the_marker_is_announced_before_the_slow_tier(self):
        order: list[str] = []

        async def summarize(previous, segment, focus):
            order.append("summarize")
            return "S"

        async def on_summary(report):
            order.append("announce")

        items = [user(self.big(300)), assistant(self.big(300)), user(self.big(300)), assistant("a")]
        run(self.compactor(summarize=summarize).compact(items, A.ContextState(), on_summary=on_summary))
        self.assertEqual(order, ["announce", "summarize"])

    def test_calibration_moves_the_watermarks(self):
        compactor = self.compactor()
        items = [user(self.big(350)), assistant(self.big(350))]
        self.assertIsNone(run(compactor.compact(items, A.ContextState())))
        compactor.counter.ratio = 2.0  # the provider counts twice what we estimate
        self.assertIsNotNone(run(compactor.compact(items, A.ContextState())))


class OverflowTest(unittest.TestCase):
    class BadRequest(Exception):
        status_code = 400

    def test_context_length_errors_are_recognised(self):
        self.assertTrue(A.is_overflow(self.BadRequest("This model's maximum context length is 8192 tokens")))
        self.assertTrue(A.is_overflow(self.BadRequest("prompt is too long: 210000 tokens > 200000 maximum")))
        self.assertFalse(A.is_overflow(self.BadRequest("invalid tool schema")))
        self.assertEqual(A.classify_error(self.BadRequest("context_length_exceeded")), A.ErrorKind.CONTEXT)

    def test_a_rate_limit_on_tokens_is_not_an_overflow(self):
        class RateLimitError(Exception):
            status_code = 429

        self.assertFalse(A.is_overflow(RateLimitError("too many tokens per minute")))

    def test_the_window_an_error_names_is_read(self):
        self.assertEqual(A.overflow_window("maximum context length is 8,192 tokens"), 8192)
        self.assertEqual(A.overflow_window("prompt is too long: 210000 tokens > 200000 maximum"), 200000)
        self.assertEqual(A.overflow_window("context window of 32768 exceeded"), 32768)
        self.assertEqual(A.overflow_window("nope"), 0)


class WindowProbeTest(unittest.TestCase):
    """What a provider says about its window (the probe is stubbed module wide)."""

    def probe(self, data):
        pool = A.ModelPool()

        class Models:
            async def list(self):
                return types.SimpleNamespace(
                    data=[types.SimpleNamespace(model_dump=functools.partial(dict, d)) for d in data]
                )

        pool.client = lambda spec: types.SimpleNamespace(models=Models())

        async def no_ollama(spec):
            return 0

        pool.probe_ollama = no_ollama
        return run(_probe_window(pool, A.ModelSpec("leader", "m/x", "http://url", "")))

    def test_an_openrouter_listing_gives_the_context_length(self):
        self.assertEqual(
            self.probe([{"id": "other", "context_length": 1}, {"id": "m/x", "context_length": 163840, "top_provider": {"context_length": 64000}}]),
            163840,
        )

    def test_a_vllm_listing_gives_the_max_model_len(self):
        self.assertEqual(self.probe([{"id": "m/x", "max_model_len": 32768}]), 32768)

    def test_a_listing_without_the_model_says_nothing(self):
        self.assertEqual(self.probe([{"id": "other", "context_length": 1}]), 0)

    def test_the_window_is_asked_once_and_failures_are_unknown(self):
        pool = A.ModelPool()
        calls = []

        async def probe(spec):
            calls.append(spec)
            raise RuntimeError("offline")

        pool.probe_window = probe
        spec = A.ModelSpec("leader", "m", "http://url", "")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run(pool.window(spec)), 0)
            self.assertEqual(run(pool.window(spec)), 0)
        self.assertEqual(len(calls), 1)
        pool.learn_window(spec, 4096)
        self.assertEqual(run(pool.window(spec)), 4096)


class ContextRunTest(unittest.TestCase):
    """In-run compaction through the real SDK runner, driven by a scripted model."""

    def setUp(self):
        from agents import function_tool

        self.summaries: list[tuple[str, str, str]] = []

        @function_tool
        def read(path: str) -> str:
            """Read a file."""
            return "x" * 2400  # ~600 tokens

        @function_tool
        def write(path: str, text: str) -> str:
            """Write a file."""
            return "ok"

        self.tool = read
        self.writer = write

    def engine(self, steps, **overrides):
        from agents.testing import ScriptedModel

        model = ScriptedModel(steps)
        test = self

        class Scripted(A.Engine):
            def build_model(self, config, role=A.LEADER_ROLE):
                return model

            def build_summarizer(self, config):
                async def summarize(previous, segment, focus=""):
                    test.summaries.append((previous, segment, focus))
                    return f"SUMMARY {len(test.summaries)}"

                return summarize

        # compaction alone: no budget note on the input
        options = dict(env=False, config_file=False, context_window=6000, context_reserve=1000, context_keep=2, plan_enabled=False)
        options.update(overrides)
        engine = Scripted(**options)
        self.addCleanup(engine.close)
        return engine, model

    def reads(self, count):
        from agents.testing import assistant_message, function_call

        steps = [[function_call("read", {"path": f"f{n}"}, call_id=f"c{n}")] for n in range(count)]
        return steps + [[assistant_message("done")]]

    def test_a_long_tool_loop_is_compacted_as_it_goes(self):
        engine, model = self.engine(self.reads(12))
        seen: list[A.Event] = []
        engine.events.on(A.EventType.ALL, seen.append)
        result = run(engine.run("read them all", tools=[self.tool]))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output, "done")
        budget = result.context.budget
        sizes = [result.context.counter.items(call.input) for call in model.calls]
        self.assertLessEqual(max(sizes), budget.high)
        self.assertTrue(result.context.compactions)
        kinds = [e.type for e in seen]
        self.assertIn(A.EventType.CONTEXT_COMPACT, kinds)
        self.assertIn(A.EventType.CONTEXT_USAGE, kinds)
        markers = [e for e in seen if e.type == A.EventType.BLOCK_END and e.data.get("kind") == "compact"]
        self.assertTrue(markers)

    def test_the_prefix_only_changes_at_a_compaction(self):
        engine, model = self.engine(self.reads(12))
        result = run(engine.run("read them all", tools=[self.tool]))
        inputs = [call.input for call in model.calls]
        changes = sum(
            1 for before, after in zip(inputs, inputs[1:]) if after[: len(before)] != before
        )
        self.assertEqual(changes, len(result.context.compactions))

    def test_the_model_keeps_its_request_and_its_newest_step(self):
        engine, model = self.engine(self.reads(12), context_keep=1, context_target=0.3)
        run(engine.run("read them all", tools=[self.tool]))
        last = model.calls[-1].input
        self.assertIn("read them all", json.dumps(last))
        self.assertEqual(last[-1]["type"], "function_call_output")
        self.assertEqual(last[-1]["call_id"], "c11")

    def writes(self, count):
        from agents.testing import assistant_message, function_call

        steps = [
            [function_call("write", {"path": f"f{n}", "text": "z" * 2000}, call_id=f"w{n}")]
            for n in range(count)
        ]
        return steps + [[assistant_message("done")]]

    def test_clearing_outputs_spares_a_summary(self):
        engine, model = self.engine(self.reads(16), context_keep=1, context_target=0.3)
        result = run(engine.run("read them all", tools=[self.tool]))
        self.assertTrue(result.context.compactions)
        self.assertEqual(self.summaries, [])

    def test_summaries_are_anchored_on_the_previous_one(self):
        engine, model = self.engine(self.writes(24), context_target=0.3)
        result = run(engine.run("write them all", tools=[self.writer]))
        self.assertTrue(result.ok, result.error)
        self.assertGreaterEqual(len(self.summaries), 2)
        self.assertEqual(self.summaries[1][0], "SUMMARY 1")
        self.assertIn("TOOL CALL write", self.summaries[0][1])
        sizes = [result.context.counter.items(call.input) for call in model.calls]
        self.assertLessEqual(max(sizes), result.context.budget.high)

    def test_the_provider_usage_calibrates_the_count(self):
        from agents.usage import Usage
        from agents.testing import ModelStep, assistant_message, function_call

        steps = [
            ModelStep(output=[function_call("read", {"path": "a"}, call_id="c1")], usage=Usage(requests=1, input_tokens=5000, output_tokens=10)),
            ModelStep(output=[assistant_message("done")], usage=Usage(requests=1, input_tokens=5000, output_tokens=10)),
        ]
        engine, model = self.engine(steps, context_window=200_000)
        result = run(engine.run("read", tools=[self.tool]))
        self.assertTrue(result.ok, result.error)
        self.assertGreater(result.context.counter.ratio, 1.0)

    def test_a_context_length_error_compacts_harder_and_retries(self):
        from agents.testing import ModelStep, assistant_message, function_call

        class BadRequest(Exception):
            status_code = 400

        steps = [[function_call("read", {"path": f"f{n}"}, call_id=f"c{n}")] for n in range(4)]
        steps += [ModelStep.raise_error(BadRequest("This model's maximum context length is 3000 tokens")), [assistant_message("done")]]
        engine, model = self.engine(steps, context_window=50_000)
        result = run(engine.run("read", tools=[self.tool]))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.context.budget.window, 3000)
        self.assertEqual(result.context.compactions[-1].reason, "overflow")
        spec = engine.config.model_spec(A.LEADER_ROLE)
        self.assertEqual(run(engine.models.window(spec)), 3000)
        self.assertLess(len(model.calls[-1].input), len(model.calls[-2].input))

    def test_compaction_can_be_switched_off(self):
        engine, model = self.engine(self.reads(3), context_trigger=0)
        result = run(engine.run("read", tools=[self.tool]))
        self.assertIsNone(result.context)
        self.assertEqual(len(model.calls[-1].input), 1 + 2 * 3)


class SliceTest(unittest.TestCase):
    """Work slicing: measured speed, the slice budget, the note and the plan."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = A.Workspace(self.root, shell_timeout=10)
        self.clock = [0.0]

    def context(self, speed=None, **options):
        compactor = A.Compactor(A.TokenCounter(None), A.ContextBudget.of(100_000, 20_000))
        options = {"stall": 600, "slice_stall": 0.5, "clock": lambda: self.clock[0], **options}
        return A.ContextRun(compactor, throughput=speed or A.Throughput(), **options)

    def call(self, context, start, first, end, read, wrote=100, cached=0):
        """One model call as the stream reports it, on the fake clock."""
        self.clock[0] = start
        context.called_at, context.first_at = start, None  # as `__call__` stamps it
        self.clock[0] = first
        context.first_token()
        self.clock[0] = end
        return context.observe(read, wrote, cached)

    def planner(self, slice_budget=None, store=None):
        limits = (lambda: slice_budget) if slice_budget is not None else None
        store = store if store is not None else A.LruCache()  # an empty cache is falsy
        return A.Planner(self.ws, A.TokenCounter(None), limits, store, "s")

    def write(self, name, size):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * size, encoding="utf-8")
        return path

    def test_throughput_averages_timed_calls_and_skips_noise(self):
        speed = A.Throughput()
        speed.observe(100, 1.0)  # too few tokens: the round trip, not the model
        self.assertEqual(speed.prefill, 0.0)
        speed.observe(1000, 10.0, 640, 8.0)
        speed.observe(3000, 10.0)
        self.assertEqual(speed.prefill, 200.0)
        self.assertEqual(speed.decode, 80.0)
        self.assertEqual(speed.samples, 2)
        self.assertEqual(speed.tokens(10), 2000)
        self.assertEqual(speed.seconds(400), 2.0)
        self.assertEqual(A.Throughput().tokens(10), 0)

    def test_a_budget_capped_by_time_keeps_its_reserve(self):
        budget = A.ContextBudget.of(100_000, 20_000)
        capped = budget.capped(30_000)
        self.assertEqual((capped.usable, capped.reserve), (30_000, 20_000))
        self.assertEqual((capped.trigger, capped.target), (budget.trigger, budget.target))
        self.assertIs(budget.capped(0), budget)
        self.assertIs(budget.capped(200_000), budget)

    def test_the_slice_is_a_share_of_the_smaller_limit(self):
        self.assertEqual(A.SliceBudget(80_000, 0, 0.25).slice, 20_000)
        timed = A.SliceBudget(80_000, 40_000, 0.25)
        self.assertEqual((timed.limit, timed.slice), (40_000, 10_000))

    def test_a_slow_model_caps_the_budget_to_what_it_reads_in_time(self):
        speed = A.Throughput()
        context = self.context(speed)
        self.call(context, 0.0, 0.5, 1.0, 20_000, wrote=0)  # first call of a run: not timed
        self.assertEqual(speed.samples, 0)
        self.call(context, 2.0, 52.0, 53.0, 25_000, wrote=0)  # 5k new tokens in 50 s
        self.assertEqual(speed.prefill, 100.0)
        # 100 tokens/s x 600 s x 0.5 = 30k: what a cold read gets done in time
        # (nothing written yet, so no time is left for a reply)
        self.assertEqual(context.budget.usable, 30_000)
        self.assertEqual(context.base.usable, 80_000)
        self.assertEqual(context.limits().slice, 7_500)

    def test_after_a_compaction_the_whole_input_is_timed(self):
        speed = A.Throughput()
        context = self.context(speed)
        context.rebuilt = True
        self.call(context, 0.0, 40.0, 41.0, 20_000)
        self.assertEqual(speed.prefill, 500.0)

    def test_a_reply_sent_whole_is_not_timed(self):
        speed = A.Throughput()
        context = self.context(speed)
        context.rebuilt = True
        self.call(context, 0.0, 50.0, 50.01, 20_000, wrote=500)  # 50k tokens/s: one piece
        self.assertEqual(speed.samples, 0)

    def test_a_slow_read_and_a_fast_write_are_both_timed(self):
        speed = A.Throughput()
        context = self.context(speed)
        context.rebuilt = True
        self.call(context, 0.0, 200.0, 201.0, 20_000, wrote=100)
        self.assertEqual((speed.prefill, speed.decode), (100.0, 100.0))

    def test_a_short_reply_sent_whole_is_not_timed(self):
        speed = A.Throughput()
        context = self.context(speed)
        context.rebuilt = True
        self.call(context, 0.0, 20.0, 20.001, 2_000, wrote=20)  # a held back tool call
        self.assertEqual(speed.samples, 0)

    def test_one_odd_call_does_not_move_the_speed(self):
        speed = A.Throughput()
        for first in (10.0, 10.0, 10.0, 200.0):  # the last: a prompt cache the server dropped
            speed.observe(1000, first)
        self.assertEqual(speed.prefill, 100.0)
        for _ in range(A.Throughput.WINDOW):
            speed.observe(1000, 20.0)  # a lasting change wins once it is the majority
        self.assertEqual(speed.prefill, 50.0)
        self.assertEqual(len(speed.reads), A.Throughput.WINDOW)

    def test_the_cache_the_provider_reports_says_what_was_read(self):
        speed = A.Throughput()
        context = self.context(speed)
        self.call(context, 0.0, 10.0, 11.0, 20_000, cached=15_000)  # even a first call
        self.assertEqual(speed.prefill, 500.0)

    def test_a_call_after_a_long_quiet_is_not_timed(self):
        speed = A.Throughput()
        context = self.context(speed)
        context.rebuilt = True
        self.call(context, 0.0, 10.0, 11.0, 5_000)
        context.rebuilt = True
        self.call(context, 11.0 + A.ContextRun.IDLE + 1, 600.0, 601.0, 5_000)  # reloaded first
        self.assertEqual(speed.samples, 1)

    def test_the_page_of_a_read_follows_the_capped_budget(self):
        engine = A.Engine(env=False, config_file=False)
        self.addCleanup(engine.close)
        result = A.RunResult()
        limit = engine.page_limit(engine.config, result, 80_000)
        self.assertEqual(limit(), 80_000)  # no context yet
        result.context = self.context(A.Throughput(prefill=100.0))  # capped to 30k tokens
        self.assertEqual(limit(), 30_000)
        fixed = engine.config.merge(read_output_limit=5000)
        self.assertEqual(engine.page_limit(fixed, result, 5000), 5000)

    def test_the_time_cap_can_be_turned_off(self):
        context = self.context(A.Throughput(prefill=100.0), slice_stall=0)
        self.assertEqual(context.budget.usable, 80_000)

    def test_a_measured_model_is_capped_from_the_first_call_of_a_run(self):
        context = self.context(A.Throughput(prefill=100.0))
        self.assertEqual(context.budget.usable, 30_000)

    def test_the_cap_never_leaves_less_than_a_floor(self):
        context = self.context(A.Throughput(prefill=1.0))
        self.assertEqual(context.budget.usable, A.ContextRun.TIMED_FLOOR)

    def test_the_cap_leaves_time_to_write_the_longest_recent_reply(self):
        speed = A.Throughput(prefill=100.0, decode=10.0, replies=[200, 1000])
        context = self.context(speed)
        # 300 s of the stall guard, 100 s of it writing 1k tokens: 200 s to read
        self.assertEqual(context.budget.usable, 20_000)
        self.assertEqual(speed.readable(300.0), 20_000)

    def test_a_reply_takes_at_most_half_of_the_time(self):
        context = self.context(A.Throughput(prefill=100.0, decode=1.0, replies=[1000]))
        self.assertEqual(context.budget.usable, 15_000)

    def test_a_reply_is_reserved_no_longer_than_the_reserve(self):
        speed = A.Throughput(prefill=100.0, decode=1000.0, replies=[50_000])
        self.assertEqual(speed.readable(300.0), 25_000)
        context = self.context(speed)  # the reserve: 20k tokens, 20 s to write
        self.assertEqual(context.budget.usable, 28_000)

    def test_an_unmeasured_write_speed_reserves_nothing(self):
        context = self.context(A.Throughput(prefill=100.0, replies=[5000]))
        self.assertEqual(context.budget.usable, 30_000)

    def test_a_reply_sent_whole_still_sizes_the_reservation(self):
        speed = A.Throughput(prefill=100.0, decode=10.0)
        context = self.context(speed)
        self.assertEqual(context.budget.usable, 30_000)
        context.rebuilt = True
        self.call(context, 0.0, 30.0, 30.01, 3_000, wrote=1000)  # a tool call held back whole
        self.assertEqual(speed.samples, 0)  # not timed
        self.assertEqual(speed.reply, 1000)
        self.assertEqual(context.budget.usable, 20_000)  # but sized
        self.assertEqual(speed.to_dict()["reply"], 1000)

    def test_only_the_newest_replies_are_kept(self):
        speed = A.Throughput()
        speed.wrote(9000)
        for _ in range(A.Throughput.WINDOW):
            speed.wrote(10)
        self.assertEqual(speed.reply, 10)
        speed.wrote(0)
        self.assertEqual(len(speed.replies), A.Throughput.WINDOW)

    def test_a_worker_budget_leaves_time_to_write_too(self):
        engine = A.Engine(env=False, config_file=False, context_window=100_000, context_reserve=20_000)
        self.addCleanup(engine.close)
        engine.speeds[(engine.config.inference_url, engine.config.inference_llm)] = A.Throughput(
            prefill=100.0, decode=10.0, replies=[1000]
        )
        self.assertEqual(run(engine.worker_budget(engine.config)), 10_000)  # half of 20k

    def test_the_usage_carries_the_slice_and_the_speeds(self):
        posted = []

        async def publish(event_type, **data):
            posted.append((event_type, data))

        context = self.context(A.Throughput(prefill=100.0, decode=20.0), publish=publish)
        run(context.fit([{"role": "user", "content": "hi"}]))
        usage = [data for kind, data in posted if kind == A.EventType.CONTEXT_USAGE][-1]
        self.assertEqual((usage["prefill"], usage["decode"]), (100.0, 20.0))
        self.assertEqual(usage["slice"], 7_500)  # a quarter of the 30k the model reads in time
        self.assertEqual(usage["usable"], context.budget.usable)
        bare = self.context(A.Throughput())
        self.assertNotIn("prefill", bare.usage(10))

    def test_the_note_says_how_fast_the_model_writes_once_measured(self):
        context = self.context(A.Throughput(prefill=100.0, decode=20.0))
        self.assertIn("the model reads ~100 and writes ~20 tokens/s", context.note_text())

    def test_the_note_closes_the_prompt_or_follows_a_tool_output(self):
        prompt = [{"role": "user", "content": "do it"}]
        merged = A.ContextRun.with_note(prompt, "NOTE")
        self.assertEqual(merged, [{"role": "user", "content": "do it\n\nNOTE"}])
        self.assertEqual(prompt[0]["content"], "do it")  # the input itself is left alone
        output = [{"type": "function_call_output", "call_id": "c1", "output": "ok"}]
        self.assertEqual(A.ContextRun.with_note(output, "NOTE")[-1], {"role": "user", "content": "NOTE"})

    def test_the_note_says_the_budget_and_the_plan(self):
        plan = A.Plan([A.PlanStep("Read", status="done"), A.PlanStep("Write", status="active")])
        context = self.context(A.Throughput(prefill=100.0), plan=lambda: plan)
        note = context.note_text()
        self.assertTrue(note.startswith(A.ContextRun.NOTE_HEADER))
        self.assertIn("of 30k tokens; one step may read up to 7.5k", note)
        self.assertIn("~100 tokens/s", note)
        self.assertIn("[plan] 1/2 done; now 2. Write", note)

    def test_a_plan_is_measured_and_kept_for_the_session(self):
        self.write("a.md", 4000)
        store = A.LruCache()
        planner = self.planner(A.SliceBudget(100_000), store)
        text = run(planner.update([
            {"title": "Read", "inputs": ["a.md"], "status": "active"},
            {"title": "Write", "inputs": ["out.md"], "status": "pending"},
        ]))
        self.assertIn("Plan: 0 of 2 done; one step may read up to 25k tokens", text)
        self.assertIn("1. [>] Read (1.0k: a.md)", text)
        self.assertIn("2. [ ] Write (not found yet: out.md)", text)
        plan = store.get("s")
        self.assertEqual(plan.steps[0].tokens, 1000)
        self.assertEqual(plan.current().title, "Read")

    def test_a_step_over_the_slice_is_refused_then_kept_when_sent_back(self):
        self.write("big.md", 8000)  # 2k tokens, over a 1k slice
        store = A.LruCache()
        planner = self.planner(A.SliceBudget(4000), store)
        steps = [{"title": "Read all", "inputs": ["big.md"], "status": "pending"}]
        refused = run(planner.update(steps))
        self.assertIn("Plan not kept", refused)
        self.assertIn("1. Read all: ~2.0k (big.md 2.0k)", refused)
        self.assertIsNone(store.get("s"))
        kept = run(planner.update(steps))  # a step that cannot be split is not refused for ever
        self.assertIn("reads more than one slice", kept)
        self.assertIsNotNone(store.get("s"))

    def test_done_steps_are_not_measured_or_refused(self):
        self.write("big.md", 8000)
        planner = self.planner(A.SliceBudget(4000))
        text = run(planner.update([{"title": "Read all", "inputs": ["big.md"], "status": "done"}]))
        self.assertIn("Plan: 1 of 1 done", text)

    def test_inputs_may_be_globs_directories_and_line_ranges(self):
        self.write("notes/a.md", 400)
        self.write("notes/b.md", 800)
        (self.root / "lines.txt").write_text("aaaa\nbbbb\ncccc\ndddd\n", encoding="utf-8")
        planner = self.planner()
        self.assertEqual(planner.measure("notes/*.md"), 300)
        self.assertEqual(planner.measure("notes"), 300)
        self.assertEqual(planner.measure("lines.txt:2-3"), 3)  # "bbbb\ncccc\n": 10 bytes
        self.assertIsNone(planner.measure("missing.md"))

    def test_an_input_outside_the_workspace_is_refused(self):
        with self.assertRaises(A.WorkspaceError):
            self.planner().measure("../elsewhere.md")

    def test_a_glob_may_not_leave_the_workspace_or_reveal_what_is_outside(self):
        planner = self.planner()
        for pattern in ("../*", str(self.root.parent / "*")):
            with self.assertRaises(A.WorkspaceError) as caught:
                planner.measure(pattern)
            self.assertIn("must stay inside the workspace", str(caught.exception))

    def test_a_root_with_glob_characters_is_not_a_pattern(self):
        (self.root / "proj[1]").mkdir()
        ws = A.Workspace(self.root / "proj[1]", shell_timeout=10)
        (ws.root / "a.md").write_text("x" * 400, encoding="utf-8")
        planner = A.Planner(ws, A.TokenCounter(None), None, A.LruCache(), "s")
        self.assertEqual(planner.measure("*.md"), 100)

    def test_a_huge_line_range_is_counted_only_in_part(self):
        self.write("huge.txt", 1024 * 1024)  # one line of 1 MiB
        planner = self.planner()
        self.assertEqual(planner.measure("huge.txt:1-1"), 262_144)  # estimated past the cap
        self.assertEqual(planner.spare, A.Planner.COUNT_TOTAL - A.Planner.COUNT_BYTES)

    def test_an_empty_plan_clears_it(self):
        store = A.LruCache()
        planner = self.planner(store=store)
        run(planner.update([{"title": "Read", "inputs": [], "status": "pending"}]))
        self.assertEqual(run(planner.update([])), "Plan cleared.")
        self.assertIsNone(store.get("s"))

    def test_the_plan_tool_is_built_only_with_a_planner(self):
        names = lambda tools: {tool.name for tool in tools}
        registry = A.ToolRegistry(shell=False)
        self.assertNotIn("plan", names(registry.build(self.ws)))
        self.assertIn("plan", names(registry.build(self.ws, planner=self.planner())))
        engine = A.Engine(env=False, config_file=False, plan_enabled=False)
        self.addCleanup(engine.close)
        self.assertIsNone(engine.build_planner(engine.config, A.RunResult(), self.ws))

    def test_the_plan_schema_is_inline(self):
        schema = A.ToolRegistry(defaults=False)._build_plan_tool(self.planner()).params_json_schema
        self.assertNotIn("$ref", json.dumps(schema))
        item = schema["properties"]["steps"]["items"]
        self.assertEqual(item["required"], ["title", "inputs", "status"])
        self.assertEqual(item["properties"]["status"]["enum"], ["pending", "active", "done"])

    def scripted(self, steps, **overrides):
        from agents.testing import ScriptedModel

        model = ScriptedModel(steps)

        class Scripted(A.Engine):
            def build_model(self, config, role=A.LEADER_ROLE):
                return model

        options = dict(env=False, config_file=False, context_window=6000, context_reserve=1000, context_tokenizer="", context_summary=False, shell_enabled=False)
        options.update(overrides)
        engine = Scripted(**options)
        self.addCleanup(engine.close)
        return engine, model

    @staticmethod
    def unnoted(items):
        """`items` without the budget note the harness ended them with."""
        last = items[-1]
        content = last.get("content") if isinstance(last, dict) else None
        if not isinstance(content, str) or A.ContextRun.NOTE_HEADER not in content:
            return items
        if content.startswith(A.ContextRun.NOTE_HEADER):
            return items[:-1]
        return [*items[:-1], {**last, "content": content.split("\n\n" + A.ContextRun.NOTE_HEADER)[0]}]

    def test_every_call_ends_with_the_note_and_the_prefix_holds_otherwise(self):
        from agents.testing import assistant_message, function_call

        self.write("f.md", 2000)
        steps = [[function_call("read_text_file", {"path": "f.md"}, call_id=f"c{n}")] for n in range(8)]
        engine, model = self.scripted(steps + [[assistant_message("done")]])
        result = run(engine.run("read it", workspace=self.root))
        self.assertTrue(result.ok, result.error)
        inputs = [call.input for call in model.calls]
        self.assertTrue(all(A.ContextRun.NOTE_HEADER in json.dumps(i[-1]) for i in inputs))
        self.assertEqual(inputs[0][-1]["content"].split("\n\n")[0], "read it")
        bare = [self.unnoted(i) for i in inputs]
        changes = sum(1 for before, after in zip(bare, bare[1:]) if after[: len(before)] != before)
        self.assertEqual(changes, len(result.context.compactions))

    def test_the_model_plans_and_the_note_recites_the_plan(self):
        from agents.testing import assistant_message, function_call

        self.write("a.md", 2000)
        plan = {"steps": [
            {"title": "Read a", "inputs": ["a.md"], "status": "active"},
            {"title": "Write b", "inputs": [], "status": "pending"},
        ]}
        engine, model = self.scripted([[function_call("plan", plan, call_id="p1")], [assistant_message("done")]])
        result = run(engine.run("plan it", workspace=self.root, session_id="s1"))
        self.assertTrue(result.ok, result.error)
        answer = next(i for i in model.calls[1].input if i.get("type") == "function_call_output")
        self.assertIn("1. [>] Read a (500: a.md)", answer["output"])
        self.assertIn("[plan] 0/2 done; now 1. Read a", model.calls[1].input[-1]["content"])
        self.assertEqual(engine.plans.get("s1").steps[1].title, "Write b")


class SizingTest(unittest.TestCase):
    """The sizing question: a large plan, or a split that depends on intent, asks once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = A.Workspace(self.root, shell_timeout=10)
        self.store = A.LruCache()
        self.questions: list[tuple[str, list[str]]] = []
        self.answers: list[str] = []

    async def ask(self, question, options):
        self.questions.append((question, options))
        return self.answers.pop(0) if self.answers else "Go ahead"

    def planner(self, slices=1, minutes=0.0, cost=0.0, price=0.0, slice_budget=4000):
        limits = A.AskLimits(slices, minutes, cost, price)
        budget = A.SliceBudget(slice_budget)  # one slice: 1k tokens
        return A.Planner(self.ws, A.TokenCounter(None), lambda: budget, self.store, "s", ask=limits)

    def steps(self, count=3, size=2000):
        for n in range(count):
            (self.root / f"p{n}.md").write_text("x" * size, encoding="utf-8")
        return [{"title": f"Read {n}", "inputs": [f"p{n}.md"], "status": "pending"} for n in range(count)]

    def test_an_estimate_says_slices_minutes_and_cost(self):
        plan = A.Plan([
            A.PlanStep("Read", sizes=[("a.md", 3000)]),
            A.PlanStep("Done", status="done", sizes=[("b.md", 9000)]),
        ])
        estimate = plan.estimate(A.SliceBudget(4000), A.Throughput(prefill=10.0), price=2.0)
        self.assertEqual((estimate.tokens, estimate.slices, estimate.seconds), (3000, 3, 300.0))
        self.assertAlmostEqual(estimate.cost, 0.006)
        self.assertEqual(
            estimate.describe(),
            "~3.0k tokens to read in 3 slices, ~5 min at ~10 tokens/s, ~0.01 to read once",
        )
        self.assertEqual(A.Plan([A.PlanStep("Read", sizes=[("a.md", 50)])]).estimate().describe(), "~50 tokens to read")

    def test_thresholds_are_passed_strictly_and_zero_is_off(self):
        estimate = A.Estimate(tokens=9000, slices=9, seconds=700, cost=2.0)
        self.assertEqual(A.AskLimits(8, 10, 1.0).passed(estimate), ["slices", "minutes", "cost"])
        self.assertEqual(A.AskLimits(9, 0, 0).passed(estimate), [])
        limits = A.AskLimits.of(A.AgentConfig(cost_input=3.0))
        self.assertEqual(limits, A.AskLimits(8, 10.0, 1.0, 3.0))

    def test_a_large_plan_asks_once_and_the_answer_comes_back(self):
        planner = self.planner()
        steps = self.steps()  # 1.5k tokens: two slices of 1k
        text = run(planner.update(steps, ask=self.ask))
        self.assertEqual(len(self.questions), 1)
        question, options = self.questions[0]
        self.assertTrue(question.startswith("This plan is large: 3 steps, ~1.5k tokens to read in 2 slices"), question)
        self.assertTrue(question.endswith("Go ahead?"))
        self.assertEqual(options, [A.ASK_GO, A.ASK_STOP])
        self.assertIn("The user answered: Go ahead", text)
        self.assertTrue(self.store.get("s").asked)
        steps[0]["status"] = "done"
        again = run(planner.update(steps, ask=self.ask))  # progress: never asked twice
        self.assertEqual(len(self.questions), 1)
        self.assertNotIn("The user", again)
        for step in steps:
            step["status"] = "done"
        run(planner.update(steps, ask=self.ask))
        run(planner.update(self.steps(count=4), ask=self.ask))  # finished: the next plan is new
        self.assertEqual(len(self.questions), 2)

    def test_stop_drops_the_plan(self):
        self.answers.append("stop")
        text = run(self.planner().update(self.steps(), ask=self.ask))
        self.assertTrue(text.startswith("Plan dropped"), text)
        self.assertIsNone(self.store.get("s"))

    def test_the_models_question_is_put_with_the_estimate(self):
        self.answers.append("Stop")  # one of the model's own options: not a stop
        planner = self.planner(slices=0)
        text = run(planner.update(
            self.steps(count=1), question="By chapter or by topic?", options=["chapter", "Stop"], ask=self.ask
        ))
        question, options = self.questions[0]
        self.assertEqual(question, "Plan: 1 step, ~500 tokens to read in 1 slice.\n\nBy chapter or by topic?")
        self.assertEqual(options, ["chapter", "Stop"])
        self.assertIn("The user answered: Stop", text)
        self.assertIsNotNone(self.store.get("s"))

    def test_a_small_plan_asks_nothing(self):
        text = run(self.planner(slices=8).update(self.steps(), ask=self.ask))
        self.assertEqual(self.questions, [])
        self.assertFalse(self.store.get("s").asked)
        self.assertNotIn("The user", text)

    def test_nobody_to_ask_leaves_the_split_to_the_model(self):
        text = run(self.planner().update(self.steps(), question="By chapter?"))
        self.assertIn("The user was not asked", text)
        self.assertFalse(self.store.get("s").asked)

    def test_a_refused_plan_asks_nothing(self):
        steps = self.steps(count=1, size=8000)  # 2k tokens, over the 1k slice
        text = run(self.planner().update(steps, ask=self.ask))
        self.assertIn("Plan not kept", text)
        self.assertEqual(self.questions, [])

    def test_the_question_takes_over_the_block_of_the_plan_call(self):
        from agents.testing import assistant_message, function_call

        for n in range(3):
            (self.root / f"p{n}.md").write_text("x" * 4000, encoding="utf-8")  # 1k tokens each
        plan = {"steps": [{"title": f"Read {n}", "inputs": [f"p{n}.md"], "status": "pending"} for n in range(3)]}
        engine, model = SliceTest.scripted(
            self, [[function_call("plan", plan, call_id="p1")], [assistant_message("done")]],
            ask_enabled=True, plan_ask_slices=1,
        )
        seen = []

        def answer(event):
            seen.append(event)
            engine.answer(event.data["id"], "Go ahead", session_id=event.session_id)

        engine.events.on(A.EventType.QUESTION_START, answer)
        result = run(engine.run("plan it", workspace=self.root, session_id="s1"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(seen[0].data["id"], result.tools[0].id)
        self.assertIn("This plan is large", seen[0].data["question"])
        output = next(i for i in model.calls[1].input if i.get("type") == "function_call_output")
        self.assertIn("The user answered: Go ahead", output["output"])


class PlanJournalTest(unittest.TestCase):
    """A plan is written through to the store: resumed or restarted, it goes on."""

    def plan(self):
        return A.Plan(
            [
                A.PlanStep("Read", ["a.md"], "done", [("a.md", 500)]),
                A.PlanStep("Write", ["b.md"], "active", [], ["b.md"]),
            ],
            asked=True,
        )

    def test_a_plan_round_trips(self):
        plan = self.plan()
        self.assertEqual(A.Plan.from_dict(json.loads(json.dumps(plan.to_dict()))), plan)
        self.assertIsNone(A.Plan.from_dict({"steps": [{"title": ""}, "junk"]}))
        self.assertIsNone(A.Plan.from_dict(None))
        odd = A.Plan.from_dict({"steps": [{"title": "x", "status": "weird", "sizes": [["a", "b"]]}]})
        self.assertEqual((odd.steps[0].status, odd.steps[0].sizes), ("pending", []))

    def test_a_plan_book_writes_through(self):
        store = A.MemoryStore()
        book = A.PlanBook(store)
        book.set("s", self.plan())
        self.assertEqual(A.PlanBook(store).get("s"), self.plan())  # another process
        book.clear()  # memory only
        self.assertEqual(book.get("s"), self.plan())
        book.delete("s")
        self.assertIsNone(book.get("s"))
        self.assertIsNone(store.get(A.PlanBook.NAMESPACE, "s"))

    def test_marking_a_step_is_written_through(self):
        store = A.MemoryStore()
        book = A.PlanBook(store)
        book.set("s", self.plan())
        planner = A.Planner(A.Workspace(tempfile.gettempdir()), A.TokenCounter(None), None, book, "s")
        self.assertIsNotNone(planner.mark(2, "done", "Write"))
        self.assertEqual(store.get(A.PlanBook.NAMESPACE, "s")["steps"][1]["status"], "done")
        self.assertIsNone(planner.mark(2, "pending", "Another"))  # replaced meanwhile
        self.assertIsNone(planner.mark(3, "done"))
        self.assertIsNone(planner.mark(1, "finished"))

    def test_a_restarted_agent_continues_the_plan(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, True))
        options = dict(console=False, config_file=False, env=False, workspace_root=root, session_durable=True)
        first = A.Agent(store=A.SqliteStore(root / "agent.db"), **options)
        session = first.sessions.create()
        first.plans.set(session.id, self.plan())
        first.close()
        second = A.Agent(store=A.SqliteStore(root / "agent.db"), **options)
        self.addCleanup(lambda: (second.sessions.close_all(destroy=True), second.close()))
        self.assertEqual(second.plans.get(session.id), self.plan())
        context = run(second.build_context(second.config, A.RunResult(session_id=session.id)))
        self.assertIn("[plan] 1/2 done; now 2. Write", context.note_text())

    def test_a_closed_or_forgotten_session_drops_its_plan(self):
        agent = quiet_agent()
        self.addCleanup(agent.close)
        closed, forgotten = agent.sessions.create(), agent.sessions.create()
        for session in (closed, forgotten):
            agent.plans.set(session.id, self.plan())
        agent.sessions.close(closed.id)
        run(agent.commands.invoke("/forget", session_id=forgotten.id))
        self.assertEqual(agent.store.list(A.PlanBook.NAMESPACE), [])

    def test_a_restart_forgets_plans_without_a_session(self):
        store = A.MemoryStore()
        store.set(A.PlanBook.NAMESPACE, "ghost", self.plan().to_dict())
        agent = quiet_agent(store=store)
        self.addCleanup(agent.close)
        self.assertIsNone(store.get(A.PlanBook.NAMESPACE, "ghost"))

    def test_resume_recites_the_open_plan(self):
        class Capture(A.Agent):
            async def run(self, template, **kwargs):
                self.asked = kwargs["input"]
                return A.RunResult()

        agent = quiet_agent(Capture)
        self.addCleanup(agent.close)
        session = agent.sessions.create()
        run(agent.resume(session))
        self.assertEqual(agent.asked, A.Agent.RESUME_PROMPT)  # no plan: the prompt alone
        agent.plans.set(session.id, self.plan())
        run(agent.resume(session.id))
        self.assertTrue(agent.asked.startswith(A.Agent.RESUME_PROMPT))
        self.assertIn(f"{A.Agent.RESUME_PLAN}\nPlan: 1 of 2 done", agent.asked)
        self.assertIn("2. [>] Write (not found yet: b.md)", agent.asked)


class WorkerTest(unittest.TestCase):
    """`delegate`: one step in a fresh context, its own tools, a capped answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "a.md").write_text("x" * 2000, encoding="utf-8")  # 500 tokens

    def scripted(self, steps, **overrides):
        return SliceTest.scripted(self, steps, **overrides)

    @staticmethod
    def outputs(call):
        """The tool outputs in the input of a model call, by call id."""
        return {i["call_id"]: i["output"] for i in call.input if i.get("type") == "function_call_output"}

    def test_a_step_runs_in_a_fresh_context_and_only_its_answer_returns(self):
        from agents.testing import assistant_message, function_call

        engine, model = self.scripted([
            [function_call("delegate", {"task": "Summarise a.md", "inputs": ["a.md"]}, call_id="d1")],
            [function_call("read_text_file", {"path": "a.md"}, call_id="r1")],
            [assistant_message("A is all x.")],
            [assistant_message("done")],
        ])
        result = run(engine.run("summarise everything", workspace=self.root, session_id="s1"))
        self.assertTrue(result.ok, result.error)
        leader, worker, _, last = model.calls
        self.assertEqual(len(worker.input), 1)  # fresh: the task alone
        self.assertEqual(worker.input[0]["content"], "Summarise a.md\n\nInputs (read long ones a page at a time):\n- a.md: ~500 tokens")
        self.assertNotIn("summarise everything", json.dumps(worker.input))
        self.assertTrue(worker.system_instructions.startswith(A.DEFAULT_WORKER_INSTRUCTIONS))
        self.assertIn("Answer within about 750 words.", worker.system_instructions)
        self.assertEqual(leader.system_instructions, A.DEFAULT_INSTRUCTIONS)
        names = {tool.name for tool in worker.tools}
        self.assertIn("read_text_file", names)
        self.assertFalse(names & {"delegate", "plan", "ask_user"})
        self.assertTrue({"delegate", "plan"} <= {tool.name for tool in leader.tools})
        outputs = self.outputs(last)
        self.assertEqual(list(outputs), ["d1"])  # the worker's own read never reaches the leader
        self.assertTrue(outputs["d1"].startswith("A is all x.\n[worker: 1 tool calls in "), outputs["d1"])
        self.assertEqual([call.name for call in result.tools], ["delegate"])

    def test_the_worker_is_shown_as_progress_of_its_call(self):
        from agents.testing import assistant_message, function_call

        engine, _ = self.scripted([
            [function_call("delegate", {"task": "Summarise a.md"}, call_id="d1")],
            [function_call("read_text_file", {"path": "a.md"}, call_id="r1")],
            [assistant_message("A is all x.")],
            [assistant_message("done")],
        ])
        seen: list[A.Event] = []
        engine.events.on(A.EventType.ALL, seen.append)
        result = run(engine.run("go", workspace=self.root, session_id="s1"))
        self.assertTrue(result.ok, result.error)
        lines = [e.data["text"] for e in seen if e.type == A.EventType.TOOL_PROGRESS]
        self.assertTrue(lines, "nothing relayed")
        self.assertTrue(all(e.data["id"] == result.tools[0].id for e in seen if e.type == A.EventType.TOOL_PROGRESS))
        self.assertIn('-> read_text_file(path="a.md")\n', lines)
        started = [e.data["name"] for e in seen if e.type == A.EventType.TOOL_START]
        self.assertEqual(started, ["delegate"])  # the worker's calls stay off the leader's bus
        self.assertEqual(sum(e.type == A.EventType.AGENT_END for e in seen), 1)

    def test_a_long_answer_is_clipped(self):
        from agents.testing import assistant_message, function_call

        engine, model = self.scripted(
            [
                [function_call("delegate", {"task": "Say a lot"}, call_id="d1")],
                [assistant_message("word " * 400)],
                [assistant_message("done")],
            ],
            worker_max_tokens=50,
        )
        run(engine.run("go", workspace=self.root, session_id="s1"))
        output = self.outputs(model.calls[-1])["d1"]
        self.assertIn("chars omitted", output)
        self.assertLess(len(output), 400)

    def test_a_plan_step_lends_its_inputs_and_is_marked_done(self):
        from agents.testing import assistant_message, function_call

        plan = {"steps": [
            {"title": "Summarise a", "inputs": ["a.md"], "status": "pending"},
            {"title": "Write", "inputs": [], "status": "pending"},
        ]}
        engine, model = self.scripted([
            [function_call("plan", plan, call_id="p1")],
            [function_call("delegate", {"task": "Summarise it", "step": 1}, call_id="d1")],
            [assistant_message("A is all x.")],
            [assistant_message("done")],
        ])
        result = run(engine.run("go", workspace=self.root, session_id="s1"))
        self.assertTrue(result.ok, result.error)
        self.assertIn("- a.md: ~500 tokens", model.calls[2].input[0]["content"])
        self.assertTrue(self.outputs(model.calls[-1])["d1"].endswith("[plan] step 1 marked done"))
        self.assertEqual([s.status for s in engine.plans.get("s1").steps], ["done", "pending"])
        self.assertIn("[plan] 1/2 done; now 2. Write", model.calls[-1].input[-1]["content"])

    def test_a_missing_step_is_an_error(self):
        from agents.testing import assistant_message, function_call

        engine, model = self.scripted([
            [function_call("delegate", {"task": "Do it", "step": 3}, call_id="d1")],
            [assistant_message("done")],
        ])
        run(engine.run("go", workspace=self.root, session_id="s1"))
        self.assertEqual(self.outputs(model.calls[-1])["d1"], "Error: there is no plan step 3")

    def test_inputs_over_a_worker_budget_are_refused_once(self):
        from agents.testing import assistant_message, function_call

        (self.root / "big.md").write_text("x" * 12_000, encoding="utf-8")  # 3k tokens, over 2.5k
        args = {"task": "Summarise big.md", "inputs": ["big.md"]}
        engine, model = self.scripted([
            [function_call("delegate", args, call_id="d1")],
            [function_call("delegate", args, call_id="d2")],
            [assistant_message("Big is all x.")],
            [assistant_message("done")],
        ])
        result = run(engine.run("go", workspace=self.root, session_id="s1"))
        self.assertTrue(result.ok, result.error)
        outputs = self.outputs(model.calls[-1])
        self.assertTrue(outputs["d1"].startswith("Not delegated: the inputs take ~3.0k tokens (big.md 3.0k), more than the 2.5k"), outputs["d1"])
        self.assertTrue(outputs["d2"].startswith("Big is all x."))

    def test_a_failing_worker_is_reported_not_raised(self):
        from agents.testing import assistant_message, function_call

        engine, model = self.scripted(
            [
                [function_call("delegate", {"task": "Loop"}, call_id="d1")],
                [function_call("list_directory", {"path": "."}, call_id="l1")],
                [assistant_message("done")],
            ],
            worker_max_turns=1,
        )
        result = run(engine.run("go", workspace=self.root, session_id="s1"))
        self.assertTrue(result.ok, result.error)
        output = self.outputs(model.calls[-1])["d1"]
        self.assertTrue(output.startswith("Error: the worker failed ("), output)
        self.assertIn("after 1 tool calls", output)
        self.assertFalse(result.tools[0].ok)

    def test_tools_names_the_only_tools_the_worker_gets(self):
        from agents.testing import assistant_message, function_call

        engine, model = self.scripted([
            [function_call("delegate", {"task": "Read", "tools": ["read_text_file"]}, call_id="d1")],
            [assistant_message("ok")],
            [function_call("delegate", {"task": "Read", "tools": ["fly"]}, call_id="d2")],
            [assistant_message("done")],
        ])
        run(engine.run("go", workspace=self.root, session_id="s1"))
        self.assertEqual([tool.name for tool in model.calls[1].tools], ["read_text_file"])
        self.assertTrue(self.outputs(model.calls[-1])["d2"].startswith("Error: no worker tool named fly (there are "))

    def test_the_worker_tokens_count_to_the_run(self):
        from agents.testing import assistant_message, function_call
        from agents.usage import Usage

        engine, model = self.scripted([
            [function_call("delegate", {"task": "Do it"}, call_id="d1")],
            [assistant_message("ok")],
            [assistant_message("done")],
        ])
        model.set_default_usage(Usage(requests=1, input_tokens=100, output_tokens=10, total_tokens=110))
        result = run(engine.run("go", workspace=self.root, session_id="s1"))
        self.assertEqual((result.usage.requests, result.usage.input_tokens), (3, 300))

    def test_an_agent_journals_the_delegate_call_not_the_workers_steps(self):
        from agents.testing import ScriptedModel, assistant_message, function_call

        model = ScriptedModel([
            [function_call("delegate", {"task": "Summarise a.md", "inputs": ["a.md"]}, call_id="d1")],
            [function_call("read_text_file", {"path": "a.md"}, call_id="r1")],
            [assistant_message("A is all x.")],
            [assistant_message("done")],
        ])

        class Scripted(A.Agent):
            def build_model(self, config, role=A.LEADER_ROLE):
                return model

        agent = quiet_agent(Scripted, shell_enabled=False, workspace_root=self.root / "ws")
        self.addCleanup(agent.close)
        session = agent.sessions.create()
        (session.workspace / "a.md").write_text("x" * 2000, encoding="utf-8")
        result = run(agent.run("{{ config.input }}", session=session, input="go"))
        self.assertTrue(result.ok, result.error)
        turns = agent.memory.transcript(session.id).turns
        self.assertEqual([t.meta.get("name") for t in turns if t.role == "tool"], ["delegate"])
        self.assertEqual([t.role for t in turns], ["user", "tool", "assistant"])
        self.assertNotIn("r1", json.dumps([t.items for t in turns]))

    def test_the_worker_role_falls_back_to_the_leader(self):
        engine = A.Engine(env=False, config_file=False, inference_llm="big", worker_max_turns=7)
        self.addCleanup(engine.close)
        worker = engine.worker_config(engine.config)
        self.assertEqual((worker.inference_llm, worker.max_turns), ("big", 7))
        self.assertFalse(worker.plan_enabled or worker.ask_enabled or worker.delegate_enabled)
        small = engine.worker_config(engine.config.merge(worker_model="small", worker_api_url="http://w"))
        self.assertEqual((small.inference_llm, small.inference_url), ("small", "http://w"))
        bare = engine.worker_config(engine.config.merge(worker_max_tokens=0))
        self.assertEqual(bare.instructions, A.DEFAULT_WORKER_INSTRUCTIONS)

    def test_the_delegate_tool_is_built_only_when_enabled(self):
        ws = A.Workspace(self.root)
        engine = A.Engine(env=False, config_file=False, delegate_enabled=False, shell_enabled=False)
        self.addCleanup(engine.close)
        self.assertIsNone(engine.build_delegate(engine.config, A.RunResult(), ws))
        names = {tool.name for tool in run(engine.build_run_tools(ws, engine.config, A.RunResult()))}
        self.assertNotIn("delegate", names)
        self.assertIn("plan", names)

    def test_the_relay_turns_events_into_lines_and_paced_beats(self):
        reports: list[tuple[str, str]] = []
        clock = [0.0]

        async def report(call_id, text):
            reports.append((call_id, text))

        relay = A.WorkerRelay(report, "c1", clock=lambda: clock[0])

        async def drive():
            await relay(A.Event(A.EventType.TOOL_START, {"text": 'read_text_file(path="a.md")'}))
            await relay(A.Event(A.EventType.BLOCK_DELTA, {"text": "x"}))  # within a beat of the line
            clock[0] = 2.0
            await relay(A.Event(A.EventType.BLOCK_DELTA, {"text": "x"}))
            await relay(A.Event(A.EventType.BLOCK_DELTA, {"text": "x"}))
            await relay(A.Event(A.EventType.TOOL_END, {"name": "run", "ok": False, "result": "boom\nagain"}))
            await relay(A.Event(A.EventType.CONTEXT_COMPACT, {"message": "Compacted 9k -> 2k"}))
            await relay(A.Event(A.EventType.AGENT_START, {}))  # nothing a worker publishes
            await relay(A.Event(A.EventType.TOOL_START, {"text": "y" * 500}))

        run(drive())
        texts = [text for _, text in reports]
        self.assertEqual(texts[:5], [
            '-> read_text_file(path="a.md")\n', "", "!! run failed: boom again\n", "~~ Compacted 9k -> 2k\n",
            "-> " + "y" * 157 + "...\n",
        ])
        self.assertTrue(all(call_id == "c1" for call_id, _ in reports))


class PlanViewTest(unittest.TestCase):
    """The plan and the measured speed, shown: `plan.update`, `/plan`, hello, the console."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "a.md").write_text("x" * 2000, encoding="utf-8")  # 500 tokens
        self.ws = A.Workspace(self.root)

    PLAN = {"steps": [
        {"title": "Summarise a", "inputs": ["a.md"], "status": "pending"},
        {"title": "Write", "inputs": [], "status": "pending"},
    ]}

    def test_a_plan_is_viewed_with_its_progress_and_estimate(self):
        plan = A.Plan([
            A.PlanStep("Read", status="done", sizes=[("a.md", 400)]),
            A.PlanStep("Write", status="active", sizes=[("b.md", 6000)], missing=["c.md"]),
            A.PlanStep("Read", status="pending", sizes=[("d.md", 6000)]),
        ])
        view = plan.view(A.SliceBudget(20_000, 0, 0.25), A.Throughput(prefill=100.0))
        self.assertEqual(view["steps"][1], {"title": "Write", "status": "active", "tokens": 6000, "missing": ["c.md"]})
        self.assertEqual((view["done"], view["total"], view["current"]), (1, 3, 2))
        self.assertEqual((view["left"], view["slices"], view["seconds"]), (12_000, 3, 120.0))
        self.assertEqual(A.Plan([A.PlanStep("x", status="done")]).view()["current"], 0)

    def test_every_change_of_a_plan_is_published_for_its_session(self):
        from agents.testing import assistant_message, function_call

        engine, _ = SliceTest.scripted(self, [
            [function_call("plan", self.PLAN, call_id="p1")],
            [function_call("delegate", {"task": "Summarise it", "step": 1}, call_id="d1")],
            [assistant_message("A is all x.")],
            [function_call("plan", {"steps": []}, call_id="p2")],
            [assistant_message("done")],
        ])
        seen: list[A.Event] = []
        engine.events.on(A.EventType.PLAN_UPDATE, seen.append)
        result = run(engine.run("go", workspace=self.root, session_id="s1"))
        self.assertTrue(result.ok, result.error)
        statuses = [
            [step["status"] for step in e.data["plan"]["steps"]] if e.data["plan"] else None for e in seen
        ]
        self.assertEqual(statuses, [
            ["pending", "pending"], ["active", "pending"], ["done", "pending"], None,
        ])
        self.assertTrue(all(e.session_id == "s1" for e in seen))
        self.assertEqual(seen[0].data["plan"]["steps"][0]["tokens"], 500)

    def test_a_refused_plan_publishes_nothing(self):
        planner = SliceTest.planner(self, A.SliceBudget(400))
        views = []

        async def notify(view):
            views.append(view)

        planner.notify = notify
        steps = [{"title": "Read", "inputs": ["a.md"], "status": "pending"}]
        self.assertIn("Plan not kept", run(planner.update(steps)))
        self.assertEqual(views, [])
        run(planner.update(steps))  # sent back: kept
        self.assertEqual([v["total"] for v in views], [1])

    def test_the_plan_command_lists_the_steps_and_the_speeds(self):
        agent = quiet_agent(A.Agent, shell_enabled=False, workspace_root=self.root / "ws")
        self.addCleanup(agent.close)
        session = agent.sessions.create()
        self.assertEqual(run(agent.commands.invoke("/plan", session_id=session.id))["message"], "No plan")
        agent.plans.set(session.id, A.Plan([
            A.PlanStep("Read", status="done", sizes=[("a.md", 500)]),
            A.PlanStep("Write", status="active", sizes=[("b.md", 1500)]),
        ]))
        agent.throughput(agent.config).prefill, agent.throughput(agent.config).decode = 100.0, 20.0
        result = run(agent.commands.invoke("/plan", session_id=session.id))
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["current"], 2)
        message = result["message"]
        self.assertIn("- [x] Read · 500", message)
        self.assertIn("- [ ] **Write** · 1.5k", message)
        self.assertIn("1 of 2 done · 1.5k tokens left", message)
        self.assertIn("reads ~100 · writes ~20 tokens/s", message)

    def test_hello_carries_the_plan_of_the_session(self):
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=False, env=False)
        self.addCleanup(agent.close)
        server = A.WebServer(agent, template="{{ config.input }}")
        session = agent.sessions.create()
        agent.plans.set(session.id, A.Plan([A.PlanStep("Read", status="active")]))
        connection = A.Connection(server.hub, HubTest.FakeSocket())
        server.hub.connections[connection.id] = connection
        run(server.on_hello(connection, {"session": session.id}))
        hello = connection.ws.sent[-1]
        self.assertEqual(hello["plan"]["steps"][0]["title"], "Read")
        other = agent.sessions.create()
        run(server.on_hello(connection, {"session": other.id}))
        self.assertIsNone(connection.ws.sent[-1]["plan"])

    def test_the_console_prints_plan_progress_and_speeds(self):
        buffer = ConsoleRendererTest.Buffer()
        renderer = A.ConsoleRenderer(color=False, stream=buffer)
        view = A.Plan([A.PlanStep("Read", status="done"), A.PlanStep("Write")]).view()
        renderer.handle(A.Event(type=A.EventType.PLAN_UPDATE, data={"plan": view}))
        renderer.handle(A.Event(type=A.EventType.PLAN_UPDATE, data={"plan": None}))
        usage = {**A.ContextBudget.of(128_000).usage(51_200), "prefill": 100.0, "decode": 20.0}
        renderer.handle(A.Event(type=A.EventType.CONTEXT_USAGE, data=usage))
        renderer.handle(A.Event(type=A.EventType.AGENT_END, data={}))
        self.assertIn("[plan] 1/2 done; now 2. Write\n", buffer.text)
        self.assertIn("[plan] cleared\n", buffer.text)
        self.assertIn("ctx 50% · 51k / 102k · reads ~100 · writes ~20 tokens/s", buffer.text)


class AgentContextTest(unittest.TestCase):
    """Compaction over the journal: an endless chat on a small window."""

    def setUp(self):
        from agents import function_tool

        self.summaries: list[tuple[str, str, str]] = []
        self.steps: list = []
        test = self

        @function_tool
        def write(path: str, text: str) -> str:
            """Write a file."""
            return "ok"

        class Scripted(A.Agent):
            def build_model(self, config, role=A.LEADER_ROLE):
                from agents.testing import ScriptedModel, assistant_message

                return ScriptedModel(test.steps.pop(0) if test.steps else [[assistant_message("ok " + "y" * 800)]])

            def build_tools(self, *args, **kwargs):
                return [write]

            def build_summarizer(self, config):
                async def summarize(previous, segment, focus=""):
                    test.summaries.append((previous, segment, focus))
                    return f"SUMMARY {len(test.summaries)}"

                return summarize

        self.agent = Scripted(
            console=False,
            store=A.MemoryStore(),
            config_file=False,
            context_window=6000,
            context_reserve=1000,
            context_keep=2,
            plan_enabled=False,  # compaction alone: no budget note on the input
        )
        self.addCleanup(self.agent.close)
        self.session = self.agent.sessions.create()

    def chat(self, text):
        result = run(self.agent.run("{{ config.input }}", session=self.session, input=text))
        self.assertTrue(result.ok, result.error)
        return result

    def transcript(self):
        return self.agent.memory.transcript(self.session.id)

    def test_an_endless_chat_stays_inside_the_window(self):
        for n in range(20):
            self.chat(f"question {n} " + "q" * 400)
        transcript = self.transcript()
        self.assertEqual(sum(t.role == "user" for t in transcript.turns), 20)  # every turn kept
        self.assertIsNotNone(transcript.summary())
        counter = self.agent.counter(self.agent.config)
        budget = A.ContextBudget.of(6000, 1000)
        self.assertLessEqual(counter.items(transcript.messages()), budget.high)

    def test_the_next_run_starts_from_the_summary(self):
        for n in range(16):
            self.chat(f"question {n} " + "q" * 400)
        seen: list = []
        original = self.agent.build_model

        def spy(config, role=A.LEADER_ROLE):
            model = original(config, role)
            seen.append(model)
            return model

        self.agent.build_model = spy
        self.chat("one more")
        first = seen[0].calls[0].input
        self.assertTrue(first[0]["content"].startswith(A.SUMMARY_HEADER))
        self.assertEqual(first[-1]["content"], "one more")

    def test_a_summary_made_mid_run_is_journaled_against_its_turns(self):
        from agents.testing import assistant_message, function_call

        self.steps.append(
            [[function_call("write", {"path": f"f{n}", "text": "z" * 2000}, call_id=f"w{n}")] for n in range(16)]
            + [[assistant_message("done")]]
        )
        self.chat("write everything")
        transcript = self.transcript()
        summaries = [t for t in transcript.turns if t.role == "compact" and t.meta.get("summary")]
        self.assertTrue(summaries)
        keep = summaries[-1].meta["keep"]
        self.assertEqual(list(keep), ["seq"])  # settled from the calls it kept
        kept = transcript.turns[transcript.boundary()]
        self.assertEqual(kept.role, "tool")
        items = transcript.messages()
        self.assertTrue(items[0]["content"].startswith(A.SUMMARY_HEADER))
        self.assertIn("write everything", items[0]["content"])  # the request, pinned
        self.assertEqual(items[-1]["content"][0]["text"], "done")

    def test_compact_command_folds_the_conversation_now(self):
        self.chat("hello")
        self.chat("world")
        result = run(self.agent.commands.invoke("/compact the greetings", session_id=self.session.id))
        self.assertTrue(result["ok"], result)
        self.assertIn("Compacted", result["message"])
        self.assertEqual(self.summaries[-1][2], "the greetings")
        replayed = self.agent.memory.replay(self.session.id)
        self.assertEqual(replayed[-1]["kind"], "compact")
        self.assertTrue(replayed[-1]["meta"]["summary"])

    def test_compact_command_with_nothing_to_compact(self):
        empty = self.agent.sessions.create()
        result = run(self.agent.commands.invoke("/compact", session_id=empty.id))
        self.assertEqual(result["message"], "Nothing to compact")

    def test_compact_command_when_nothing_can_be_folded(self):
        self.agent.memory.journal(self.session.id, A.Turn("user", "only this"))
        result = run(self.agent.commands.invoke("/compact", session_id=self.session.id))
        self.assertEqual(result["message"], "Nothing to compact")
        self.assertIsNone(self.transcript().summary())

    def test_the_gauge_never_waits_on_a_provider(self):
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=False)
        self.addCleanup(agent.close)
        session = agent.sessions.create()
        agent.memory.journal(session.id, A.Turn("user", "remembered"))
        probed = []

        async def probe(spec):
            probed.append(spec)
            return 64_000

        agent.models.probe_window = probe
        self.assertIsNone(run(agent.context_usage(session.id)))
        self.assertEqual(probed, [])
        agent.models.learn_window(agent.config.model_spec(A.LEADER_ROLE), 64_000)
        self.assertEqual(run(agent.context_usage(session.id))["window"], 64_000)

    def test_a_compaction_between_runs_happens_in_the_background(self):
        async def scenario():
            for n in range(12):
                await self.agent.run("{{ config.input }}", session=self.session, input=f"q{n} " + "q" * 600)
            await self.agent.compacted(self.session.id)

        run(scenario())
        summary = self.transcript().summary()
        self.assertIsNotNone(summary)
        self.assertEqual(summary.run, "")  # made by no run: between them

    def test_usage_is_kept_for_a_client_that_joins(self):
        self.chat("hello")
        usage = run(self.agent.context_usage(self.session.id))
        self.assertEqual(usage["window"], 6000)
        self.assertGreater(usage["tokens"], 0)
        fresh = self.agent.sessions.create()
        self.agent.memory.journal(fresh.id, A.Turn("user", "remembered"))
        self.assertTrue(run(self.agent.context_usage(fresh.id))["estimated"])


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = A.Workspace(self.tmp.name, shell_timeout=10)

    def test_paths_are_scoped_to_the_root(self):
        self.assertEqual(self.ws.resolve("a/b.txt"), self.ws.root / "a" / "b.txt")
        for escape in ("../outside.txt", "/etc/passwd", "a/../../x", ""):
            with self.assertRaises(A.WorkspaceError):
                self.ws.resolve(escape)

    def test_symlinks_cannot_escape_the_root(self):
        (Path(self.ws.root) / "link").symlink_to(Path(self.tmp.name).parent)
        with self.assertRaises(A.WorkspaceError):
            self.ws.resolve("link/outside.txt")

    def test_quotes_are_stripped(self):
        self.assertEqual(self.ws.resolve("'a.txt'"), self.ws.root / "a.txt")

    def test_reads_may_leave_the_root_when_allowed(self):
        base = Path(self.tmp.name)
        (base / "root").mkdir()
        (base / "outside.txt").write_text("out", encoding="utf-8")
        ws = A.Workspace(base / "root", outside_read=True)
        self.assertEqual(ws.read_text(str(base / "outside.txt")), "out")
        self.assertEqual(ws.read_text("../outside.txt"), "out")
        self.assertIn("outside.txt", ws.list_dir(".."))
        self.assertTrue(ws.read_data_url("../outside.txt").startswith("data:"))

    def test_writes_leave_the_root_only_with_outside_write(self):
        base = Path(self.tmp.name)
        (base / "root").mkdir()
        (base / "outside.txt").write_text("out", encoding="utf-8")
        ws = A.Workspace(base / "root", outside_read=True)
        with self.assertRaises(A.WorkspaceError):
            ws.write_text("../outside.txt", "x")
        with self.assertRaises(A.WorkspaceError):
            ws.apply_patch("../outside.txt", "", "delete_file")
        self.assertEqual((base / "outside.txt").read_text(encoding="utf-8"), "out")
        ws = A.Workspace(base / "root", outside_write=True)
        ws.write_text("../outside.txt", "in")
        self.assertEqual((base / "outside.txt").read_text(encoding="utf-8"), "in")
        with self.assertRaises(A.WorkspaceError):
            ws.read_text("../outside.txt")  # writing out is not reading out

    def test_read_write_and_list(self):
        self.ws.write_text("dir/file.txt", "hello")
        self.assertEqual(self.ws.read_text("dir/file.txt"), "hello")
        self.assertEqual(self.ws.list_dir("."), ["dir/"])
        with self.assertRaises(A.WorkspaceError):
            self.ws.read_text("missing.txt")
        with self.assertRaises(A.WorkspaceError):
            self.ws.list_dir("missing")

    def test_read_page_returns_whole_lines_up_to_the_cap(self):
        self.ws.write_text("f.txt", "".join(f"line {n}\n" for n in range(1, 11)))
        page = self.ws.read_page("f.txt", max_chars=20)
        self.assertEqual((page.start, page.end, page.total), (1, 2, 10))
        self.assertEqual(page.text, "line 1\nline 2\n")
        self.assertEqual(page.render(), "line 1\nline 2\n[lines 1-2 of 10 of f.txt; continue with offset=3]")
        page = self.ws.read_page("f.txt", offset=9)
        self.assertEqual(page.text, "line 9\nline 10\n")
        self.assertIn("lines 9-10 of 10", page.footer)
        self.assertNotIn("continue", page.footer)
        self.assertEqual(self.ws.read_page("f.txt", offset=3, limit=2).text, "line 3\nline 4\n")
        with self.assertRaises(A.WorkspaceError):
            self.ws.read_page("f.txt", offset=11)

    def test_read_page_of_a_whole_file_is_the_file(self):
        self.ws.write_text("f.txt", "a\nb")
        page = self.ws.read_page("f.txt", max_chars=100)
        self.assertTrue(page.complete)
        self.assertEqual(page.render(), "a\nb")
        self.assertTrue(self.ws.read_page("f.txt").complete)  # no cap
        self.ws.write_text("empty.txt", "")
        self.assertEqual(self.ws.read_page("empty.txt").render(), "")

    def test_read_page_clips_a_line_longer_than_the_cap(self):
        self.ws.write_text("f.txt", "x" * 500 + "\nshort\n")
        page = self.ws.read_page("f.txt", max_chars=100)
        self.assertTrue(page.clipped)
        self.assertEqual((page.start, page.end), (1, 1))
        self.assertIn("chars omitted", page.text)
        self.assertIn("line 1 clipped", page.footer)
        self.assertIn("offset=2", page.footer)

    def test_read_data_url(self):
        (Path(self.ws.root) / "pixel.png").write_bytes(b"\x89PNG\r\n")
        url = self.ws.read_data_url("pixel.png")
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), b"\x89PNG\r\n")

    def test_empty_file_is_rejected_for_data_url(self):
        (Path(self.ws.root) / "empty.png").write_bytes(b"")
        with self.assertRaises(A.WorkspaceError):
            self.ws.read_data_url("empty.png")

    def test_shell_runs_in_the_workspace(self):
        output = run(self.ws.run_shell("pwd"))
        self.assertIn(str(self.ws.root), output)

    def test_shell_reports_failures(self):
        self.assertIn("[exit code 3]", run(self.ws.run_shell("exit 3")))

    def test_shell_times_out(self):
        ws = A.Workspace(self.tmp.name, shell_timeout=1)
        self.assertIn("timed out", run(ws.run_shell("sleep 5")))

    def test_shell_timeout_kills_the_whole_process_tree(self):
        # The interpreter is a child of the shell and holds its pipes: killing
        # only the shell used to leave the call waiting for the child to end.
        pid_file = Path(self.tmp.name) / "pid"
        ws = A.Workspace(self.tmp.name, shell_timeout=2)
        start = time.monotonic()
        self.assertIn("timed out", run(ws.run_shell(sleeper_command(pid_file))))
        self.assertLess(time.monotonic() - start, 6)
        self.assertFalse(process_alive(int(pid_file.read_text())))

    def test_shell_cancellation_kills_the_command(self):
        pid_file = Path(self.tmp.name) / "pid"

        async def cancel_once_started():
            task = asyncio.ensure_future(self.ws.run_shell(sleeper_command(pid_file)))
            for _ in range(100):
                if pid_file.exists() and pid_file.read_text():
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        run(cancel_once_started())
        self.assertFalse(process_alive(int(pid_file.read_text())))

    def test_shell_streams_output_and_heartbeats_while_it_runs(self):
        ws = A.Workspace(self.tmp.name, shell_heartbeat=0.1)
        seen: list[str] = []

        async def progress(text):
            seen.append(text)

        command = python_command(
            "import time; print('hi', flush=True); time.sleep(0.5); print('bye')"
        )
        output = run(ws.run_shell(command, progress))
        self.assertIn("hi", "".join(seen))
        self.assertIn("", seen)
        self.assertIn("hi", output)
        self.assertIn("bye", output)

    def test_a_failing_progress_callback_never_breaks_the_command(self):
        async def progress(text):
            raise RuntimeError("boom")

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            output = run(self.ws.run_shell(python_command("print('hi')"), progress))
        self.assertIn("hi", output)
        self.assertIn("boom", errors.getvalue())

    def test_shell_output_is_clipped(self):
        ws = A.Workspace(self.tmp.name, shell_output_limit=200)
        output = run(ws.run_shell("python -c \"print('a' * 1000 + 'END')\""))
        self.assertLess(len(output), 300)
        self.assertIn("chars omitted", output)
        self.assertTrue(output.startswith("a"))
        self.assertTrue(output.endswith("END"))

    def test_clip_keeps_short_text(self):
        self.assertEqual(A.clip_output("short", 100), "short")
        self.assertEqual(A.clip_output("x" * 500, 0), "x" * 500)

    def test_guess_mime(self):
        self.assertEqual(A.guess_mime("a.png"), "image/png")
        self.assertEqual(A.guess_mime("a.unknown"), "application/octet-stream")


class ToolRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = A.Workspace(self.tmp.name)
        (Path(self.ws.root) / "a.png").write_bytes(b"\x89PNG")

    @staticmethod
    def invoke(tool, payload):
        from agents.tool_context import ToolContext

        context = ToolContext(
            context=None, tool_name=tool.name, tool_call_id="1", tool_arguments=payload
        )
        return run(tool.on_invoke_tool(context, payload))

    def tools(self, vision=None):
        built = A.ToolRegistry(shell=False).build(self.ws, vision=vision)
        return {tool.name: tool for tool in built}

    def test_leader_sees_images_when_no_vision_model_is_configured(self):
        tools = self.tools()
        self.assertIn("view_image", tools)
        self.assertNotIn("describe_image", tools)
        self.assertTrue(
            self.invoke(tools["view_image"], '{"path": "a.png"}').startswith("data:image/png")
        )

    def test_tools_mention_outside_paths_only_when_allowed(self):
        self.assertNotIn("outside", self.tools()["read_text_file"].description)
        self.ws.outside_read = True
        for name in ("list_directory", "read_text_file", "view_image"):
            self.assertIn("outside the workspace", self.tools()[name].description)
        self.assertNotIn("outside", self.tools()["write_text_file"].description)
        self.ws.outside_write = True
        for name in ("write_text_file", "apply_patch"):
            self.assertIn("written too", self.tools()[name].description)

    def test_long_files_are_read_a_page_at_a_time(self):
        self.ws.write_text("f.txt", "".join(f"line {n}\n" for n in range(1, 101)))
        tools = {t.name: t for t in A.ToolRegistry(shell=False).build(self.ws, read_limit=80)}
        first = self.invoke(tools["read_text_file"], '{"path": "f.txt"}')
        self.assertTrue(first.startswith("line 1\n"))
        self.assertIn("continue with offset=", first)
        offset = int(first.rsplit("offset=", 1)[1].rstrip("]"))
        second = self.invoke(tools["read_text_file"], f'{{"path": "f.txt", "offset": {offset}}}')
        self.assertTrue(second.startswith(f"line {offset}\n"))

    def test_a_cut_read_repeated_back_to_back_is_refused(self):
        self.ws.write_text("f.txt", "".join(f"line {n}\n" for n in range(1, 101)))
        tools = {t.name: t for t in A.ToolRegistry(shell=False).build(self.ws, read_limit=80)}
        read = lambda: self.invoke(tools["read_text_file"], '{"path": "f.txt"}')
        first = read()
        refused = read()
        self.assertTrue(refused.startswith("Error: this exact read just returned [lines 1-"))
        self.assertNotIn("line 1\n", refused)
        self.invoke(tools["write_text_file"], '{"path": "g.txt", "content": "x"}')
        self.assertEqual(read(), first)  # a write in between: the file may have changed

    def test_a_whole_read_may_be_repeated(self):
        self.ws.write_text("f.txt", "short")
        tools = {t.name: t for t in A.ToolRegistry(shell=False).build(self.ws, read_limit=80)}
        for _ in range(2):
            self.assertEqual(self.invoke(tools["read_text_file"], '{"path": "f.txt"}'), "short")

    def test_read_limit_is_configured_or_a_quarter_of_the_usable_budget(self):
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=False, env=False)
        self.assertEqual(run(agent.read_limit(A.AgentConfig(read_output_limit=1234))), 1234)
        self.assertEqual(run(agent.read_limit(A.AgentConfig(read_output_limit=-1))), 0)
        config = A.AgentConfig(context_window=40_000, context_reserve=8_000)
        self.assertEqual(run(agent.read_limit(config)), 8_000 * A.TokenCounter.BYTES_PER_TOKEN)

    def test_vision_model_replaces_the_raw_image_tool(self):
        seen: list[tuple[str, str]] = []

        async def vision(data_url, question):
            seen.append((data_url, question))
            return "a cat"

        tools = self.tools(vision)
        self.assertIn("describe_image", tools)
        self.assertNotIn("view_image", tools)
        answer = self.invoke(tools["describe_image"], '{"path": "a.png", "question": "q"}')
        self.assertEqual(answer, "a cat")
        self.assertTrue(seen[0][0].startswith("data:image/png"))
        self.assertEqual(seen[0][1], "q")

    def test_the_shell_reports_progress_under_its_call_id(self):
        seen: list[tuple[str, str]] = []

        async def progress(call_id, text):
            seen.append((call_id, text))

        self.ws.outside_read = self.ws.outside_write = True  # unconfined: no sandbox
        built = A.ToolRegistry().build(self.ws, progress=progress)
        shell = {tool.name: tool for tool in built}["run_shell_command"]
        self.assertEqual(set(shell.params_json_schema["properties"]), {"command"})
        answer = self.invoke(shell, json.dumps({"command": python_command("print('hi')")}))
        self.assertIn("hi", answer)
        self.assertIn("hi", "".join(text for call_id, text in seen if call_id == "1"))

    def test_the_ask_tool_comes_only_with_an_asker(self):
        self.assertNotIn("ask_user", self.tools())

        async def ask(call_id, question, options):
            return "fine"

        built = A.ToolRegistry(shell=False).build(self.ws, ask=ask)
        tool = {tool.name: tool for tool in built}["ask_user"]
        self.assertEqual(set(tool.params_json_schema["properties"]), {"question", "options"})

    def test_the_ask_tool_asks_under_its_call_id(self):
        asked: list[tuple] = []
        replies = iter(["blue", ""])

        async def ask(call_id, question, options):
            asked.append((call_id, question, options))
            return next(replies)

        built = A.ToolRegistry(shell=False).build(self.ws, ask=ask)
        tool = {tool.name: tool for tool in built}["ask_user"]
        payload = json.dumps({"question": "Which one?", "options": ["red", "blue"]})
        self.assertEqual(self.invoke(tool, payload), "blue")
        self.assertEqual(asked, [("1", "Which one?", ["red", "blue"])])
        self.assertIn("did not answer", self.invoke(tool, json.dumps({"question": "And?"})))
        self.assertEqual(asked[-1][2], [])

    def test_vision_errors_are_reported_to_the_leader(self):
        async def vision(data_url, question):
            raise RuntimeError("upstream is down")

        tools = self.tools(vision)
        answer = self.invoke(tools["describe_image"], '{"path": "a.png"}')
        self.assertEqual(answer, "Error: the vision model failed: upstream is down")

    def test_registered_tools_are_appended(self):
        registry = A.ToolRegistry(defaults=False, shell=False)
        registry.register("mine", lambda ws: f"tool:{ws.root.name}")
        self.assertEqual(registry.build(self.ws), [f"tool:{self.ws.root.name}"])


class RepoSpecTest(unittest.TestCase):
    def test_https_ssh_and_shorthand_urls_resolve_to_the_same_repository(self):
        for url in (
            "https://github.com/owner/name.git",
            "git@github.com:owner/name.git",
            "owner/name",
        ):
            spec = A.RepoSpec(url=url)
            self.assertEqual(spec.slug, "owner/name", url)
            self.assertEqual(spec.name, "name", url)
            self.assertEqual(spec.api_url, "https://api.github.com", url)

    def test_enterprise_host_uses_its_own_api(self):
        spec = A.RepoSpec(url="https://git.acme.io/owner/name")
        self.assertEqual(spec.api_url, "https://git.acme.io/api/v3")

    def test_unknown_urls_are_rejected(self):
        with self.assertRaises(A.RepoError):
            _ = A.RepoSpec(url="not a repository").slug
        with self.assertRaises(A.RepoError):
            _ = A.RepoSpec().slug

    def test_filesystem_repositories_have_no_pull_request_api(self):
        spec = A.RepoSpec(url="/tmp/origin.git")
        self.assertTrue(spec.local)
        self.assertEqual(spec.name, "origin")
        self.assertEqual(spec.clone_url, "/tmp/origin.git")
        with self.assertRaises(A.RepoError):
            _ = spec.api_url

    def test_token_is_embedded_only_in_the_authenticated_url_and_masked(self):
        spec = A.RepoSpec(url="https://github.com/owner/name.git", token="ghp_secret")
        self.assertNotIn("ghp_secret", spec.clone_url)
        self.assertIn("x-access-token:ghp_secret@", spec.authenticated_url())
        self.assertEqual(spec.mask("fatal: ghp_secret"), "fatal: ***")

    def test_config_layers_into_a_spec(self):
        config = A.AgentConfig().with_env(
            {"AGENTIC_REPO_URL": "owner/name", "AGENTIC_REPO_BRANCH": "trunk"}
        )
        spec = A.RepoSpec.from_config(config)
        self.assertEqual((spec.url, spec.branch), ("owner/name", "trunk"))
        self.assertEqual(config.to_dict()["repo_token"], "")


def git(*args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


class RepoManagerTest(unittest.TestCase):
    """Clone, commit and push against a bare repository on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        seed = self.root / "seed"
        seed.mkdir()
        git("init", "--initial-branch", "main", cwd=seed)
        (seed / "README.md").write_text("seed", encoding="utf-8")
        git("add", "--all", cwd=seed)
        git("commit", "--message", "seed", cwd=seed)
        git("init", "--bare", "--initial-branch", "main", str(self.origin), cwd=self.root)
        git("remote", "add", "origin", str(self.origin), cwd=seed)
        git("push", "origin", "main", cwd=seed)
        self.manager = A.RepoManager(A.RepoSpec(url=str(self.origin)), timeout=60)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def clone(self, session_id="abc123"):
        return run(self.manager.clone(self.workspace, session_id=session_id))

    def test_clone_checks_out_a_session_branch(self):
        checkout = self.clone()
        self.assertEqual(checkout.path, self.workspace / "origin")
        self.assertEqual(checkout.branch, "agent/abc123")
        self.assertEqual(checkout.base, "main")
        self.assertTrue((checkout.path / "README.md").is_file())
        self.assertEqual(
            run(self.manager.git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout.path)),
            "agent/abc123",
        )

    def test_clone_refuses_a_non_empty_target(self):
        self.clone()
        with self.assertRaises(A.RepoError):
            self.clone()

    def test_commit_and_push_reach_the_remote(self):
        checkout = self.clone()
        (checkout.path / "note.txt").write_text("work", encoding="utf-8")
        self.assertIn("add note", run(checkout.commit("add note")))
        run(checkout.push())
        branches = run(
            self.manager.git("ls-remote", "--heads", str(self.origin), cwd=self.workspace)
        )
        self.assertIn("agent/abc123", branches)

    def test_commit_without_changes_is_refused(self):
        checkout = self.clone()
        with self.assertRaises(A.RepoError):
            run(checkout.commit("nothing"))
        with self.assertRaises(A.RepoError):
            run(checkout.commit("   "))

    def test_status_reports_the_working_tree(self):
        checkout = self.clone()
        (checkout.path / "note.txt").write_text("work", encoding="utf-8")
        self.assertIn("note.txt", run(checkout.status()))

    def test_failing_git_commands_raise(self):
        with self.assertRaises(A.RepoError):
            run(self.manager.git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.workspace))

    def test_pull_request_posts_to_the_forge(self):
        posted: list[tuple[str, dict]] = []

        class Forge(A.RepoManager):
            def _post(self, url, payload):
                posted.append((url, payload))
                return {"number": 7, "html_url": "https://example/pull/7", "state": "open"}

        manager = Forge(A.RepoSpec(url="owner/name", token="t"), timeout=5)
        pull = run(
            manager.open_pull_request(title="Work", body="b", head="agent/x", base="main")
        )
        self.assertEqual(pull["url"], "https://example/pull/7")
        self.assertEqual(posted[0][0], "https://api.github.com/repos/owner/name/pulls")
        self.assertEqual(posted[0][1]["head"], "agent/x")

    def test_pull_request_requires_a_token_a_title_and_a_base(self):
        manager = A.RepoManager(A.RepoSpec(url="owner/name"))
        for kwargs in (
            {"title": "", "head": "h", "base": "main"},
            {"title": "t", "head": "h", "base": "main"},
        ):
            with self.assertRaises(A.RepoError):
                run(manager.open_pull_request(**kwargs))
        manager.spec = A.RepoSpec(url="owner/name", token="t")
        with self.assertRaises(A.RepoError):
            run(manager.open_pull_request(title="t", head="h", base=""))


class AgentRepoTest(unittest.TestCase):
    """The session lifecycle around a repository backed workspace."""

    class LocalSpec(A.RepoSpec):
        """A filesystem repository that pretends to have a forge API."""

        @property
        def api_url(self) -> str:
            return "https://api.example"

    class Forge(A.RepoManager):
        def _post(self, url, payload):
            return {"number": 3, "html_url": "https://example/pull/3", "state": "open"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        seed = self.root / "seed"
        seed.mkdir()
        git("init", "--initial-branch", "main", cwd=seed)
        (seed / "README.md").write_text("seed", encoding="utf-8")
        git("add", "--all", cwd=seed)
        git("commit", "--message", "seed", cwd=seed)
        git("init", "--bare", "--initial-branch", "main", str(self.origin), cwd=self.root)
        git("remote", "add", "origin", str(self.origin), cwd=seed)
        git("push", "origin", "main", cwd=seed)
        self.agent = A.Agent(
            console=False,
            store=A.MemoryStore(),
            repos=self.Forge(timeout=60),
            repo_url=str(self.origin),
            repo_token="t",
        )
        self.agent.repos.spec = self.LocalSpec(url=str(self.origin), token="t")
        self.addCleanup(self.agent.close)

    def test_new_session_clones_the_default_repository(self):
        result = run(self.agent.commands.invoke("/new"))
        session = self.agent.sessions.get(result["session"]["id"])
        checkout = self.agent.checkout(session)
        self.assertIsNotNone(checkout)
        self.assertEqual(checkout.branch, f"agent/{session.id}")
        self.assertTrue((checkout.path / "README.md").is_file())
        self.assertEqual(session.meta["repo"]["base"], "main")

    def test_prepare_session_is_idempotent(self):
        session = self.agent.sessions.create()
        first = run(self.agent.prepare_session(session))
        second = run(self.agent.prepare_session(session))
        self.assertEqual(first.path, second.path)

    def test_a_clone_failure_leaves_the_session_usable(self):
        self.agent.repos.spec = A.RepoSpec(url="/nowhere/missing.git")
        session = self.agent.sessions.create()
        self.assertIsNone(run(self.agent.prepare_session(session)))
        self.assertIsNone(self.agent.checkout(session))

    def test_sessions_without_a_repository_have_no_git_tools(self):
        agent = A.Agent(console=False, store=A.MemoryStore())
        self.addCleanup(agent.close)
        session = agent.sessions.create()
        self.assertIsNone(run(agent.prepare_session(session)))
        names = {
            tool.name
            for tool in agent.build_tools(A.Workspace(session.workspace), agent.config)
        }
        self.assertNotIn("git_commit", names)

    def test_repository_sessions_expose_the_git_tools(self):
        session = self.agent.sessions.create()
        checkout = run(self.agent.prepare_session(session))
        tools = {
            tool.name: tool
            for tool in self.agent.build_tools(
                A.Workspace(session.workspace), self.agent.config, checkout
            )
        }
        self.assertLessEqual(
            {"git_status", "git_commit", "git_push", "open_pull_request", "publish_work"},
            set(tools),
        )
        (checkout.path / "note.txt").write_text("work", encoding="utf-8")
        answer = ToolRegistryTest.invoke(
            tools["publish_work"], json.dumps({"message": "add note"})
        )
        self.assertIn("https://example/pull/3", answer)

    def test_git_tool_errors_are_reported_to_the_leader(self):
        session = self.agent.sessions.create()
        checkout = run(self.agent.prepare_session(session))
        tools = {
            tool.name: tool
            for tool in self.agent.build_tools(
                A.Workspace(session.workspace), self.agent.config, checkout
            )
        }
        answer = ToolRegistryTest.invoke(tools["git_commit"], json.dumps({"message": "x"}))
        self.assertTrue(answer.startswith("Error:"), answer)

    def test_repo_commands_drive_the_checkout(self):
        created = run(self.agent.commands.invoke("/new"))
        session_id = created["session"]["id"]
        checkout = self.agent.checkout(self.agent.sessions.get(session_id))
        (checkout.path / "note.txt").write_text("work", encoding="utf-8")
        invoke = lambda text: run(
            self.agent.commands.invoke(text, session_id=session_id)
        )
        self.assertIn("note.txt", invoke("/status")["message"])
        self.assertTrue(invoke("/commit add note")["ok"])
        self.assertTrue(invoke("/push")["ok"])
        pull = invoke("/pr Add note\nA body")
        self.assertIn("https://example/pull/3", pull["message"])

    def test_repo_command_shows_and_clears_the_default_repository(self):
        self.assertIn(str(self.origin), run(self.agent.commands.invoke("/repo"))["message"])
        run(self.agent.commands.invoke("/repo owner/name"))
        self.assertEqual(self.agent.repos.spec.slug, "owner/name")
        run(self.agent.commands.invoke("/repo none"))
        self.assertFalse(self.agent.repos.configured)
        self.assertFalse(run(self.agent.commands.invoke("/clone"))["ok"])

    def test_commands_without_a_checkout_report_it(self):
        agent = A.Agent(console=False, store=A.MemoryStore())
        self.addCleanup(agent.close)
        session = agent.sessions.create()
        result = run(agent.commands.invoke("/push", session_id=session.id))
        self.assertFalse(result["ok"])
        self.assertIn("no git checkout", result["message"])


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.commands = A.CommandRegistry()

    def test_parse(self):
        self.assertEqual(A.CommandRegistry.parse("/new  abc "), ("new", "abc"))
        self.assertEqual(A.CommandRegistry.parse("/HELP"), ("help", ""))
        self.assertIsNone(A.CommandRegistry.parse("hello"))
        self.assertIsNone(A.CommandRegistry.parse("/ nope"))

    def test_invoke_sync_and_async_handlers(self):
        self.commands.register("echo", "echo", lambda args, **_: {"ok": True, "args": args})

        async def slow(args, **_):
            return {"ok": True, "args": args.upper()}

        self.commands.register("loud", "loud", slow)
        self.assertEqual(run(self.commands.invoke("/echo hi"))["args"], "hi")
        self.assertEqual(run(self.commands.invoke("/loud hi"))["args"], "HI")
        self.assertIsNone(run(self.commands.invoke("plain text")))
        self.assertFalse(run(self.commands.invoke("/nope"))["ok"])

    def test_describe_is_sorted(self):
        self.commands.register("b", "second", lambda a, **_: None)
        self.commands.register("a", "first", lambda a, **_: None)
        self.assertEqual([c["name"] for c in self.commands.describe()], ["a", "b"])


class AgentCommandTest(unittest.TestCase):
    def setUp(self):
        self.agent = A.Agent(console=False, store=A.MemoryStore())
        self.addCleanup(self.agent.close)

    def test_default_commands_manage_sessions(self):
        created = run(self.agent.commands.invoke("/new"))
        session_id = created["session"]["id"]
        listed = run(self.agent.commands.invoke("/sessions"))
        self.assertIn(session_id, [s["id"] for s in listed["sessions"]])
        self.assertTrue(run(self.agent.commands.invoke(f"/use {session_id}"))["ok"])
        self.assertTrue(run(self.agent.commands.invoke(f"/end {session_id}"))["ok"])
        self.assertFalse(run(self.agent.commands.invoke(f"/use {session_id}"))["ok"])

    def test_model_command_updates_config(self):
        run(self.agent.commands.invoke("/model my-model"))
        self.assertEqual(self.agent.config.inference_llm, "my-model")

    def test_vision_command_sets_and_clears_the_vision_model(self):
        self.assertIn("(leader)", run(self.agent.commands.invoke("/vision"))["message"])
        run(self.agent.commands.invoke("/vision my-eyes"))
        self.assertEqual(self.agent.config.inference_vlm, "my-eyes")
        run(self.agent.commands.invoke("/vision none"))
        self.assertEqual(self.agent.config.inference_vlm, "")

    def test_build_vision_delegates_to_the_pool(self):
        calls: list[tuple] = []

        class Pool(A.ModelPool):
            async def describe_image(self, spec, data_url, question="", **kwargs):
                calls.append((spec, data_url, question, kwargs))
                return "described"

        agent = A.Agent(
            console=False, store=A.MemoryStore(), models=Pool(), inference_vlm="v"
        )
        self.addCleanup(agent.close)
        self.assertIsNone(agent.build_vision(A.AgentConfig()))
        delegate = agent.build_vision(agent.config)
        self.assertEqual(run(delegate("data:image/png;base64,AA", "q")), "described")
        spec, data_url, question, kwargs = calls[0]
        self.assertEqual((spec.name, question), ("v", "q"))
        self.assertEqual(kwargs["max_tokens"], agent.config.vision_max_tokens)

    def test_help_lists_commands(self):
        names = [c["name"] for c in run(self.agent.commands.invoke("/help"))["commands"]]
        self.assertIn("help", names)
        self.assertIn("new", names)


class EngineTest(unittest.TestCase):
    """The embeddable loop: no sessions, no storage, no memory, no console."""

    class FakeEngine(A.Engine):
        def build_sdk_agent(self, config, tools):
            self.built = {"config": config, "tools": tools}
            return self.built

        async def stream(self, sdk_agent, model_input, config, result):
            self.inputs = getattr(self, "inputs", [])
            self.inputs.append(model_input)
            result.output = A.prompt_of(model_input).upper()
            return result

    def test_engine_owns_no_batteries(self):
        engine = A.Engine(env=False)
        self.addCleanup(engine.close)
        for battery in ("store", "cache", "sessions", "memory", "repos", "commands"):
            self.assertFalse(hasattr(engine, battery), battery)
        self.assertIsNone(engine.renderer)

    def test_env_layer_can_be_skipped(self):
        import os

        os.environ["AGENTIC_INFERENCE_LLM"] = "env-model"
        self.addCleanup(os.environ.pop, "AGENTIC_INFERENCE_LLM", None)
        self.assertEqual(A.Engine(env=False).config.inference_llm, A.DEFAULT_INFERENCE_LLM)
        self.assertEqual(A.Engine().config.inference_llm, "env-model")

    def test_run_takes_the_model_input_verbatim(self):
        engine = self.FakeEngine(env=False)
        self.addCleanup(engine.close)
        seen: list[str] = []
        engine.events.on(A.EventType.ALL, lambda e: seen.append(e.type))
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]
        result = run(engine.run(messages))
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "THIRD")
        self.assertEqual(result.prompt, "third")
        self.assertEqual(engine.inputs[-1], messages)
        self.assertEqual(seen, ["agent.start", "agent.end"])

    def test_run_has_no_tools_without_a_workspace(self):
        engine = self.FakeEngine(env=False)
        self.addCleanup(engine.close)
        run(engine.run("hello"))
        self.assertEqual(engine.built["tools"], [])

    def test_a_workspace_brings_the_built_in_tools(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        engine = self.FakeEngine(env=False, workspace=root)
        self.addCleanup(engine.close)
        run(engine.run("hello"))
        names = [tool.name for tool in engine.built["tools"]]
        self.assertIn("read_text_file", names)

    def test_the_ask_tool_comes_only_with_a_mode_that_can_answer(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        engine = self.FakeEngine(env=False, workspace=root)
        self.addCleanup(engine.close)
        run(engine.run("hello"))
        self.assertNotIn("ask_user", [tool.name for tool in engine.built["tools"]])
        run(engine.run("hello", ask_enabled=True))
        self.assertIn("ask_user", [tool.name for tool in engine.built["tools"]])

    def test_tools_may_be_supplied_by_the_consumer(self):
        engine = self.FakeEngine(env=False)
        self.addCleanup(engine.close)
        run(engine.run("hello", tools=["mine"]))
        self.assertEqual(engine.built["tools"], ["mine"])

    def test_run_reports_errors_instead_of_raising(self):
        class Boom(self.FakeEngine):
            async def stream(self, sdk_agent, model_input, config, result):
                raise RuntimeError("boom")

        engine = Boom(env=False)
        self.addCleanup(engine.close)
        result = run(engine.run("hello"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "boom")

    def test_overrides_are_merged_per_run(self):
        engine = self.FakeEngine(env=False)
        self.addCleanup(engine.close)
        run(engine.run("hello", instructions="be brief"))
        self.assertEqual(engine.built["config"].instructions, "be brief")
        self.assertEqual(engine.config.instructions, A.DEFAULT_INSTRUCTIONS)

    def test_agent_is_an_engine(self):
        self.assertTrue(issubclass(A.Agent, A.Engine))


class StreamTest(unittest.TestCase):
    """The block stream itself, driven by a scripted SDK run."""

    class FakeStream:
        """What `Runner.run_streamed` returns: events, then an optional stall."""

        def __init__(self, events=(), final_output="", usage=None, stall=0.0, pause=0.0):
            self.events = list(events)
            self.final_output = final_output
            self.context_wrapper = types.SimpleNamespace(usage=usage)
            self.raw_responses = []
            self.cancelled = False
            self.stall = stall
            self.pause = pause

        def cancel(self):
            self.cancelled = True

        async def stream_events(self):
            if self.pause:
                await asyncio.sleep(self.pause)
            for event in self.events:
                yield event
            if self.stall:
                await asyncio.sleep(self.stall)

    @staticmethod
    def delta(text, kind="response.output_text.delta"):
        from agents import RawResponsesStreamEvent

        return RawResponsesStreamEvent(
            data=types.SimpleNamespace(type=kind, delta=text)
        )

    @staticmethod
    def item(name, item):
        from agents import RunItemStreamEvent

        return RunItemStreamEvent(name=name, item=item)

    def drive(self, streamed, **overrides):
        """An engine, the result it will fill and a call that streams `streamed`."""
        import agents

        engine = A.Engine(env=False, **overrides)
        self.addCleanup(engine.close)
        config = engine.resolve_config()
        result = A.RunResult(session_id="s1")

        def stream():
            with unittest.mock.patch.object(
                agents.Runner, "run_streamed", lambda **_: streamed
            ):
                return run(engine.stream({}, "hello", config, result))

        return engine, result, stream

    def test_deltas_become_blocks_and_events(self):
        streamed = self.FakeStream(
            events=[
                self.delta("thinking", "response.reasoning_text.delta"),
                self.delta("hel"),
                self.delta("lo"),
            ],
            usage={"prompt_tokens": 12, "completion_tokens": 3},
        )
        engine, result, stream = self.drive(streamed)
        seen: list[str] = []
        engine.events.on(A.EventType.ALL, lambda e: seen.append(e.type))
        stream()
        self.assertEqual(result.output, "hello")
        self.assertEqual(result.text_of("reasoning"), "thinking")
        self.assertEqual([b.kind for b in result.blocks], ["reasoning", "output"])
        self.assertEqual(seen.count("block.start"), 2)
        self.assertEqual(seen.count("block.end"), 2)
        self.assertEqual(result.usage.input_tokens, 12)
        self.assertEqual(engine.models.usage.output_tokens, 3)

    def test_tool_calls_open_and_close_a_tool_block(self):
        call = types.SimpleNamespace(
            tool_name="read_text_file",
            raw_item=types.SimpleNamespace(call_id="c1", arguments='{"path": "a.txt"}'),
        )
        output = types.SimpleNamespace(output="contents", call_id="c1")
        streamed = self.FakeStream(
            events=[
                self.item("tool_called", call),
                self.item("tool_output", output),
                self.delta("done"),
            ],
            final_output="done",
        )
        _engine, result, stream = self.drive(streamed)
        stream()
        self.assertEqual([t.name for t in result.tools], ["read_text_file"])
        self.assertTrue(result.tools[0].done)
        self.assertEqual(result.tools[0].result, "contents")
        self.assertEqual(result.output, "done")

    def test_a_stalled_stream_fails_the_attempt_and_is_cancelled(self):
        streamed = self.FakeStream(stall=5.0)
        _engine, _result, stream = self.drive(streamed, stream_timeout=0.01)
        with self.assertRaises(A.ModelError) as caught:
            stream()
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertTrue(streamed.cancelled)

    def test_what_a_failed_attempt_spent_is_still_accounted(self):
        streamed = self.FakeStream(
            events=[self.delta("partial")],
            usage={"prompt_tokens": 8},
            stall=5.0,
        )
        _engine, result, stream = self.drive(streamed, stream_timeout=0.01)
        with self.assertRaises(A.ModelError):
            stream()
        self.assertEqual(result.usage.input_tokens, 8)

    def test_a_silence_longer_than_a_request_is_not_a_stall(self):
        # The model writing a long tool call through a provider that sends the
        # call whole (Ollama) is silent for minutes; only `stream_timeout`
        # bounds that, never the per-request timeout of the specialists.
        streamed = self.FakeStream(
            events=[self.delta("writing")], final_output="done", stall=0.2
        )
        _engine, result, stream = self.drive(
            streamed, request_timeout=0.01, stream_timeout=5.0
        )
        stream()
        self.assertEqual(result.output, "done")

    def test_a_run_survives_an_sdk_that_reports_no_usage(self):
        streamed = self.FakeStream(events=[self.delta("hi")])
        _engine, result, stream = self.drive(streamed)
        stream()
        self.assertEqual(result.output, "hi")
        self.assertEqual(result.usage.requests, 0)

    def test_a_silent_model_is_published_as_waiting(self):
        streamed = self.FakeStream(events=[self.delta("hi")], pause=0.2)
        engine, _result, stream = self.drive(
            streamed, stream_heartbeat=0.05, stream_timeout=5.0
        )
        waits: list[A.Event] = []
        engine.events.on(A.EventType.MODEL_WAIT, waits.append)
        stream()
        self.assertGreaterEqual(len(waits), 2)
        self.assertEqual(waits[0].session_id, "s1")
        self.assertEqual(waits[0].data["role"], A.LEADER_ROLE)
        self.assertEqual(waits[0].data["timeout"], 5.0)
        self.assertGreater(waits[-1].data["elapsed"], waits[0].data["elapsed"])

    def test_an_open_tool_call_is_not_a_silent_model(self):
        call = types.SimpleNamespace(
            tool_name="run_shell_command",
            raw_item=types.SimpleNamespace(call_id="c1", arguments="{}"),
        )
        streamed = self.FakeStream(events=[self.item("tool_called", call)], stall=0.2)
        engine, _result, stream = self.drive(streamed, stream_heartbeat=0.05)
        waits: list[A.Event] = []
        engine.events.on(A.EventType.MODEL_WAIT, waits.append)
        stream()
        self.assertEqual(waits, [])

    def test_the_wait_heartbeat_can_be_turned_off(self):
        streamed = self.FakeStream(events=[self.delta("hi")], pause=0.15)
        engine, _result, stream = self.drive(streamed, stream_heartbeat=0.0)
        waits: list[A.Event] = []
        engine.events.on(A.EventType.MODEL_WAIT, waits.append)
        stream()
        self.assertEqual(waits, [])


class ToolCallTest(unittest.TestCase):
    """Tool calls are published with their name, arguments, outcome and time."""

    def setUp(self):
        self.engine = A.Engine(env=False)
        self.addCleanup(self.engine.close)

    @staticmethod
    def call_item(name="read_text_file", arguments='{"path": "a.txt"}', call_id="c1"):
        raw = types.SimpleNamespace(name=name, arguments=arguments, call_id=call_id)
        return types.SimpleNamespace(raw_item=raw, tool_name=name)

    @staticmethod
    def output_item(output="contents", call_id="c1"):
        return types.SimpleNamespace(
            raw_item={"call_id": call_id, "output": output},
            output=output,
            call_id=call_id,
        )

    def test_secrets_are_redacted_and_long_values_are_cut(self):
        redacted = A.redact({"path": "a.txt", "api_key": "sk-1", "n": 2, "big": "x" * 20}, 10)
        self.assertEqual(redacted["path"], "a.txt")
        self.assertEqual(redacted["api_key"], A.REDACTED)
        self.assertEqual(redacted["n"], 2)
        self.assertEqual(redacted["big"], "x" * 10 + "\u2026")
        self.assertEqual(A.redact([{"token": "t"}]), [{"token": A.REDACTED}])

    def test_arguments_are_parsed_from_the_json_of_the_model(self):
        self.assertEqual(self.engine.redact_arguments('{"path": "a"}'), {"path": "a"})
        self.assertEqual(self.engine.redact_arguments(""), {})
        self.assertEqual(self.engine.redact_arguments(None), {})
        self.assertEqual(self.engine.redact_arguments("not json"), "not json")

    def test_a_call_reports_its_signature_then_its_outcome(self):
        call = self.engine.tool_call_of(self.call_item(), "b0")
        self.assertEqual(call.name, "read_text_file")
        self.assertEqual(call.call_id, "c1")
        self.assertEqual(call.signature(), 'read_text_file(path="a.txt")')
        self.assertFalse(call.done)
        call.finish("contents")
        self.assertTrue(call.done)
        self.assertIn("-> ok in", call.report())
        self.assertIn("contents", call.report())
        self.assertGreaterEqual(call.duration, 0.0)
        self.assertEqual(call.to_dict()["name"], "read_text_file")

    def test_an_error_result_marks_the_call_as_failed(self):
        self.assertEqual(self.engine.tool_result_of(self.output_item("done")), ("done", True))
        text, ok = self.engine.tool_result_of(self.output_item("Error: nope"))
        self.assertEqual((text, ok), ("Error: nope", False))

    def test_an_output_is_matched_to_its_call(self):
        result = A.RunResult()
        first = A.ToolCall(id="b0", name="a", call_id="c1")
        second = A.ToolCall(id="b1", name="b", call_id="c2")
        result.tools.extend([first, second])
        self.assertIs(A.Engine.pending_tool(result, "c2"), second)
        self.assertIs(A.Engine.pending_tool(result, None), first)
        first.finish("x")
        second.finish("y")
        self.assertIsNone(A.Engine.pending_tool(result, "missing"))

    def test_the_stream_publishes_a_call_from_start_to_end(self):
        from agents import RunItemStreamEvent

        events = [
            RunItemStreamEvent(name="tool_called", item=self.call_item()),
            RunItemStreamEvent(name="tool_output", item=self.output_item()),
        ]

        class Streamed:
            final_output = "done"

            async def stream_events(self):
                for event in events:
                    yield event

        seen = []
        self.engine.events.on(A.EventType.ALL, lambda e: seen.append(e))
        result = A.RunResult(session_id="s1")
        with unittest.mock.patch("agents.Runner.run_streamed", return_value=Streamed()):
            run(self.engine.stream(None, "hi", self.engine.config, result))

        self.assertEqual([e.type for e in seen], ["tool.start", "tool.end"])
        start, end = (e.data for e in seen)
        self.assertEqual(start["name"], "read_text_file")
        self.assertEqual(start["arguments"], {"path": "a.txt"})
        self.assertEqual(start["kind"], "tool")
        self.assertEqual(end["result"], "contents")
        self.assertTrue(end["ok"])
        self.assertGreaterEqual(end["duration"], 0.0)
        self.assertEqual(end["id"], start["id"])
        self.assertEqual(len(result.tools), 1)
        self.assertEqual(result.text_of("tool"), result.tools[0].report())
        self.assertEqual(result.output, "done")

    def test_runs_of_one_session_never_reuse_a_block_id(self):
        from agents import RunItemStreamEvent

        class Streamed:
            final_output = "done"

            async def stream_events(inner):
                yield RunItemStreamEvent(name="tool_called", item=self.call_item())

        seen = []
        self.engine.events.on(A.EventType.TOOL_START, lambda e: seen.append(e.data["id"]))
        for _ in range(2):
            result = A.RunResult(session_id="s1")
            with unittest.mock.patch("agents.Runner.run_streamed", return_value=Streamed()):
                run(self.engine.stream(None, "hi", self.engine.config, result))
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])


class AgentRunTest(unittest.TestCase):
    """Exercises the run pipeline with the SDK call stubbed out."""

    class FakeAgent(A.Agent):
        def build_sdk_agent(self, config, tools):
            return {"config": config, "tools": tools}

        async def stream(self, sdk_agent, model_input, config, result):
            self.inputs = getattr(self, "inputs", [])
            self.inputs.append(model_input)
            prompt = A.prompt_of(model_input)
            session_id = result.session_id
            block = A.Block(id="b0", kind="output", session_id=session_id)
            result.blocks.append(block)
            await self.events.publish(
                A.EventType.BLOCK_START, session_id=session_id, id=block.id, kind="output"
            )
            block.text = prompt.upper()
            await self.events.publish(
                A.EventType.BLOCK_DELTA,
                session_id=session_id,
                id=block.id,
                kind="output",
                text=block.text,
            )
            result.output = block.text
            return result

    def setUp(self):
        self.agent = self.FakeAgent(console=False, store=A.MemoryStore())
        self.addCleanup(self.agent.close)

    def test_run_renders_template_and_publishes_events(self):
        seen: list[str] = []
        self.agent.events.on(A.EventType.ALL, lambda e: seen.append(e.type))
        result = run(self.agent.run("Say: {{ config.input }}", input="hello"))
        self.assertTrue(result.ok)
        self.assertEqual(result.prompt, "Say: hello")
        self.assertEqual(result.output, "SAY: HELLO")
        self.assertEqual(result.text_of("output"), "SAY: HELLO")
        # the prompt is the first block of the run, then the answer streams
        self.assertEqual(
            seen, ["agent.start", "block.start", "block.start", "block.delta", "agent.end"]
        )

    def test_run_reuses_a_given_session(self):
        session = self.agent.sessions.create()
        result = run(self.agent.run("{{ config.input }}", session=session, input="x"))
        self.assertEqual(result.session_id, session.id)

    def test_run_reports_errors_instead_of_raising(self):
        result = run(self.agent.run("{{ missing }}", input="x"))
        self.assertFalse(result.ok)
        self.assertIn("missing", result.error)

    def test_attachments_are_appended_to_the_prompt(self):
        result = run(
            self.agent.run("Base", input="x", attachments=["attachments/a.png"])
        )
        self.assertIn("## Attached files", result.prompt)
        self.assertIn("attachments/a.png", result.prompt)

    def test_close_wipes_every_workspace(self):
        result = run(self.agent.run("{{ config.input }}", input="x"))
        workspace = self.agent.sessions.get(result.session_id).workspace
        self.agent.close()
        self.assertFalse(workspace.exists())

    def test_durable_sessions_survive_a_restart(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        db = root / "agent.db"
        first = self.FakeAgent(
            console=False,
            store=A.SqliteStore(db),
            workspace_root=root,
            session_durable=True,
        )
        result = run(first.run("{{ config.input }}", input="hello"))
        workspace = first.sessions.get(result.session_id).workspace
        first.close()
        self.assertTrue(workspace.is_dir())
        second = self.FakeAgent(
            console=False,
            store=A.SqliteStore(db),
            workspace_root=root,
            session_durable=True,
        )
        self.addCleanup(lambda: second.sessions.close_all(destroy=True))
        restored = second.sessions.get(result.session_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.workspace, workspace)
        self.assertEqual(
            second.memory.history(result.session_id),
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "HELLO"},
            ],
        )

    def test_a_second_run_replays_the_conversation(self):
        session = self.agent.sessions.create()
        run(self.agent.run("{{ config.input }}", session=session, input="hello"))
        run(self.agent.run("{{ config.input }}", session=session, input="again"))
        self.assertEqual(self.agent.inputs[0], "hello")
        self.assertEqual(
            self.agent.inputs[1],
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "HELLO"},
                {"role": "user", "content": "again"},
            ],
        )

    def test_the_input_is_remembered_rather_than_the_rendered_prompt(self):
        session = self.agent.sessions.create()
        run(self.agent.run("Say: {{ config.input }}", session=session, input="hello"))
        self.assertEqual(
            self.agent.memory.history(session.id),
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "SAY: HELLO"},
            ],
        )

    def test_a_failed_run_is_not_remembered(self):
        result = run(self.agent.run("{{ missing }}", input="x"))
        self.assertEqual(self.agent.memory.history(result.session_id), [])

    def test_memory_can_be_disabled(self):
        agent = self.FakeAgent(console=False, store=A.MemoryStore(), memory_enabled=False)
        self.addCleanup(agent.close)
        session = agent.sessions.create()
        run(agent.run("{{ config.input }}", session=session, input="hello"))
        run(agent.run("{{ config.input }}", session=session, input="again"))
        self.assertEqual(agent.inputs, ["hello", "again"])
        self.assertEqual(agent.memory.history(session.id), [])

    def test_forget_clears_the_conversation_of_a_session(self):
        session = self.agent.sessions.create()
        run(self.agent.run("{{ config.input }}", session=session, input="hello"))
        result = run(self.agent.commands.invoke("/forget", session_id=session.id))
        self.assertTrue(result["ok"])
        self.assertEqual(self.agent.memory.history(session.id), [])

    def test_ending_a_session_forgets_it(self):
        session = self.agent.sessions.create()
        run(self.agent.run("{{ config.input }}", session=session, input="hello"))
        run(self.agent.commands.invoke("/end", session_id=session.id))
        self.assertEqual(self.agent.memory.history(session.id), [])

    def test_a_summarizer_is_built_unless_switched_off(self):
        self.assertIsNotNone(self.agent.build_summarizer(self.agent.config))
        off = self.agent.config.merge(context_summary=False)
        self.assertIsNone(self.agent.build_summarizer(off))


class ConsoleRendererTest(unittest.TestCase):
    class Buffer:
        def __init__(self, tty=False):
            self.text = ""
            self.chunks: list[str] = []
            self.tty = tty

        def write(self, chunk):
            self.text += chunk
            if chunk:
                self.chunks.append(chunk)

        def flush(self):
            pass

        def isatty(self):
            return self.tty

    def setUp(self):
        self.buffer = self.Buffer()
        self.renderer = A.ConsoleRenderer(color=False, stream=self.buffer)

    @staticmethod
    def delta(kind, text):
        return A.Event(type=A.EventType.BLOCK_DELTA, data={"kind": kind, "text": text})

    def test_a_compaction_and_the_context_use_are_reported(self):
        usage = A.ContextBudget.of(128_000).usage(51_200)
        self.renderer.handle(A.Event(type=A.EventType.CONTEXT_USAGE, data=usage))
        self.renderer.handle(
            A.Event(type=A.EventType.CONTEXT_COMPACT, data={"message": "Compacted 90k → 40k tokens"})
        )
        self.renderer.handle(A.Event(type=A.EventType.AGENT_END, data={}))
        self.assertIn("~~ Compacted 90k → 40k tokens", self.buffer.text)
        self.assertIn("ctx 50% · 51k / 102k", self.buffer.text)

    def test_only_a_terminal_gathers_text_into_frames(self):
        self.assertEqual(A.ConsoleRenderer(stream=self.Buffer()).frame, 0.0)
        tty = A.ConsoleRenderer(stream=self.Buffer(tty=True))
        self.assertEqual(tty.frame, A.ConsoleRenderer.FRAME)

    def test_streamed_text_is_written_once_per_frame(self):
        renderer = A.ConsoleRenderer(color=False, stream=self.buffer, frame=0.05)

        async def stream():
            for token in ("a", "b", "c"):
                renderer.handle(self.delta("output", token))
            self.assertEqual(self.buffer.text, "")
            await asyncio.sleep(0.15)

        run(stream())
        self.assertEqual(self.buffer.chunks, ["abc"])

    def test_frames_change_when_text_is_written_not_what(self):
        def render(frame):
            buffer = self.Buffer()
            renderer = A.ConsoleRenderer(color=False, stream=buffer, frame=frame)

            async def stream():
                renderer.handle(self.delta("reasoning", "think"))
                renderer.handle(self.delta("reasoning", "ing"))
                renderer.handle(self.delta("output", "answer"))
                renderer.emit("error", "\n(cancelled)\n")
                renderer.handle(A.Event(type=A.EventType.AGENT_END, data={}))

            run(stream())
            return buffer

        framed, direct = render(10), render(0)
        self.assertEqual(framed.text, direct.text)
        self.assertLess(len(framed.chunks), len(direct.chunks))

    def test_without_a_loop_framed_text_is_written_at_once(self):
        renderer = A.ConsoleRenderer(color=False, stream=self.buffer, frame=10)
        renderer.handle(self.delta("output", "now"))
        self.assertEqual(self.buffer.text, "now")

    def test_block_changes_insert_one_blank_line(self):
        self.renderer.emit("reasoning", "thinking")
        self.renderer.emit("output", "answer")
        self.assertEqual(self.buffer.text, "thinking\n\nanswer")

    def test_existing_newlines_are_not_doubled(self):
        self.renderer.emit("output", "line\n\n")
        self.renderer.emit("reasoning", "\n\nnext")
        self.assertEqual(self.buffer.text, "line\n\nnext")

    def test_empty_text_is_ignored(self):
        self.renderer.emit("output", "")
        self.assertEqual(self.buffer.text, "")

    def test_a_retry_is_announced(self):
        self.renderer.handle(
            A.Event(
                type=A.EventType.MODEL_RETRY,
                data={
                    "role": "leader",
                    "error_kind": "rate_limit",
                    "attempt": 1,
                    "attempts": 3,
                    "delay": 0.75,
                },
            )
        )
        self.assertEqual(
            self.buffer.text, "!! leader model rate_limit: retry 2/3 in 0.8s\n"
        )

    def test_color_styles_are_applied_when_enabled(self):
        renderer = A.ConsoleRenderer(color=True, stream=self.Buffer())
        self.assertTrue(renderer.style("reasoning", "x").endswith("\x1b[0m"))
        self.assertEqual(A.ConsoleRenderer(color=False, stream=self.buffer).style("x", "y"), "y")

    def test_banner_is_boxed(self):
        self.renderer.banner("title", [("Key", "value")])
        lines = self.buffer.text.splitlines()
        self.assertTrue(lines[0].startswith("+---"))
        self.assertIn("Key -> value", self.buffer.text)

    def test_renderer_subscribes_to_a_bus(self):
        bus = A.EventBus()
        self.renderer.attach(bus)
        run(bus.publish(A.EventType.BLOCK_DELTA, kind="output", text="hi"))
        self.assertEqual(self.buffer.text, "hi")

    def test_tool_calls_are_printed_with_their_outcome(self):
        bus = A.EventBus()
        self.renderer.attach(bus)
        run(bus.publish(A.EventType.TOOL_START, kind="tool", text='read(path="a")'))
        run(
            bus.publish(
                A.EventType.TOOL_END,
                kind="tool",
                name="read",
                ok=False,
                duration=0.002,
                result="Error: nope",
            )
        )
        self.assertIn('-> read(path="a")', self.buffer.text)
        self.assertIn("<- read: failed in 2 ms", self.buffer.text)
        self.assertIn("Error: nope", self.buffer.text)

    def test_a_running_tool_prints_a_dot_per_heartbeat(self):
        bus = A.EventBus()
        self.renderer.attach(bus)
        run(bus.publish(A.EventType.TOOL_START, kind="tool", text="sh()"))
        run(bus.publish(A.EventType.TOOL_PROGRESS, kind="tool", name="sh", text=""))
        run(bus.publish(A.EventType.TOOL_PROGRESS, kind="tool", name="sh", text="output"))
        run(bus.publish(A.EventType.TOOL_PROGRESS, kind="tool", name="sh", text=""))
        run(bus.publish(A.EventType.TOOL_END, kind="tool", name="sh", duration=1.0))
        self.assertEqual(self.buffer.text, "-> sh()\n..\n<- sh: ok in 1000 ms\n")

    def test_truncate(self):
        self.assertEqual(A.truncate("abcdef", 10), "abcdef")
        self.assertEqual(A.truncate("abcdefghij", 6), "abc...")
        self.assertEqual(A.truncate("/a/very/long/path.txt", 10), "...ath.txt")


class ThemeTest(unittest.TestCase):
    def test_vscode_theme_maps_onto_css_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "theme.json"
            path.write_text(
                json.dumps(
                    {
                        "type": "dark",
                        "colors": {
                            "editor.background": "#101010",
                            "editor.foreground": "#f0f0f0",
                            "unsupported.key": "#123456",
                        },
                    }
                ),
                encoding="utf-8",
            )
            theme = A.load_vscode_theme(path)
        self.assertEqual(theme["--bg"], "#101010")
        self.assertEqual(theme["--fg"], "#f0f0f0")
        self.assertEqual(theme["--theme-type"], "dark")
        self.assertNotIn("unsupported.key", theme)

    def test_missing_theme_is_empty(self):
        self.assertEqual(A.load_vscode_theme(None), {})

    def write_theme(self, **data) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "theme.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_a_variable_falls_back_to_the_next_color_the_theme_names(self):
        path = self.write_theme(
            colors={
                "editorGroupHeader.tabsBackground": "#202020",
                "editorWidget.border": "#303030",
            }
        )
        theme = A.load_vscode_theme(path)
        self.assertEqual(theme["--bg-soft"], "#202020")
        self.assertEqual(theme["--border"], "#303030")

    def test_a_variable_no_color_covers_is_left_to_the_stylesheet(self):
        theme = A.load_vscode_theme(self.write_theme(colors={"editor.background": "#101010"}))
        self.assertNotIn("--accent", theme)
        self.assertNotIn("--muted", theme)

    def test_a_code_surface_the_color_of_the_page_is_skipped(self):
        path = self.write_theme(
            colors={
                "editor.background": "#101010",
                "textCodeBlock.background": "#101010",
                "editorWidget.background": "#202020",
            }
        )
        self.assertEqual(A.load_vscode_theme(path)["--code-bg"], "#202020")

    def test_a_code_surface_with_no_distinct_candidate_is_left_out(self):
        path = self.write_theme(
            colors={"editor.background": "#101010", "textCodeBlock.background": "#101010"}
        )
        self.assertNotIn("--code-bg", A.load_vscode_theme(path))

    def test_theme_types_are_normalised(self):
        for raw, expected in (
            ("light", "light"),
            ("hcDark", "hc-dark"),
            ("hc", "hc-dark"),
            ("hcLight", "hc-light"),
            ("dark", "dark"),
            (None, "dark"),
            ("nonsense", "dark"),
        ):
            self.assertEqual(A.theme_type(raw), expected, raw)


class HubTest(unittest.TestCase):
    class FakeSocket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    def setUp(self):
        self.hub = A.Hub()
        self.socket = self.FakeSocket()
        self.connection = A.Connection(self.hub, self.socket)
        self.hub.connections[self.connection.id] = self.connection

    def test_channels_receive_broadcasts(self):
        self.hub.join(self.connection, "room")
        run(self.hub.broadcast("room", "tick", n=1))
        self.hub.leave(self.connection, "room")
        run(self.hub.broadcast("room", "tick", n=2))
        self.assertEqual(self.socket.sent, [{"type": "tick", "n": 1}])

    def test_dispatch_routes_to_handlers(self):
        seen = []
        self.hub.on("hi", lambda conn, msg: seen.append(msg["v"]))
        run(self.hub.dispatch(self.connection, {"type": "hi", "v": 7}))
        self.assertEqual(seen, [7])

    def test_dispatch_reports_unknown_types(self):
        run(self.hub.dispatch(self.connection, {"type": "nope"}))
        self.assertEqual(self.socket.sent[0]["type"], "error")


class WebServerTest(unittest.TestCase):
    def setUp(self):
        self.agent = A.Agent(console=False, store=A.MemoryStore())
        self.addCleanup(self.agent.close)
        self.server = A.WebServer(self.agent, template="{{ config.input }}")

    def test_rest_endpoints(self):
        status, body = self.server.rest("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIn("inference_llm", json.loads(self.server.rest("/api/config")[1]))
        self.assertIsInstance(json.loads(self.server.rest("/api/commands")[1]), list)
        self.assertIsNone(self.server.rest("/api/unknown"))

    def test_reads_stay_inside_the_workspace_by_default(self):
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=False, env=False)
        self.addCleanup(agent.close)
        A.WebServer(agent)
        self.assertIs(agent.config.outside_read, False)
        self.assertIs(agent.config.outside_write, False)

    def test_an_explicit_outside_flag_wins_over_the_web_default(self):
        agent = A.Agent(
            console=False, store=A.MemoryStore(), config_file=False, env=False, outside_read=True
        )
        self.addCleanup(agent.close)
        A.WebServer(agent)
        self.assertIs(agent.config.outside_read, True)

    def test_page_assets_exist_and_are_linked(self):
        for name, _ in A.WebServer.ASSETS.values():
            self.assertTrue((A.ROOT / name).is_file(), name)
        page = (A.ROOT / A.WebServer.ASSETS["/"][0]).read_text(encoding="utf-8")
        for path in ("/ui.css", "/ui.js"):
            self.assertIn(path, page)
            self.assertIn(path, A.WebServer.ASSETS)

    def test_the_banner_is_printed_once_not_per_message(self):
        out = io.StringIO()
        self.agent.renderer = A.ConsoleRenderer(color=False, stream=out)
        self.agent.renderer.attach(self.agent.events)
        self.server.greet()
        run(
            self.agent.events.emit(
                A.Event(type=A.EventType.AGENT_START, data={"config": self.agent.config})
            )
        )
        self.assertEqual(out.getvalue().count(self.agent.config.name), 1)
        self.assertFalse(self.agent.renderer.banners)

    def test_event_payloads_drop_non_serializable_values(self):
        event = A.Event(
            type=A.EventType.BLOCK_DELTA, data={"text": "x", "config": A.AgentConfig()}
        )
        self.assertEqual(self.server.encode(event), {"text": "x"})

    def test_bind_moves_a_connection_between_channels(self):
        connection = A.Connection(self.server.hub, HubTest.FakeSocket())
        self.server.hub.connections[connection.id] = connection
        self.server.bind(connection, "one")
        self.server.bind(connection, "two")
        self.assertEqual(connection.channels, {A.WebServer.channel("two")})

    def test_hello_replays_the_conversation_of_the_session(self):
        session = self.agent.sessions.create()
        run(
            self.agent.memory.remember(
                session.id, [A.Turn("user", "hello"), A.Turn("assistant", "hi")]
            )
        )
        connection = A.Connection(self.server.hub, HubTest.FakeSocket())
        self.server.hub.connections[connection.id] = connection
        run(self.server.on_hello(connection, {"session": session.id}))
        hello = connection.ws.sent[-1]
        self.assertEqual(hello["session"]["id"], session.id)
        self.assertEqual(
            [(t["role"], t["text"]) for t in hello["history"]],
            [("user", "hello"), ("assistant", "hi")],
        )

    def connect(self, session_id=None):
        connection = A.Connection(self.server.hub, HubTest.FakeSocket())
        self.server.hub.connections[connection.id] = connection
        if session_id:
            self.server.bind(connection, session_id)
        return connection

    def test_hello_resumes_a_live_session_and_replaces_a_stale_one(self):
        live = self.agent.sessions.create()
        connection = self.connect()
        run(self.server.on_hello(connection, {"session": live.id}))
        self.assertEqual(connection.ws.sent[-1]["session"]["id"], live.id)
        self.agent.sessions.close(live.id)
        run(self.server.on_hello(connection, {"session": live.id}))
        fresh = connection.ws.sent[-1]["session"]["id"]
        self.assertNotEqual(fresh, live.id)
        self.assertIsNotNone(self.agent.sessions.get(fresh))

    def test_the_web_server_lets_the_model_ask(self):
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=False, env=False)
        self.addCleanup(agent.close)
        self.assertIsNone(agent.config.ask_enabled)
        A.WebServer(agent)
        self.assertIs(agent.config.ask_enabled, True)

    def test_an_answer_reaches_the_question_of_its_session(self):
        session = self.agent.sessions.create()

        async def drive():
            result = A.RunResult(session_id=session.id)
            asker = self.agent.build_asker(self.agent.config, result)
            task = asyncio.create_task(asker(None, "Which one?", ["red", "blue"]))
            while not self.agent.questions:
                await asyncio.sleep(0.005)
            question = next(iter(self.agent.questions))
            stranger = self.connect(self.agent.sessions.create().id)
            await self.server.on_answer(stranger, {"id": question, "text": "red"})
            owner = self.connect(session.id)
            await self.server.on_answer(owner, {"id": question, "text": "blue"})
            return stranger, owner, await task

        stranger, owner, answer = run(drive())
        self.assertEqual(answer, "blue")
        self.assertEqual(stranger.ws.sent[-1]["type"], "error")
        # the owner hears the outcome on its channel, like every client of it
        self.assertEqual([m["type"] for m in owner.ws.sent], ["question.end"])
        self.assertEqual(owner.ws.sent[0]["answer"], "blue")

    def test_attachments_are_stored_inside_the_session(self):
        session = self.agent.sessions.create()
        saved = self.server.store_attachments(
            session,
            [{"name": "../../evil name.png", "data": base64.b64encode(b"png").decode()}],
        )
        self.assertEqual(saved, ["attachments/evil_name.png"])
        self.assertEqual((session.workspace / saved[0]).read_bytes(), b"png")

    def test_oversized_attachments_are_rejected(self):
        session = self.agent.sessions.create()
        blob = base64.b64encode(b"x" * (A.MAX_ATTACHMENT_BYTES + 1)).decode()
        with self.assertRaises(ValueError):
            self.server.store_attachments(session, [{"name": "big.bin", "data": blob}])

    def test_invalid_attachment_encoding_is_rejected(self):
        session = self.agent.sessions.create()
        with self.assertRaises(ValueError):
            self.server.store_attachments(session, [{"name": "a.bin", "data": "@@@"}])

    def test_safe_name(self):
        self.assertEqual(A.safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(A.safe_name(""), "file")
        self.assertEqual(A.safe_name("a b?c.txt"), "a_b_c.txt")

    def test_content_type_replaces_the_one_the_transport_set(self):
        from websockets.datastructures import Headers

        response = types.SimpleNamespace(headers=Headers())
        response.headers["Content-Type"] = "text/plain; charset=utf-8"
        A.WebServer.retype(response, "text/css; charset=utf-8")
        self.assertEqual(
            response.headers.get_all("Content-Type"), ["text/css; charset=utf-8"]
        )


class E2eCaptureTest(unittest.TestCase):
    """The scripted agent behind `e2e.py`, without a browser."""

    def setUp(self):
        import test.agentic_e2e as E

        self.E = E
        self.gate = E.Gate()
        self.agent = E.ScriptedAgent(
            console=False,
            env=False,
            config_file=False,
            gate=self.gate,
            pace=0.0,
            store=A.MemoryStore(),
        )
        self.addCleanup(self.agent.close)

    def test_chunks_rebuild_the_text_they_came_from(self):
        text = self.E.ANSWER_HEAD + self.E.ANSWER_TAIL
        self.assertEqual("".join(self.E.chunks(text)), text)

    def test_the_run_holds_until_the_capture_releases_it(self):
        seen = []
        self.agent.events.on(
            A.EventType.ALL,
            lambda event: seen.append((event.type, str(event.data.get("id", "")))),
        )

        async def drive():
            task = asyncio.ensure_future(self.agent.run("{{ config.input }}", input=self.E.PROMPT))
            while not self.gate.reached.is_set():
                await asyncio.sleep(0.01)
            held = list(seen)
            self.gate.release()
            return held, await task

        held, result = run(drive())
        types = [event for event, _id in held]
        ended = [id for event, id in held if event == A.EventType.BLOCK_END]
        self.assertIn(A.EventType.TOOL_END, types)
        # Reasoning and the tool call are behind the gate; the answer is not:
        # it is half written, which is what the middle frame photographs.
        self.assertTrue(any(id.endswith("-reasoning") for id in ended))
        self.assertFalse(any(id.endswith("-answer") for id in ended))
        self.assertTrue(result.ok)
        self.assertEqual(result.output, self.E.ANSWER_HEAD + self.E.ANSWER_TAIL)
        self.assertEqual([call.name for call in result.tools], [self.E.TOOL_NAME])

    def test_the_reasoning_is_streamed_before_the_answer(self):
        """The page can only show a reason block if the run publishes one."""
        seen = []
        self.agent.events.on(
            A.EventType.BLOCK_START,
            lambda event: seen.append(str(event.data.get("kind", ""))),
        )

        async def drive():
            task = asyncio.ensure_future(self.agent.run("{{ config.input }}", input=self.E.PROMPT))
            while not self.gate.reached.is_set():
                await asyncio.sleep(0.01)
            self.gate.release()
            return await task

        result = run(drive())
        self.assertEqual(seen, ["prompt", "reasoning", "output"])
        self.assertEqual(result.text_of("reasoning"), self.E.REASONING)


class ConfigFileTest(unittest.TestCase):
    """The file layer: JSON or YAML, the cwd first and the module next to it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
        return path

    def test_candidates_are_the_cwd_then_the_module_directory(self):
        names = [p.name for p in A.config_candidates(directories=[self.tmp])]
        self.assertEqual(names, ["agentic.json", "agentic.yaml", "settings.yml"])
        directories = A.config_directories()
        self.assertEqual(directories[0], Path.cwd())
        self.assertIn(A.ROOT, directories)

    def test_the_cwd_wins_over_the_module_directory(self):
        near, far = self.tmp / "near", self.tmp / "far"
        near.mkdir()
        far.mkdir()
        (far / "agentic.yaml").write_text("inference_llm: far\n", encoding="utf-8")
        self.assertEqual(
            A.find_config_file(directories=[near, far]), far / "agentic.yaml"
        )
        (near / "agentic.yaml").write_text("inference_llm: near\n", encoding="utf-8")
        self.assertEqual(
            A.find_config_file(directories=[near, far]), near / "agentic.yaml"
        )

    def test_a_missing_file_is_not_a_find(self):
        self.assertIsNone(A.find_config_file(directories=[self.tmp]))

    def test_json_and_yaml_read_the_same_way(self):
        js = self.write("agentic.json", '{"model": "j", "port": 9001}')
        ya = self.write("agentic.yaml", "inference_llm: y\nport: 9002")
        self.assertEqual(A.read_config_file(js)["model"], "j")
        self.assertEqual(A.read_config_file(ya)["port"], 9002)

    def test_values_are_coerced_to_the_field_types(self):
        path = self.write(
            "agentic.yaml",
            """
            inference_llm: file-model
            port: 9100
            quiet: true
            theme: /tmp/theme.json
            retry_backoff: 2
            max_turns: "7"
            """,
        )
        config = A.AgentConfig().with_file(path)
        self.assertEqual(config.inference_llm, "file-model")
        self.assertEqual(config.port, 9100)
        self.assertIs(config.quiet, True)
        self.assertEqual(config.theme, Path("/tmp/theme.json"))
        self.assertEqual(config.retry_backoff, 2.0)
        self.assertEqual(config.max_turns, 7)

    def test_nested_groups_flatten_onto_field_names(self):
        path = self.write(
            "agentic.yaml",
            """
            inference:
              vlm: eyes
            vision:
              max_tokens: 33
            memory:
              enabled: false
            """,
        )
        config = A.AgentConfig().with_file(path)
        self.assertEqual(config.inference_vlm, "eyes")
        self.assertEqual(config.vision_max_tokens, 33)
        self.assertFalse(config.memory_enabled)

    def test_unknown_keys_and_nested_extras_land_in_extras(self):
        path = self.write(
            "agentic.yaml",
            """
            genre: noir
            profile:
              tone: dry
            extras:
              mood: calm
            """,
        )
        config = A.AgentConfig().with_file(path)
        self.assertEqual(config.genre, "noir")
        self.assertEqual(config.profile, {"tone": "dry"})
        self.assertEqual(config.mood, "calm")

    def test_null_leaves_the_default_in_place(self):
        path = self.write("agentic.yaml", "inference_llm: null")
        self.assertEqual(A.AgentConfig().with_file(path).inference_llm, A.DEFAULT_INFERENCE_LLM)

    def test_an_empty_file_is_harmless(self):
        self.assertEqual(A.read_config_file(self.write("agentic.yaml", "")), {})

    def test_a_file_that_is_not_a_mapping_is_rejected(self):
        path = self.write("agentic.json", "[1, 2, 3]")
        with self.assertRaises(ValueError):
            A.read_config_file(path)

    def test_without_a_path_nothing_found_means_no_change(self):
        with unittest.mock.patch.object(A, "find_config_file", return_value=None):
            self.assertEqual(A.AgentConfig(inference_llm="m").with_file().inference_llm, "m")

    def test_the_engine_layers_file_then_environment_then_arguments(self):
        path = self.write("agentic.yaml", "inference_llm: from-file\nport: 9100")
        engine = A.Engine(config_file=path, env=False)
        self.assertEqual(engine.config_file, path)
        self.assertEqual(engine.config.inference_llm, "from-file")
        self.assertEqual(engine.config.port, 9100)
        with unittest.mock.patch.dict(
            "os.environ", {"AGENTIC_INFERENCE_LLM": "from-env"}, clear=False
        ):
            engine = A.Engine(config_file=path)
            self.assertEqual(engine.config.inference_llm, "from-env")
            self.assertEqual(engine.config.port, 9100)
            engine = A.Engine(config_file=path, inference_llm="from-argument")
            self.assertEqual(engine.config.inference_llm, "from-argument")

    def test_a_file_in_the_working_directory_is_found_on_its_own(self):
        self.write("agentic.yaml", "inference_llm: from-the-cwd")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp)
        self.assertEqual(A.Engine(env=False).config.inference_llm, "from-the-cwd")
        self.assertIsNone(A.Engine(config_file=False, env=False).config_file)
        self.assertEqual(
            A.Engine(config_file=False, env=False).config.inference_llm, A.DEFAULT_INFERENCE_LLM
        )

    def test_a_named_file_that_does_not_exist_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            A.Engine(config_file=self.tmp / "nowhere.yaml")

    def test_an_agent_reads_the_same_layer(self):
        path = self.write("agentic.yaml", "name: scribe\nmemory_tools: false")
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=path)
        self.addCleanup(agent.close)
        self.assertEqual(agent.config.name, "scribe")
        self.assertFalse(agent.memory.tools)


class EnvFileTest(unittest.TestCase):
    """The ``.env`` layer: loaded into the environment before the config file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = unittest.mock.patch.dict("os.environ")
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in [k for k in os.environ if k.startswith(A.ENV_PREFIX)]:
            del os.environ[key]

    def write(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
        return path

    def test_the_cwd_wins_over_the_module_directory(self):
        near, far = self.tmp / "near", self.tmp / "far"
        near.mkdir()
        far.mkdir()
        self.assertEqual(A.env_candidates([near, far]), [near / ".env", far / ".env"])
        self.assertIsNone(A.find_env_file([near, far]))
        (far / ".env").write_text("A=far\n", encoding="utf-8")
        self.assertEqual(A.find_env_file([near, far]), far / ".env")
        (near / ".env").write_text("A=near\n", encoding="utf-8")
        self.assertEqual(A.find_env_file([near, far]), near / ".env")

    def test_reading_leaves_the_environment_alone(self):
        path = self.write(
            ".env",
            """
            # comment
            DOTENV_PLAIN=one
            DOTENV_QUOTED="two words"
            export DOTENV_EXPORTED=three
            DOTENV_BARE
            """,
        )
        self.assertEqual(
            A.read_env_file(path),
            {
                "DOTENV_PLAIN": "one",
                "DOTENV_QUOTED": "two words",
                "DOTENV_EXPORTED": "three",
            },
        )
        self.assertNotIn("DOTENV_PLAIN", os.environ)

    def test_loading_keeps_existing_variables_unless_overridden(self):
        path = self.write(".env", "KEEP=file\nNEW=file")
        environ = {"KEEP": "shell"}
        self.assertEqual(A.load_env_file(path, environ), {"NEW": "file"})
        self.assertEqual(environ, {"KEEP": "shell", "NEW": "file"})
        A.load_env_file(path, environ, override=True)
        self.assertEqual(environ["KEEP"], "file")

    def test_the_engine_loads_it_before_the_config_file(self):
        env = self.write(".env", "AGENTIC_PORT=9200\nDOTENV_MODEL=from-dotenv")
        config = self.write("agentic.yaml", "inference_llm: ${oc.env:DOTENV_MODEL}")
        engine = A.Engine(env_file=env, config_file=config)
        self.assertEqual(engine.env_file, env)
        self.assertEqual(engine.config.inference_llm, "from-dotenv")
        self.assertEqual(engine.config.port, 9200)

    def test_the_file_wins_over_the_real_environment(self):
        env = self.write(".env", "AGENTIC_INFERENCE_LLM=from-dotenv")
        os.environ["AGENTIC_INFERENCE_LLM"] = "from-shell"
        self.addCleanup(os.environ.pop, "AGENTIC_INFERENCE_LLM", None)
        engine = A.Engine(env_file=env, config_file=False)
        self.assertEqual(engine.config.inference_llm, "from-dotenv")

    def test_off_is_a_value_and_env_false_skips_it_too(self):
        env = self.write(".env", "AGENTIC_INFERENCE_LLM=from-dotenv")
        self.assertIsNone(A.Engine(env_file=False, config_file=False).env_file)
        engine = A.Engine(env_file=env, env=False, config_file=False)
        self.assertIsNone(engine.env_file)
        self.assertNotIn("AGENTIC_INFERENCE_LLM", os.environ)
        self.assertEqual(engine.config.inference_llm, A.DEFAULT_INFERENCE_LLM)

    def test_a_named_file_that_does_not_exist_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            A.Engine(env_file=self.tmp / "nowhere.env", config_file=False)

    def test_an_agent_reads_the_same_layer(self):
        env = self.write(".env", "AGENTIC_NAME=scribe")
        agent = A.Agent(
            console=False, store=A.MemoryStore(), env_file=env, config_file=False
        )
        self.addCleanup(agent.close)
        self.assertEqual(agent.env_file, env)
        self.assertEqual(agent.config.name, "scribe")


class ReplTest(unittest.TestCase):
    """The terminal client, driven by an injected reader."""

    class ReplAgent(A.Agent):
        """An agent whose runs are recorded instead of performed."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.asked: list[str] = []

        async def run(self, template, *, config=None, session=None, context=None, **overrides):
            self.asked.append(str(overrides.get("input", "")))
            result = A.RunResult(session_id=getattr(session, "id", ""))
            result.output = f"echo: {overrides.get('input', '')}"
            return result

    def setUp(self):
        self.out = io.StringIO()
        self.agent = self.ReplAgent(
            console=False,
            store=A.MemoryStore(),
            config_file=False,
            renderer=A.ConsoleRenderer(color=False, stream=self.out),
        )
        self.addCleanup(self.agent.close)

    def repl(self, lines, **kwargs):
        feed = iter(lines)
        return self.agent.repl(reader=lambda prompt: next(feed), **kwargs)

    def test_a_plain_line_is_a_prompt_and_exit_leaves(self):
        repl = self.repl(["hello", "/exit", "never"])
        self.assertEqual(run(repl.start()), 0)
        self.assertEqual(self.agent.asked, ["hello"])

    def test_end_of_input_leaves(self):
        repl = self.repl(["one", None])
        run(repl.start())
        self.assertEqual(self.agent.asked, ["one"])

    def test_blank_lines_are_ignored(self):
        repl = self.repl(["", "   ", None])
        run(repl.start())
        self.assertEqual(self.agent.asked, [])

    def test_an_opening_prompt_is_answered_first(self):
        repl = self.repl([None])
        run(repl.start(opening="from the command line"))
        self.assertEqual(self.agent.asked, ["from the command line"])

    def test_reaches_outside_the_workspace_by_default(self):
        self.repl([None])
        self.assertIs(self.agent.config.outside_read, True)
        self.assertIs(self.agent.config.outside_write, True)

    def test_an_explicit_outside_flag_wins_over_the_repl_default(self):
        self.agent.config = self.agent.config.merge(outside_write=False)
        self.repl([None])
        self.assertIs(self.agent.config.outside_read, True)
        self.assertIs(self.agent.config.outside_write, False)

    def test_a_slash_line_is_a_command(self):
        repl = self.repl(["/model tiny", None])
        run(repl.start())
        self.assertEqual(self.agent.asked, [])
        self.assertEqual(self.agent.config.inference_llm, "tiny")
        self.assertIn("Model: tiny", self.out.getvalue())

    def test_help_lists_the_repl_commands_too(self):
        repl = self.repl(["/help", None])
        run(repl.start())
        self.assertIn("/exit", self.out.getvalue())
        self.assertIn("/publish", self.out.getvalue())

    def test_an_unknown_command_is_reported(self):
        repl = self.repl(["/nope", None])
        run(repl.start())
        self.assertIn("Unknown command: /nope", self.out.getvalue())

    def test_new_and_end_move_the_repl_between_sessions(self):
        repl = self.repl(["/new", "/end", None])
        first = self.agent.sessions.ensure(None)
        repl.session = first
        run(repl.start())
        self.assertIsNone(repl.session)
        self.assertNotEqual(repl.current().id, first.id)

    def test_the_banner_is_printed_once_not_per_run(self):
        repl = self.repl(["hello", None])
        run(repl.start())
        self.assertEqual(self.out.getvalue().count("(repl)"), 1)
        self.assertFalse(self.agent.renderer.banners)

    def test_the_banner_names_the_config_file_and_session(self):
        repl = self.repl([None])
        repl.session = self.agent.sessions.ensure(None)
        self.agent.config_file = Path("/somewhere/agentic.yaml")
        labels = [label for label, _ in repl.banner_items()]
        self.assertIn("Config", labels)
        self.assertIn("Session", labels)
        self.assertNotIn("Input", labels)

    def test_a_failing_run_keeps_the_repl_open(self):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("model is down")

        self.agent.run = boom
        repl = self.repl(["one", "two", None])
        run(repl.start())
        self.assertEqual(self.out.getvalue().count("model is down"), 2)

    def test_an_interrupt_cancels_the_run_and_stays_open(self):
        async def forever(*_args, **_kwargs):
            await asyncio.sleep(30)

        self.agent.run = forever

        async def drive():
            repl = self.repl(["slow", None])

            async def interrupt():
                while repl.task is None:
                    await asyncio.sleep(0.01)
                repl.interrupt()

            asyncio.get_running_loop().create_task(interrupt())
            return await repl.start()

        self.assertEqual(run(drive()), 0)
        self.assertIn("(cancelled)", self.out.getvalue())

    def test_cancelling_the_repl_itself_stops_it(self):
        async def forever(*_args, **_kwargs):
            await asyncio.sleep(30)

        self.agent.run = forever

        async def drive():
            repl = self.repl(["slow", None])
            task = asyncio.create_task(repl.start())
            while repl.task is None:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(repl.running)

        run(drive())

    def test_an_idle_interrupt_only_prints_a_hint(self):
        repl = self.repl([None])
        repl.interrupt()
        self.assertIn("Ctrl-D to leave", self.out.getvalue())

    def test_the_agent_builds_its_own_repl(self):
        self.assertIsInstance(self.agent.repl(), A.Repl)

    def test_the_repl_lets_the_model_ask(self):
        self.repl([None])
        self.assertIs(self.agent.config.ask_enabled, True)

    def asking(self, answers, *, ask_on=("go",)):
        """Replace the run with one that asks a question for each input in `ask_on`."""
        agent = self.agent

        async def ask(template, *, session=None, **overrides):
            text = str(overrides.get("input", ""))
            agent.asked.append(text)
            result = A.RunResult(session_id=session.id)
            if text in ask_on:
                asker = agent.build_asker(agent.config, result)
                answers.append(await asker(None, "Which one?", ["red", "blue"]))
            return result

        agent.run = ask

    def test_a_question_is_answered_by_number_or_in_words(self):
        answers: list[str] = []
        self.asking(answers, ask_on=("go", "again"))
        repl = self.repl(["go", "2", "again", "green", None])
        run(repl.start())
        self.assertEqual(answers, ["blue", "green"])
        output = self.out.getvalue()
        self.assertEqual(output.count("Which one?"), 2)
        self.assertIn("1. red", output)
        self.assertIn("2. blue", output)

    def test_an_interrupted_question_hands_the_terminal_back(self):
        answers: list[str] = []
        self.asking(answers)

        async def drive():
            loop = asyncio.get_running_loop()
            feed = iter(["go", None])

            def reader(prompt):
                if prompt == A.Repl.ANSWER:
                    loop.call_soon_threadsafe(repl.interrupt)
                    time.sleep(0.2)  # the line arrives after the run is gone
                    return "after"
                return next(feed)

            repl = self.agent.repl(reader=reader)
            await repl.start()
            return repl

        repl = run(drive())
        self.assertIn("(cancelled)", self.out.getvalue())
        self.assertEqual(answers, [])
        self.assertEqual(self.agent.asked, ["go", "after"])
        self.assertEqual(repl.replies, {})
        self.assertEqual(self.agent.questions, {})


class CliTest(unittest.TestCase):
    def test_the_accepted_arguments(self):
        args = A.parse_args(["--input", "notes.md", "--serve"])
        self.assertEqual(args.input, "notes.md")
        self.assertTrue(args.serve)
        self.assertEqual(
            vars(A.parse_args([])),
            {"input": "", "repl": False, "serve": False, "config": ""},
        )
        self.assertTrue(A.parse_args(["--repl"]).repl)
        self.assertEqual(A.parse_args(["--config", "c.yaml"]).config, "c.yaml")
        with self.assertRaises(SystemExit):
            A.parse_args(["--unknown"])

    def test_no_arguments_open_the_repl(self):
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=False)
        opened: list[str] = []

        class FakeRepl:
            async def start(self, opening=""):
                opened.append(opening)
                return 0

        agent.repl = lambda **_: FakeRepl()
        self.assertEqual(run(agent.execute("T", A.parse_args([]))), 0)
        self.assertEqual(opened, [""])

    def test_an_input_argument_performs_one_run(self):
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=False)
        ran: list[str] = []

        async def fake_run(template, **overrides):
            ran.append(overrides.get("input", ""))
            return A.RunResult(session_id="s")

        agent.run = fake_run
        self.assertEqual(run(agent.execute("T", A.parse_args(["--input", "hi"]))), 0)
        self.assertEqual(ran, ["hi"])

    def test_template_resolution_prefers_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.md"
            path.write_text("from file", encoding="utf-8")
            os.environ["AGENTIC_TEMPLATE"] = str(path)
            self.addCleanup(os.environ.pop, "AGENTIC_TEMPLATE", None)
            self.assertEqual(A.load_template(), "from file")
            os.environ["AGENTIC_TEMPLATE"] = "raw {{ config.input }}"
            self.assertEqual(A.load_template(), "raw {{ config.input }}")

    def test_template_resolution_prefers_the_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.md"
            path.write_text("from config", encoding="utf-8")
            config = A.AgentConfig(template=str(path))
            self.assertEqual(A.load_template(config), "from config")
            self.assertEqual(
                A.load_template(A.AgentConfig(template="raw one")), "raw one"
            )

    def test_template_candidates_follow_the_config_directories(self):
        names = {p.name for p in A.template_candidates()}
        self.assertEqual(names, {"agentic_prompt.md"})
        self.assertIn(A.ROOT / "agentic_prompt.md", A.template_candidates())

    def test_the_passthrough_template_is_the_last_resort(self):
        with unittest.mock.patch.object(A, "template_candidates", return_value=[]):
            self.assertEqual(A.load_template(A.AgentConfig()), A.PASSTHROUGH_TEMPLATE)


# ---------------------------------------------------------------------------
# Modes, boundaries, the journal and learned rules
# ---------------------------------------------------------------------------


def quiet_agent(cls=A.Agent, **kwargs):
    """An agent with no console, no config file, no environment and no disk."""
    kwargs.setdefault("store", A.MemoryStore())
    return cls(console=False, config_file=False, env=False, **kwargs)


class ModeTest(unittest.TestCase):
    def agent(self, **kwargs):
        agent = quiet_agent(**kwargs)
        self.addCleanup(agent.close)
        return agent

    def test_an_interactive_mode_keeps_its_state_under_the_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(mode="repl", state_dir=tmp)
            state = Path(tmp).resolve()
            self.assertEqual(agent.config.db_path, state / "agentic.db")
            self.assertEqual(agent.config.workspace_root, state / "workspaces")
            self.assertTrue(agent.sessions.durable)
            self.assertEqual(agent.sessions.ttl, 86400)
            self.assertIs(agent.config.outside_read, True)
            self.assertIs(agent.config.outside_write, True)

    def test_the_web_mode_confines_the_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(mode="web", state_dir=tmp)
            self.assertTrue(agent.sessions.durable)
            self.assertIs(agent.config.outside_read, False)
            self.assertIs(agent.config.outside_write, False)

    def test_no_mode_keeps_nothing(self):
        agent = self.agent()
        self.assertIsNone(agent.config.state_dir)
        self.assertIsNone(agent.config.db_path)
        self.assertFalse(agent.sessions.durable)
        self.assertEqual(agent.sessions.ttl, A.DEFAULT_SESSION_TTL)

    def test_an_explicit_setting_wins_over_the_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(mode="repl", state_dir=tmp, session_durable=False, outside_write=False)
            self.assertFalse(agent.sessions.durable)
            self.assertIs(agent.config.outside_write, False)
            self.assertIs(agent.config.outside_read, True)

    def test_the_arguments_select_the_mode(self):
        self.assertEqual(A.mode_of(A.parse_args(["--serve"])), "web")
        self.assertEqual(A.mode_of(A.parse_args([])), "repl")
        self.assertEqual(A.mode_of(A.parse_args(["--repl", "--input", "x"])), "repl")
        self.assertIsNone(A.mode_of(A.parse_args(["--input", "x"])))


class SandboxTest(unittest.TestCase):
    class EchoSandbox(A.Sandbox):
        """Marks the command instead of confining it, which proves the wiring."""

        name = "echo"

        def argv(self, command, ws):
            return ["/bin/sh", "-c", f"echo sandboxed; {command}"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "root"
        self.root.mkdir()
        self.secret = self.base / "secret.txt"
        self.secret.write_text("classified", encoding="utf-8")

    @staticmethod
    def tools(ws):
        with contextlib.redirect_stderr(io.StringIO()):
            return {tool.name: tool for tool in A.ToolRegistry().build(ws)}

    def test_a_confined_workspace_without_a_sandbox_gets_no_shell(self):
        ws = A.Workspace(self.root)
        self.assertFalse(ws.shell_allowed)
        self.assertNotIn("run_shell_command", self.tools(ws))
        self.assertIn("read_text_file", self.tools(ws))

    def test_an_unconfined_workspace_gets_a_plain_shell(self):
        ws = A.Workspace(self.root, outside_read=True, outside_write=True)
        self.assertNotIn("sandboxed", self.tools(ws)["run_shell_command"].description)

    def test_a_sandboxed_shell_tells_the_model_its_boundary(self):
        ws = A.Workspace(self.root, outside_read=True, sandbox=self.EchoSandbox())
        description = self.tools(ws)["run_shell_command"].description
        self.assertIn("sandboxed", description)
        self.assertIn("read anywhere and write inside the workspace only", description)

    @unittest.skipIf(os.name == "nt", "a POSIX shell")
    def test_a_confined_shell_runs_through_the_sandbox(self):
        ws = A.Workspace(self.root, sandbox=self.EchoSandbox())
        lines = run(ws.run_shell("echo $TMPDIR; echo $HOME")).splitlines()
        self.assertEqual(lines[0], "sandboxed")
        self.assertEqual(lines[1], str(self.root / ".sandbox" / "tmp"))
        self.assertEqual(lines[2], str(self.root / ".sandbox" / "home"))

    def test_an_unconfined_shell_bypasses_the_sandbox(self):
        ws = A.Workspace(
            self.root, outside_read=True, outside_write=True, sandbox=self.EchoSandbox()
        )
        self.assertNotIn("sandboxed", run(ws.run_shell("echo plain")))

    def test_home_stays_when_reads_may_leave(self):
        ws = A.Workspace(self.root, outside_read=True)
        env = self.EchoSandbox().environ({"HOME": "/home/me"}, ws)
        self.assertEqual(env["HOME"], "/home/me")
        self.assertEqual(env["XDG_CACHE_HOME"], str(self.root / ".sandbox" / "cache"))

    def test_unknown_and_none_pick_nothing(self):
        self.assertIsNone(A.pick_sandbox("none"))
        self.assertIsNone(A.pick_sandbox("bogus"))

    def test_the_engine_confines_only_what_the_config_confines(self):
        engine = A.Engine(env=False, config_file=False)
        self.addCleanup(engine.close)
        ws = engine.build_workspace(self.root, A.AgentConfig(shell_sandbox="none"))
        self.assertIsNone(ws.sandbox)
        self.assertFalse(ws.shell_allowed)
        ws = engine.build_workspace(
            self.root, A.AgentConfig(outside_read=True, outside_write=True, shell_sandbox="none")
        )
        self.assertTrue(ws.shell_allowed)

    def test_the_seatbelt_profile_denies_what_the_flags_do_not_allow(self):
        sandbox = A.SeatbeltSandbox()
        confined = sandbox.profile(A.Workspace(self.root))
        self.assertIn("(allow default)", confined)
        self.assertIn(
            f'(deny file-write* (require-not (require-any (subpath "{self.root}")', confined
        )
        self.assertIn("(deny file-read-data", confined)
        reads = sandbox.profile(A.Workspace(self.root, outside_read=True))
        self.assertNotIn("file-read-data", reads)
        self.assertIn("file-write*", reads)
        argv = sandbox.argv("ls", A.Workspace(self.root))
        self.assertEqual(argv[:2], [A.SeatbeltSandbox.EXE, "-p"])
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "ls"])

    def test_the_seatbelt_profile_quotes_paths(self):
        self.assertEqual(A.SeatbeltSandbox.quote('a"b\\c'), '"a\\"b\\\\c"')

    @unittest.skipIf(os.name == "nt", "POSIX paths")
    def test_bubblewrap_binds_the_workspace_and_reads_the_system(self):
        confined = " ".join(A.BubblewrapSandbox().argv("ls", A.Workspace(self.root)))
        self.assertIn(f"--bind-try {self.root} {self.root}", confined)
        self.assertIn("--ro-bind-try /usr /usr", confined)
        self.assertNotIn("--ro-bind-try / /", confined)
        reads = " ".join(
            A.BubblewrapSandbox().argv("ls", A.Workspace(self.root, outside_read=True))
        )
        self.assertIn("--ro-bind-try / /", reads)
        self.assertTrue(reads.endswith("/bin/sh -c ls"))

    @unittest.skipUnless(A.LandlockSandbox.available(), "needs Landlock")
    def test_landlock_holds_the_shell_to_the_workspace(self):
        ws = A.Workspace(self.root, sandbox=A.LandlockSandbox())
        out = run(
            ws.run_shell(
                f"echo in > inside.txt; cat inside.txt; cat '{self.secret}'; "
                f"echo x > '{self.base / 'out.txt'}'"
            )
        )
        self.assertIn("in", out.splitlines())
        self.assertNotIn("classified", out)
        self.assertIn("Permission denied", out)
        self.assertFalse((self.base / "out.txt").exists())

    @unittest.skipUnless(A.LandlockSandbox.available(), "needs Landlock")
    def test_landlock_follows_the_outside_flags(self):
        reader = A.Workspace(self.root, outside_read=True, sandbox=A.LandlockSandbox())
        out = run(reader.run_shell(f"cat '{self.secret}'; echo x > '{self.base / 'out.txt'}'"))
        self.assertIn("classified", out)
        self.assertFalse((self.base / "out.txt").exists())
        writer = A.Workspace(self.root, outside_write=True, sandbox=A.LandlockSandbox())
        out = run(writer.run_shell(f"cat '{self.secret}'; echo x > '{self.base / 'out.txt'}'"))
        self.assertNotIn("classified", out)
        self.assertTrue((self.base / "out.txt").exists())


class StreamJournalTest(unittest.TestCase):
    """The stream hands each finished step to `checkpoint` as raw input items."""

    drive = StreamTest.drive

    @staticmethod
    def raw(item, payload):
        item.to_input_item = lambda: payload
        return item

    def test_messages_and_whole_tool_pairs_become_turns(self):
        S = StreamTest
        message = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Let me look"}]}
        call = self.raw(
            types.SimpleNamespace(
                tool_name="read_text_file",
                raw_item=types.SimpleNamespace(call_id="c1", arguments='{"path": "a"}'),
            ),
            {"type": "function_call", "call_id": "c1", "name": "read_text_file", "arguments": '{"path": "a"}'},
        )
        output = self.raw(
            types.SimpleNamespace(output="A", call_id="c1"),
            {"type": "function_call_output", "call_id": "c1", "output": "A"},
        )
        orphan = self.raw(
            types.SimpleNamespace(
                tool_name="run_shell_command",
                raw_item=types.SimpleNamespace(call_id="c2", arguments="{}"),
            ),
            {"type": "function_call", "call_id": "c2", "name": "run_shell_command", "arguments": "{}"},
        )
        streamed = S.FakeStream(
            events=[
                S.delta("Let me look"),
                S.item("message_output_created", self.raw(types.SimpleNamespace(), message)),
                S.item("tool_called", call),
                S.item("tool_output", output),
                S.item("tool_called", orphan),
            ]
        )
        _engine, result, stream = self.drive(streamed)
        stream()
        self.assertEqual([t.role for t in result.turns], ["assistant", "tool"])
        said, tool = result.turns
        self.assertEqual(said.id, result.blocks[0].id)
        self.assertEqual(said.text, "Let me look")
        self.assertEqual(said.items, [message])
        self.assertEqual(tool.id, result.tools[0].id)
        self.assertEqual([i["type"] for i in tool.items], ["function_call", "function_call_output"])
        self.assertTrue(result.blocks[0].done)


class JournalTest(unittest.TestCase):
    """A run journals each step as it finishes, so a crash keeps them."""

    class StepAgent(A.Agent):
        """Checkpoints a tool call and a message, then answers; with a gate it
        parks after the tool call, the way a process that died there would."""

        gate = None
        reached = None
        fail = None

        def build_sdk_agent(self, config, tools):
            return {}

        async def stream(self, sdk_agent, model_input, config, result):
            self.inputs = [*getattr(self, "inputs", []), model_input]
            call_id = f"c{len(self.inputs)}"
            prefix = f"{result.session_id}-{result.run_id}"
            await self.checkpoint(
                result,
                A.Turn(
                    "tool",
                    'read_text_file(path="a") -> ok',
                    id=f"{prefix}-0",
                    run=result.run_id,
                    items=[
                        {"type": "function_call", "call_id": call_id, "name": "read_text_file", "arguments": '{"path": "a"}'},
                        {"type": "function_call_output", "call_id": call_id, "output": "A" * 20000},
                    ],
                ),
            )
            if self.reached is not None:
                self.reached.set()
            if self.gate is not None:
                await self.gate.wait()
            if self.fail is not None:
                raise self.fail
            text = f"done: {A.prompt_of(model_input)[:20]}"
            message = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
            await self.checkpoint(
                result, A.Turn("assistant", text, id=f"{prefix}-1", run=result.run_id, items=[message])
            )
            result.output = text
            return result

    def agent(self, **kwargs):
        agent = quiet_agent(self.StepAgent, **kwargs)
        self.addCleanup(agent.close)
        return agent

    def test_steps_are_journaled_and_replayed_as_the_context_window(self):
        agent = self.agent()
        session = agent.sessions.create()
        run(agent.run("{{ config.input }}", session=session, input="first"))
        run(agent.run("{{ config.input }}", session=session, input="second"))
        replayed = agent.inputs[1]
        self.assertEqual(
            [i.get("type") or i.get("role") for i in replayed],
            ["user", "function_call", "function_call_output", "message", "user"],
        )
        self.assertEqual(replayed[0]["content"], "first")
        self.assertLessEqual(len(replayed[2]["output"]), A.ConversationMemory.TOOL_LIMIT + 40)
        transcript = agent.memory.transcript(session.id)
        answers = [t for t in transcript.turns if t.role == "assistant"]
        self.assertEqual(len(answers), 2)  # the final answer is not journaled twice
        self.assertEqual(transcript.last_run().meta["status"], A.RUN_OK)
        self.assertIsNone(agent.unfinished(session.id))

    def test_a_crash_leaves_the_steps_for_the_next_process(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)

        def build():
            return quiet_agent(
                self.StepAgent,
                store=A.SqliteStore(root / "a.db"),
                workspace_root=root / "ws",
                session_durable=True,
            )

        first = build()
        session = first.sessions.create()

        async def crash():
            first.gate, first.reached = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(first.run("{{ config.input }}", session=session, input="build it"))
            await first.reached.wait()
            second = build()  # the process that starts after the crash
            seen = (
                second.sessions.get(session.id),
                second.memory.history(session.id),
                second.unfinished(session.id),
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return second, seen

        second, (restored, history, unfinished) = run(crash())
        self.addCleanup(second.close)
        self.addCleanup(first.store.close)
        self.assertIsNotNone(restored)
        self.assertEqual(
            [i.get("type") or i.get("role") for i in history],
            ["user", "function_call", "function_call_output"],
        )
        self.assertEqual(unfinished.text, "build it")
        self.assertEqual(unfinished.meta["status"], A.RUN_RUNNING)

        result = run(second.commands.invoke("/resume", session_id=session.id))
        self.assertTrue(result["ok"])
        self.assertTrue(result["resume"])
        run(second.resume(session.id))
        resumed = second.inputs[-1]
        self.assertEqual(resumed[-1], {"role": "user", "content": A.Agent.RESUME_PROMPT})
        self.assertIn("function_call", [i.get("type") for i in resumed])
        self.assertIsNone(second.unfinished(session.id))

    def test_a_failed_run_keeps_its_steps_and_says_so(self):
        agent = self.agent()
        agent.fail = RuntimeError("provider fell over")
        session = agent.sessions.create()
        result = run(agent.run("{{ config.input }}", session=session, input="try"))
        self.assertFalse(result.ok)
        unfinished = agent.unfinished(session.id)
        self.assertEqual(unfinished.meta["status"], A.RUN_FAILED)
        self.assertIn("provider fell over", unfinished.meta["error"])
        roles = [t.role for t in agent.memory.transcript(session.id).turns]
        self.assertEqual(roles, ["user", "tool"])
        self.assertEqual(run(agent.commands.invoke("/resume", session_id=session.id))["ok"], True)

    def test_a_cancelled_run_is_marked_cancelled(self):
        agent = self.agent()
        session = agent.sessions.create()

        async def cancel():
            agent.gate, agent.reached = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(agent.run("{{ config.input }}", session=session, input="x"))
            await agent.reached.wait()
            self.assertIn(session.id, agent.live)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        run(cancel())
        self.assertNotIn(session.id, agent.live)
        self.assertEqual(agent.unfinished(session.id).meta["status"], A.RUN_CANCELLED)

    def test_tool_calls_can_be_left_out_of_the_context(self):
        agent = self.agent(memory_tools=False)
        session = agent.sessions.create()
        run(agent.run("{{ config.input }}", session=session, input="first"))
        history = agent.memory.history(session.id)
        self.assertNotIn("function_call", [i.get("type") for i in history])
        self.assertIn("tool", [t["kind"] for t in agent.memory.replay(session.id)])

    def test_a_journal_dies_with_its_session(self):
        agent = self.agent()
        session = agent.sessions.create()
        run(agent.run("{{ config.input }}", session=session, input="x"))
        self.assertIsNotNone(agent.store.get(A.ConversationMemory.NAMESPACE, session.id))
        session.expires_at = 0
        agent.sessions.purge_expired()
        self.assertIsNone(agent.store.get(A.ConversationMemory.NAMESPACE, session.id))

    def test_a_restart_forgets_journals_without_a_session(self):
        store = A.MemoryStore()
        store.set(A.ConversationMemory.NAMESPACE, "gone", {"turns": [{"role": "user", "text": "hi"}]})
        self.agent(store=store)
        self.assertIsNone(store.get(A.ConversationMemory.NAMESPACE, "gone"))

    def test_closing_a_session_cancels_its_run(self):
        agent = self.agent()
        session = agent.sessions.create()

        async def close():
            agent.gate, agent.reached = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(agent.run("{{ config.input }}", session=session, input="x"))
            await agent.reached.wait()
            agent.sessions.close(session.id)
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return task

        self.assertTrue(run(close()).cancelled())

    def test_a_working_run_renews_its_session(self):
        agent = self.agent()
        session = agent.sessions.create()
        session.expires_at = time.time() + 1
        run(agent.run("{{ config.input }}", session=session, input="x"))
        self.assertGreater(session.expires_at, time.time() + 60)

    def test_the_journal_replays_as_chat_messages_the_sdk_accepts(self):
        """Raw items keep an assistant message and its tool call one message."""
        from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem
        from agents.models.chatcmpl_converter import Converter
        from openai.types.responses import (
            ResponseFunctionToolCall,
            ResponseOutputMessage,
            ResponseOutputText,
        )

        sdk = A.Agent  # any agent object satisfies the items' back reference
        said = MessageOutputItem(
            agent=sdk,
            raw_item=ResponseOutputMessage(
                id="m1",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text="Let me look", annotations=[])],
            ),
        )
        call = ToolCallItem(
            agent=sdk,
            raw_item=ResponseFunctionToolCall(
                type="function_call", call_id="c1", name="read_text_file", arguments='{"path": "a"}'
            ),
        )
        output = ToolCallOutputItem(
            agent=sdk,
            raw_item={"type": "function_call_output", "call_id": "c1", "output": "A"},
            output="A",
        )
        agent = self.agent()
        session = agent.sessions.create()
        agent.memory.journal(
            session.id,
            A.Turn("user", "look", run="r1"),
            A.Turn("assistant", "Let me look", run="r1", items=[A.Engine.input_item(said)]),
            A.Turn("tool", "t", run="r1", items=[A.Engine.input_item(call), A.Engine.input_item(output)]),
        )
        stored = A.ConversationMemory(agent.store).history(session.id)  # from the store, as JSON
        messages = Converter.items_to_messages([*stored, {"role": "user", "content": "next"}])
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "user"])
        self.assertEqual(messages[1]["content"], "Let me look")
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "c1")
        self.assertEqual(messages[2]["tool_call_id"], "c1")

    def test_turns_round_trip(self):
        turn = A.Turn(
            "tool", "t", id="b1", run="r1", items=[{"type": "function_call"}], meta={"ok": True}
        )
        again = A.Turn.from_dict(json.loads(json.dumps(turn.to_dict())))
        self.assertEqual(again, turn)
        self.assertEqual(again.kind, "tool")
        self.assertNotIn("items", A.Turn("user", "plain").to_dict())


class RuleBookTest(unittest.TestCase):
    def setUp(self):
        self.store = A.MemoryStore()
        self.book = A.RuleBook(self.store)

    def test_a_json_reply_is_read(self):
        decision = self.book.parse(
            'Sure: {"action": "add", "polarity": "avoid", "rule": "No emojis."}', "up"
        )
        self.assertEqual(
            decision, {"action": "add", "id": "", "polarity": "avoid", "rule": "No emojis."}
        )

    def test_an_edit_of_an_unknown_rule_is_an_add(self):
        decision = self.book.parse('{"action": "edit", "id": "r404", "rule": "X."}', "down")
        self.assertEqual((decision["action"], decision["polarity"]), ("add", "avoid"))

    def test_a_plain_reply_is_the_rule_itself(self):
        decision = self.book.parse("Keep answers short.\nBecause...", "down")
        self.assertEqual((decision["rule"], decision["polarity"]), ("Keep answers short.", "avoid"))

    def test_an_empty_reply_teaches_nothing(self):
        with self.assertRaises(ValueError):
            self.book.parse("   ", "up")
        with self.assertRaises(ValueError):
            self.book.parse('{"action": "add"}', "up")
        with self.assertRaises(ValueError):
            self.book.parse("{}", "up")

    def test_rules_persist_through_the_store(self):
        self.book.add("do", "Cite files.")
        self.assertEqual([r.line() for r in A.RuleBook(self.store).rules()], ["Do: Cite files."])

    def test_decorate_appends_the_rules(self):
        self.assertEqual(self.book.decorate("Base."), "Base.")
        self.book.add("do", "Cite files.")
        self.assertEqual(
            self.book.decorate("Base."),
            "Base.\n\n## Learned rules\n\nThe user taught you these through feedback:"
            "\n\n- Do: Cite files.",
        )
        self.book.enabled = False
        self.assertEqual(self.book.decorate("Base."), "Base.")

    def test_the_warning_follows_the_budget(self):
        self.assertIsNone(self.book.warning())
        self.book.max_chars = 10
        self.book.add("do", "x" * 20)
        self.assertIn("over the 10 budget", self.book.warning())
        self.book.max_chars = 0
        self.assertIsNone(self.book.warning())

    def test_a_long_trace_keeps_its_head_and_tail(self):
        text = self.book.trace([A.Turn("user", "ask " + "a" * 20000), A.Turn("assistant", "end")])
        self.assertLess(len(text), A.RuleBook.TRACE_LIMIT + 40)
        self.assertTrue(text.startswith("USER: ask"))
        self.assertTrue(text.endswith("ASSISTANT: end"))

    def test_learning_is_serialized(self):
        async def drive():
            seen = []

            async def distill(messages):
                seen.append(messages[1]["content"])
                await asyncio.sleep(0.01)
                return '{"action": "add", "polarity": "do", "rule": "R%d"}' % len(seen)

            await asyncio.gather(
                self.book.learn("up", [A.Turn("assistant", "a")], distill),
                self.book.learn("up", [A.Turn("assistant", "b")], distill),
            )
            return seen

        with contextlib.redirect_stderr(io.StringIO()):
            seen = run(drive())
        self.assertIn("R1", seen[1])  # the second saw what the first learned


class FeedbackTest(unittest.TestCase):
    class LearningAgent(A.Agent):
        """Answers in capitals; its distiller replies from a script."""

        replies: list[str] = []

        def build_sdk_agent(self, config, tools):
            self.instructions = config.instructions
            return {}

        async def stream(self, sdk_agent, model_input, config, result):
            block = A.Block(
                id=f"{result.session_id}-{result.run_id}-0", kind="output", session_id=result.session_id
            )
            block.text = A.prompt_of(model_input).upper()
            result.blocks.append(block)
            result.output = block.text
            return result

        def build_distiller(self, config):
            async def distill(messages):
                self.requests = [*getattr(self, "requests", []), messages]
                return self.replies.pop(0)

            return distill

    def setUp(self):
        self.agent = quiet_agent(self.LearningAgent)
        self.addCleanup(self.agent.close)
        self.session = self.agent.sessions.create()
        run(self.agent.run("{{ config.input }}", session=self.session, input="hello"))
        self.stderr = contextlib.redirect_stderr(io.StringIO())
        self.stderr.__enter__()
        self.addCleanup(self.stderr.__exit__, None, None, None)

    def answers(self):
        return [t for t in self.agent.memory.transcript(self.session.id).turns if t.role == "assistant"]

    def test_good_adds_a_do_rule_from_the_trace(self):
        self.agent.replies = ['{"action": "add", "polarity": "do", "rule": "Answer in capitals."}']
        events = []
        self.agent.events.on(A.EventType.FEEDBACK, events.append)
        result = run(self.agent.commands.invoke("/good loved it", session_id=self.session.id))
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "add")
        self.assertIn("Do: Answer in capitals.", result["message"])
        request = self.agent.requests[0][1]["content"]
        for expected in ("USER: hello", "ASSISTANT: HELLO", "LIKED", "loved it"):
            self.assertIn(expected, request)
        self.assertEqual(events[0].data["id"], self.answers()[0].id)
        self.assertEqual(self.answers()[0].meta["vote"], "up")

    def test_an_answer_is_rated_once(self):
        self.agent.replies = ['{"action": "add", "rule": "R."}']
        run(self.agent.feedback(self.session.id, "up"))
        again = run(self.agent.commands.invoke("/bad", session_id=self.session.id))
        self.assertFalse(again["ok"])
        self.assertIn("Already rated up", again["message"])
        self.assertEqual(len(self.agent.requests), 1)

    def test_bad_edits_the_related_rule(self):
        rule = self.agent.rules.add("do", "Answer in capitals.")
        self.agent.replies = [
            json.dumps({"action": "edit", "id": rule.id, "polarity": "avoid", "rule": "Shouting."})
        ]
        result = run(self.agent.feedback(self.session.id, "down"))
        self.assertEqual(result["action"], "edit")
        self.assertEqual(len(self.agent.rules.rules()), 1)
        edited = self.agent.rules.get(rule.id)
        self.assertEqual((edited.polarity, edited.text, edited.votes), ("avoid", "Shouting.", 2))
        self.assertIn(rule.id, self.agent.requests[0][1]["content"])

    def test_a_target_picks_the_answer_it_names(self):
        first = self.answers()[0].id
        run(self.agent.run("{{ config.input }}", session=self.session, input="again"))
        self.agent.replies = ['{"rule": "R."}']
        result = run(self.agent.feedback(self.session.id, "up", target=first))
        self.assertEqual(result["id"], first)
        request = self.agent.requests[0][1]["content"]
        self.assertIn("HELLO", request)
        self.assertNotIn("AGAIN", request)

    def test_rules_reach_the_instructions_of_the_next_run(self):
        self.agent.rules.add("avoid", "Long preambles.")
        run(self.agent.run("{{ config.input }}", session=self.session, input="again"))
        self.assertIn("## Learned rules", self.agent.instructions)
        self.assertIn("- Avoid: Long preambles.", self.agent.instructions)

    def test_disabled_rules_stay_out(self):
        agent = quiet_agent(self.LearningAgent, rules_enabled=False)
        self.addCleanup(agent.close)
        agent.rules.add("do", "Hidden.")
        session = agent.sessions.create()
        run(agent.run("{{ config.input }}", session=session, input="x"))
        self.assertNotIn("Learned rules", agent.instructions)
        self.assertFalse(run(agent.feedback(session.id, "up"))["ok"])

    def test_a_failed_distillation_claims_nothing(self):
        self.agent.replies = ["   "]
        result = run(self.agent.feedback(self.session.id, "up"))
        self.assertFalse(result["ok"])
        self.assertIn("Could not learn", result["message"])
        self.assertNotIn("vote", self.answers()[0].meta)

    def test_an_unknown_vote_is_refused(self):
        result = run(self.agent.feedback(self.session.id, "sideways"))
        self.assertFalse(result["ok"])
        self.assertIn("Unknown vote", result["message"])
        self.assertNotIn("vote", self.answers()[0].meta)

    def test_nothing_to_rate_is_said(self):
        empty = self.agent.sessions.create()
        self.assertIn("No answer", run(self.agent.feedback(empty.id, "up"))["message"])

    def test_the_warning_comes_with_an_oversized_book(self):
        self.agent.rules.max_chars = 10
        self.agent.replies = ['{"rule": "A rule longer than ten characters."}']
        result = run(self.agent.feedback(self.session.id, "up"))
        self.assertIn("over the 10 budget", result["warning"])
        self.assertIn(result["warning"], result["message"])

    def test_rules_command_lists_drops_and_clears(self):
        rule = self.agent.rules.add("do", "Be brief.")
        listing = run(self.agent.commands.invoke("/rules"))
        self.assertIn(rule.id, listing["message"])
        self.assertEqual(listing["rules"][0]["text"], "Be brief.")
        self.assertTrue(run(self.agent.commands.invoke(f"/rules drop {rule.id}"))["ok"])
        self.assertEqual(self.agent.rules.rules(), [])
        self.assertFalse(run(self.agent.commands.invoke("/rules drop nope"))["ok"])
        self.agent.rules.add("do", "One.")
        self.agent.rules.add("avoid", "Two.")
        self.assertIn("Dropped 2", run(self.agent.commands.invoke("/rules clear"))["message"])


class WebReattachTest(unittest.TestCase):
    """A run belongs to its session: it outlives a socket and is picked up again."""

    class GatedAgent(FeedbackTest.LearningAgent):
        """Streams half an answer, then waits for the gate."""

        gate = None
        reached = None

        async def stream(self, sdk_agent, model_input, config, result):
            self.inputs = [*getattr(self, "inputs", []), model_input]
            block = A.Block(
                id=f"{result.session_id}-{result.run_id}-0", kind="output", session_id=result.session_id
            )
            result.blocks.append(block)
            publish = functools.partial(self.events.publish, session_id=result.session_id, id=block.id, kind="output")
            await publish(A.EventType.BLOCK_START, role="assistant")
            block.text = "half"
            await publish(A.EventType.BLOCK_DELTA, text="half")
            if self.reached is not None:
                self.reached.set()
            if self.gate is not None:
                await self.gate.wait()
            block.text += " whole"
            await publish(A.EventType.BLOCK_DELTA, text=" whole")
            block.done = True
            await publish(A.EventType.BLOCK_END, text=block.text)
            result.output = block.text
            return result

    def setUp(self):
        self.agent = quiet_agent(self.GatedAgent)
        self.addCleanup(self.agent.close)
        self.server = A.WebServer(self.agent, template="{{ config.input }}")

    def connect(self):
        connection = A.Connection(self.server.hub, HubTest.FakeSocket())
        self.server.hub.connections[connection.id] = connection
        return connection

    def drop(self, connection):
        """What `Hub.serve_connection` does when a socket closes."""
        run_soon = connection.cancel_task()
        for channel in list(connection.channels):
            self.server.hub.leave(connection, channel)
        self.server.hub.connections.pop(connection.id, None)
        return run_soon

    def test_a_run_outlives_its_socket_and_a_new_one_picks_it_up(self):
        async def drive():
            self.agent.gate, self.agent.reached = asyncio.Event(), asyncio.Event()
            first = self.connect()
            await self.server.on_hello(first, {})
            session_id = first.session_id
            await self.server.on_prompt(first, {"text": "write"})
            await self.agent.reached.wait()
            await self.drop(first)
            self.assertTrue(self.server.active(session_id))
            second = self.connect()
            await self.server.on_hello(second, {"session": session_id})
            hello = second.ws.sent[-1]
            self.agent.gate.set()
            await self.server.runs[session_id]
            return session_id, hello, second

        session_id, hello, second = run(drive())
        self.assertTrue(hello["busy"])
        self.assertEqual([t["kind"] for t in hello["history"]], ["prompt"])
        self.assertEqual(
            [(b["kind"], b["text"], b["done"]) for b in hello["live"]], [("output", "half", False)]
        )
        after = [m["type"] for m in second.ws.sent[1:]]
        self.assertIn("block.end", after)
        self.assertIn("agent.end", after)
        texts = [t.text for t in self.agent.memory.transcript(session_id).turns]
        self.assertEqual(texts, ["write", "half whole"])
        self.assertFalse(self.server.active(session_id))

    def test_hello_shows_an_open_question_again(self):
        session = self.agent.sessions.create()

        async def drive():
            asker = self.agent.build_asker(
                self.agent.config.merge(ask_enabled=True), A.RunResult(session_id=session.id)
            )
            task = asyncio.create_task(asker(None, "Which one?", ["red", "blue"]))
            while not self.agent.questions:
                await asyncio.sleep(0.005)
            connection = self.connect()
            await self.server.on_hello(connection, {"session": session.id})
            hello = connection.ws.sent[-1]
            await self.server.on_answer(connection, {"id": hello["questions"][0]["id"], "text": "red"})
            return hello, await task

        hello, answer = run(drive())
        self.assertEqual(hello["questions"][0]["question"], "Which one?")
        self.assertEqual(hello["questions"][0]["options"], ["red", "blue"])
        self.assertEqual(answer, "red")

    def test_stop_cancels_the_run_of_the_session_and_one_run_at_a_time(self):
        async def drive():
            self.agent.gate, self.agent.reached = asyncio.Event(), asyncio.Event()
            first = self.connect()
            await self.server.on_hello(first, {})
            await self.server.on_prompt(first, {"text": "write"})
            await self.agent.reached.wait()
            other = self.connect()
            await self.server.on_hello(other, {"session": first.session_id})
            await self.server.on_prompt(other, {"text": "again"})
            await self.server.on_cancel(other, {})
            return first.session_id, other

        session_id, other = run(drive())
        self.assertIn("A run is already active", [m.get("message") for m in other.ws.sent])
        self.assertEqual(other.ws.sent[-1], {"type": "cancelled", "ok": True})
        self.assertEqual(self.agent.unfinished(session_id).meta["status"], A.RUN_CANCELLED)

    def test_resume_starts_a_run_without_the_template(self):
        session = self.agent.sessions.create()
        self.agent.memory.journal(
            session.id, A.Turn("user", "build it", run="r1", meta={"status": A.RUN_RUNNING})
        )

        async def drive():
            connection = self.connect()
            await self.server.on_hello(connection, {"session": session.id})
            hello = connection.ws.sent[-1]
            await self.server.on_command(connection, {"text": "/resume"})
            await self.server.runs[session.id]
            return hello

        hello = run(drive())
        self.assertEqual(hello["unfinished"], "r1")
        self.assertEqual(self.agent.inputs[-1][-1]["content"], A.Agent.RESUME_PROMPT)

    def test_a_thumb_learns_off_the_read_loop(self):
        session = self.agent.sessions.create()
        self.agent.gate = None
        self.agent.replies = ['{"rule": "Finish what you start."}']

        async def drive():
            connection = self.connect()
            await self.server.on_hello(connection, {"session": session.id})
            await self.agent.run("{{ config.input }}", session=session, input="write")
            answer = self.agent.memory.transcript(session.id).turns[-1]
            await self.server.on_feedback(connection, {"id": answer.id, "vote": "up"})
            await asyncio.gather(*self.server.tasks)
            return connection, answer

        with contextlib.redirect_stderr(io.StringIO()):
            connection, answer = run(drive())
        feedback = [m for m in connection.ws.sent if m["type"] == "feedback"]
        self.assertTrue(feedback[0]["ok"])
        self.assertEqual(feedback[0]["id"], answer.id)
        self.assertEqual(feedback[0]["rule"]["text"], "Finish what you start.")


class ReplResumeTest(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.agent = quiet_agent(
            ReplTest.ReplAgent, renderer=A.ConsoleRenderer(color=False, stream=self.out)
        )
        self.addCleanup(self.agent.close)

    def repl(self, lines):
        feed = iter(lines)
        return self.agent.repl(reader=lambda prompt: next(feed))

    def test_the_repl_resumes_its_last_session(self):
        old = self.agent.sessions.create(client="repl")
        self.agent.memory.journal(
            old.id,
            A.Turn("user", "first question", run="r1", meta={"status": A.RUN_OK}),
            A.Turn("assistant", "first answer", run="r1"),
        )
        self.agent.sessions.create(client="web")  # newer, but not the repl's
        repl = self.repl([None])
        run(repl.start())
        self.assertEqual(repl.session.id, old.id)
        out = self.out.getvalue()
        self.assertIn(f"Resumed session {old.id}", out)
        self.assertIn("> first question", out)
        self.assertNotIn("/resume", out)

    def test_a_fresh_start_opens_a_tagged_session(self):
        repl = self.repl([None])
        run(repl.start())
        self.assertEqual(repl.session.meta["client"], "repl")
        self.assertNotIn("Resumed", self.out.getvalue())

    def test_an_unfinished_run_is_flagged_and_resumed(self):
        old = self.agent.sessions.create(client="repl")
        self.agent.memory.journal(
            old.id, A.Turn("user", "build it", run="r1", meta={"status": A.RUN_RUNNING})
        )
        repl = self.repl(["/resume", None])
        run(repl.start())
        self.assertIn("/resume to continue", self.out.getvalue())
        self.assertEqual(self.agent.asked, [A.Agent.RESUME_PROMPT])

    def test_good_and_bad_teach_from_the_terminal(self):
        old = self.agent.sessions.create(client="repl")
        self.agent.memory.journal(
            old.id,
            A.Turn("user", "q", run="r1", meta={"status": A.RUN_OK}),
            A.Turn("assistant", "a", id="b1", run="r1"),
        )

        async def distill(messages):
            return '{"action": "add", "polarity": "avoid", "rule": "Terse answers."}'

        repl = self.repl(["/bad too short", None])
        with unittest.mock.patch.object(self.agent, "build_distiller", return_value=distill):
            with contextlib.redirect_stderr(io.StringIO()):
                run(repl.start())
        self.assertIn("Avoid: Terse answers.", self.out.getvalue())
        self.assertEqual([r.text for r in self.agent.rules.rules()], ["Terse answers."])

    def test_an_oversized_book_is_flagged_at_start(self):
        self.agent.rules.max_chars = 5
        self.agent.rules.add("do", "Something long enough.")
        run(self.repl([None]).start())
        self.assertIn("over the 5 budget", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
