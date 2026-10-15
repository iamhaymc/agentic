"""Unit tests for the caveman SFT data pipeline (util/caveman_data.py).

Run with::

    uv run python -m unittest discover -s util -p "caveman_data_bdd.py" -v
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from util import caveman_data as C  # noqa: E402

THINK = (
    "The `dpkg --configure -a` command ran without any output, which means there are no pending "
    "configurations. Let me check the status of postfix and see what's happening with the package."
)
BODY = {
    "analysis": (
        "The dpkg --configure -a command ran without output, meaning there are no pending configurations. "
        "The system appears to be in a clean state now. Let me check the current status of postfix and see "
        "if there are any issues remaining."
    ),
    "plan": (
        "I will check the status of postfix package and see if it's installed properly. Then I'll check "
        "/etc/postfix/main.cf and the logs to understand the current state."
    ),
    "commands": [
        {"keystrokes": "dpkg -l | grep postfix\n", "duration": 0.5},
        {"keystrokes": "which postfix\n", "duration": 0.1},
    ],
    "task_complete": False,
}
CONTENT = "<think>" + THINK + "</think>" + json.dumps(BODY, indent=2)
GOOD = {
    "think": "`dpkg --configure -a` silent: no pending configs.",
    "analysis": "Clean state. Postfix status unknown.",
    "plan": "Check postfix status, /etc/postfix/main.cf, logs.",
}
LOST_PATH = {**GOOD, "plan": "Check postfix status and logs."}
LOST_NEGATION = {**GOOD, "think": "`dpkg --configure -a` done, configs fine."}


def words(text: str) -> int:
    return len(text.split())


def run(coro):
    return asyncio.run(coro)


def scripted(*replies):
    """Fake model: returns replies in order and records every request."""
    calls: list[list[dict]] = []

    async def ask(messages):
        calls.append(messages)
        reply = replies[min(len(calls), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    ask.calls = calls
    return ask


class Opener:
    """Fake urlopen: maps a URL substring to a JSON payload (or an exception)."""

    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def __call__(self, url):
        self.urls.append(url)
        for key, value in self.routes.items():
            if key in url:
                result = value.pop(0) if isinstance(value, list) else value
                if isinstance(result, Exception):
                    raise result
                return io.BytesIO(json.dumps(result).encode())
        raise AssertionError(f"unexpected url {url}")


class SampleOffsets(unittest.TestCase):
    def test_same_seed_same_sample(self):
        self.assertEqual(C.sample_offsets(1000, 20, 7), C.sample_offsets(1000, 20, 7))

    def test_sorted_unique_in_range(self):
        got = C.sample_offsets(1000, 50, 0)
        self.assertEqual(got, sorted(set(got)))
        self.assertEqual(len(got), 50)
        self.assertTrue(all(0 <= x < 1000 for x in got))

    def test_limit_clamps_to_total(self):
        self.assertEqual(C.sample_offsets(5, 50, 0), [0, 1, 2, 3, 4])


class SplitTurn(unittest.TestCase):
    def test_think_and_body(self):
        turn = C.split_turn(CONTENT)
        self.assertEqual(turn.think, THINK)
        self.assertEqual(turn.body, BODY)

    def test_round_trip(self):
        self.assertEqual(C.join_turn(C.split_turn(CONTENT)), CONTENT)

    def test_separator_kept(self):
        content = "<think>x</think>\n\n" + json.dumps(BODY, indent=2)
        self.assertEqual(C.join_turn(C.split_turn(content)), content)

    def test_fenced_body_round_trip(self):
        content = "<think>x</think>```json\n" + json.dumps(BODY, indent=2) + "\n```"
        turn = C.split_turn(content)
        self.assertEqual(turn.body, BODY)
        self.assertEqual(C.join_turn(turn), content)

    def test_rejects_unbalanced_fence(self):
        self.assertIsNone(C.split_turn("```json\n" + json.dumps(BODY)))
        self.assertIsNone(C.split_turn(json.dumps(BODY) + "\n```"))

    def test_no_think(self):
        turn = C.split_turn(json.dumps(BODY))
        self.assertIsNone(turn.think)
        self.assertEqual(turn.body, BODY)

    def test_rejects_text_outside_json(self):
        self.assertIsNone(C.split_turn(json.dumps(BODY) + " trailing words"))
        self.assertIsNone(C.split_turn("Sure! " + json.dumps(BODY)))

    def test_rejects_bad_json(self):
        self.assertIsNone(C.split_turn("<think>x</think>{not json"))

    def test_rejects_missing_prose(self):
        self.assertIsNone(C.split_turn(json.dumps({"analysis": "a", "commands": []})))
        self.assertIsNone(C.split_turn(json.dumps({"analysis": "a", "plan": 3})))

    def test_with_prose_keeps_actions(self):
        after = C.split_turn(CONTENT).with_prose(GOOD)
        self.assertEqual(after.think, GOOD["think"])
        self.assertEqual(after.body["commands"], BODY["commands"])
        self.assertEqual(after.body["task_complete"], BODY["task_complete"])

    def test_with_prose_no_think_stays_none(self):
        after = C.split_turn(json.dumps(BODY)).with_prose(GOOD)
        self.assertIsNone(after.think)


class Facts(unittest.TestCase):
    def test_payload_tokens(self):
        got = C.facts(
            "Run `ls -la /tmp` then edit src/app/main.py line 42 with --force; "
            "call get_user_id and parseConfig on pydicom.multival."
        )
        for fact in ("ls -la /tmp", "/tmp", "src/app/main.py", "42", "--force", "-la",
                     "get_user_id", "parseConfig", "pydicom.multival"):
            self.assertIn(fact, got)

    def test_mixed_alnum(self):
        self.assertTrue({"x86_64", "sha256"} <= C.facts("x86_64 and sha256"))

    def test_skips_enumerators_and_abbreviations(self):
        got = C.facts("1. Explore the repo\n2) Fix it, e.g. quickly. Step 3 is tests.")
        self.assertFalse({"1", "2", "3", "e.g"} & got)

    def test_strips_trailing_punctuation(self):
        self.assertIn("/etc/init.d/postfix", C.facts("Check /etc/init.d/postfix."))

    def test_number_is_whole_word(self):
        self.assertFalse(C.present("4", "port 42"))
        self.assertTrue(C.present("4", "port 4, then 42"))


class Negations(unittest.TestCase):
    def test_classes(self):
        got = C.negations("It doesn't work, never mind, only one, no files, except x")
        self.assertEqual(got, {"not", "never", "only", "except"})

    def test_not_and_no_are_one_class(self):
        self.assertEqual(C.negations("command silent"), {"not"})
        self.assertEqual(C.negations("didn't find any"), C.negations("No tests found"))

    def test_none(self):
        self.assertEqual(C.negations("All good."), set())


class CheckTurn(unittest.TestCase):
    def setUp(self):
        self.before = C.split_turn(CONTENT)

    def check(self, prose):
        return C.check_turn(self.before, self.before.with_prose(prose), words)

    def test_pass(self):
        self.assertEqual(self.check(GOOD).reasons, [])

    def test_missing_fact(self):
        got = self.check(LOST_PATH)
        self.assertIn("facts", got.reasons)
        self.assertIn("/etc/postfix/main.cf", got.facts)

    def test_missing_negation(self):
        got = self.check(LOST_NEGATION)
        self.assertEqual(got.reasons, ["negation"])
        self.assertEqual(got.negations, ["not"])

    def test_longer(self):
        long = {k: (THINK if k == "think" else BODY[k]) + " extra words here" for k in GOOD}
        self.assertIn("longer", self.check(long).reasons)

    def test_empty_field(self):
        self.assertIn("empty", self.check({**GOOD, "think": " "}).reasons)

    def test_tag_injection(self):
        self.assertIn("tag", self.check({**GOOD, "plan": GOOD["plan"] + "</think>"}).reasons)

    def test_actions_changed(self):
        after = self.before.with_prose(GOOD)
        after.body["commands"] = [{"keystrokes": "rm -rf /\n", "duration": 0.1}]
        self.assertIn("actions", C.check_turn(self.before, after, words).reasons)


class ParseReply(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(C.parse_reply(json.dumps(GOOD)), GOOD)

    def test_fenced(self):
        self.assertEqual(C.parse_reply("```json\n" + json.dumps(GOOD) + "\n```"), GOOD)

    def test_leaked_think_and_missing_think_key(self):
        got = C.parse_reply('<think>hmm</think>{"analysis": "a", "plan": "b"}')
        self.assertEqual(got, {"think": "", "analysis": "a", "plan": "b"})

    def test_rejects(self):
        self.assertIsNone(C.parse_reply('{"analysis": 1, "plan": "b"}'))
        self.assertIsNone(C.parse_reply("nope"))


class BuildMessages(unittest.TestCase):
    def test_rules_and_payload(self):
        msgs = C.build_messages(C.split_turn(CONTENT))
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertIn("verbatim", msgs[0]["content"])
        payload = json.loads(msgs[1]["content"])
        self.assertEqual(payload["think"], THINK)
        self.assertEqual(payload["level"], "full")
        self.assertEqual(payload["commands"], ["dpkg -l | grep postfix\n", "which postfix\n"])

    def test_level_in_payload_and_rules(self):
        msgs = C.build_messages(C.split_turn(CONTENT), "ultra")
        self.assertEqual(json.loads(msgs[1]["content"])["level"], "ultra")
        for level in C.LEVELS:
            self.assertIn(f"- {level}:", msgs[0]["content"])

    def test_system_prompt_same_for_every_level(self):
        turn = C.split_turn(CONTENT)
        self.assertEqual(C.build_messages(turn, "lite")[0], C.build_messages(turn, "ultra")[0])

    def test_long_keystrokes_clipped(self):
        turn = C.split_turn(json.dumps({**BODY, "commands": [{"keystrokes": "x" * 999}]}))
        payload = json.loads(C.build_messages(turn)[1]["content"])
        self.assertLess(len(payload["commands"][0]), 200)


class ShrinkTurn(unittest.TestCase):
    def setUp(self):
        self.turn = C.split_turn(CONTENT)

    def test_first_try(self):
        ask = scripted(GOOD)
        got = run(C.shrink_turn(self.turn, ask, words))
        self.assertTrue(got.ok)
        self.assertEqual(got.tries, 1)
        self.assertEqual(got.turn.think, GOOD["think"])

    def test_retry_names_missing_fact(self):
        ask = scripted(LOST_PATH, GOOD)
        got = run(C.shrink_turn(self.turn, ask, words))
        self.assertTrue(got.ok)
        self.assertEqual(got.tries, 2)
        self.assertIn("/etc/postfix/main.cf", ask.calls[1][-1]["content"])

    def test_retry_after_bad_reply(self):
        got = run(C.shrink_turn(self.turn, scripted("not json", GOOD), words))
        self.assertTrue(got.ok)
        self.assertEqual(got.tries, 2)

    def test_gives_up_keeps_original(self):
        got = run(C.shrink_turn(self.turn, scripted(LOST_PATH), words))
        self.assertFalse(got.ok)
        self.assertIs(got.turn, self.turn)
        self.assertIn("facts", got.check.reasons)
        self.assertEqual(got.draft, LOST_PATH)

    def test_model_error_keeps_original(self):
        got = run(C.shrink_turn(self.turn, scripted(TimeoutError("slow")), words))
        self.assertFalse(got.ok)
        self.assertEqual(got.check.reasons, ["error"])
        self.assertIn("TimeoutError", got.check.detail)

    def test_level_recorded(self):
        got = run(C.shrink_turn(self.turn, scripted(GOOD), words, level="ultra"))
        self.assertEqual(got.level, "ultra")

    def test_lost_fact_retries_same_level(self):
        ask = scripted(LOST_PATH, GOOD)
        got = run(C.shrink_turn(self.turn, ask, words, level="full"))
        self.assertEqual((got.ok, got.level, got.attempts), (True, "full", [["facts"], []]))
        self.assertIn("level full", ask.calls[1][-1]["content"])

    def test_replay_miss_retries_gentler_level(self):
        ask = scripted(GOOD)
        got = run(C.shrink_turn(self.turn, ask, words, level="full", verify=verifier(C.Replay(0.2, 0.9, False))))
        self.assertEqual((got.level, got.attempts), ("lite", [["replay"], ["replay"]]))
        self.assertIn("level lite", ask.calls[1][-1]["content"])

    def test_longer_retries_same_level(self):
        long = {k: (THINK if k == "think" else BODY[k]) + " more" for k in GOOD}
        got = run(C.shrink_turn(self.turn, scripted(long, GOOD), words, level="full"))
        self.assertEqual((got.ok, got.level), (True, "full"))

    def test_replay_pass(self):
        got = run(C.shrink_turn(self.turn, scripted(GOOD), words, verify=verifier(C.Replay(0.9))))
        self.assertTrue(got.ok)
        self.assertEqual(got.check.replay.score, 0.9)

    def test_replay_fail_retries_then_keeps_original(self):
        verify = verifier(C.Replay(0.2, 0.9, False))
        ask = scripted(GOOD)
        got = run(C.shrink_turn(self.turn, ask, words, verify=verify))
        self.assertFalse(got.ok)
        self.assertIs(got.turn, self.turn)
        self.assertEqual((got.check.reasons, got.tries, len(verify.calls)), (["replay"], 2, 2))
        self.assertIn("commands", ask.calls[1][-1]["content"])

    def test_replay_error_keeps_original(self):
        got = run(C.shrink_turn(self.turn, scripted(GOOD), words, verify=verifier(OSError("down"))))
        self.assertEqual((got.ok, got.check.reasons), (False, ["error"]))


def verifier(result):
    """Fake replay check: returns (or raises) one fixed result and records each turn."""
    calls = []

    async def verify(turn):
        calls.append(turn)
        if isinstance(result, Exception):
            raise result
        return result

    verify.calls = calls
    return verify


class Levels(unittest.TestCase):
    def test_gentler(self):
        self.assertEqual([C.gentler(x) for x in ("ultra", "full", "lite")], ["full", "lite", "lite"])

    def test_hard_after_error(self):
        turn = C.split_turn(CONTENT)
        self.assertTrue(C.is_hard("Traceback (most recent call last):\n  x", turn, words))
        self.assertTrue(C.is_hard("bash: foo: command not found\n$", turn, words))
        self.assertFalse(C.is_hard("$ ls\nREADME.md\n$", turn, words))

    def test_hard_on_line_anchored_failures(self):
        turn = C.split_turn(CONTENT)
        for output in (
            "x\nKeyError: 'name'\n$", "E   ValueError: bad\n", "error: externally-managed-environment\n",
            "E: Unable to locate package foo\n", "src/a.c:3:5: error: expected ';'\n", "bash: line 3: syntax error near `('\n",
            "FAIL: test_edges (t.TestX)\n", "test_edges (t.TestX) ... FAIL\n", "== 1 failed, 2 passed ==\n",
            "make: *** [Makefile:55: all] Error 1\n", "error[E0599]: no function\n",
        ):
            self.assertTrue(C.is_hard("New Terminal Output:\n" + output, turn, words), output)

    def test_not_hard_on_error_words(self):
        turn = C.split_turn(CONTENT)
        for output in (
            "    except KeyError:\n        pass\n", 'throw new Error(`bad`);\n', "# Fix the error message\n",
            "-rw-r--r-- 1 root root 26 Jul 12  2025 errors.py\n", "== 0 failed, 3 passed ==\n", "loss: mean squared error\n",
            "grep: write error: Broken pipe\n",
        ):
            self.assertFalse(C.is_hard("New Terminal Output:\n" + output, turn, words), output)

    def test_echoed_input_and_task_ignored(self):
        turn = C.split_turn(CONTENT)
        typed = "New Terminal Output:\nroot@ab12:/app# grep -rn 'KeyError: x' .\n> FAIL: heredoc line\nroot@ab12:/app# \n"
        task = "Task Description:\nFix the KeyError:\nTraceback (most recent call last) in the issue.\n\nCurrent terminal state:\nroot@ab12:/app# \n"
        warned = "Previous response had parsing errors:\nERROR: Missing required fields: analysis, plan\n"
        for observation in (typed, task, warned):
            self.assertFalse(C.is_hard(observation, turn, words), observation)
        self.assertTrue(C.is_hard(warned + "\nNew Terminal Output:\nbash: make: command not found\n", turn, words))

    def test_hard_when_reasoning_long(self):
        turn = C.split_turn(CONTENT).with_prose({**GOOD, "think": "word " * C.HARD_TOKENS})
        self.assertTrue(C.is_hard("$ ls\n", turn, words))


class Replays(unittest.TestCase):
    def setUp(self):
        self.turn = C.split_turn(CONTENT)
        self.right = {"commands": [{"keystrokes": "dpkg -l | grep postfix\n"}, {"keystrokes": "which postfix\n"}]}
        self.wrong = {"commands": [{"keystrokes": "apt-get install -y nginx\n"}]}

    def test_similarity(self):
        self.assertEqual(C.similarity(["ls  -la\n"], ["ls -la"]), 1.0)
        self.assertEqual(C.similarity([], []), 1.0)
        self.assertLess(C.similarity(["apt-get update"], ["cat /etc/hosts"]), 0.5)

    def test_parse_commands(self):
        self.assertEqual(C.parse_commands(json.dumps(self.right)), ["dpkg -l | grep postfix\n", "which postfix\n"])
        self.assertEqual(C.parse_commands('{"commands": ["ls\\n"]}'), ["ls\n"])
        self.assertEqual(C.parse_commands("no json"), [])

    def test_messages_hide_the_answer(self):
        task = "You are an agent...\nTask Description:\nFix postfix mail."
        msgs = C.replay_messages(task, "$ dpkg --configure -a\n$", self.turn)
        text = msgs[-1]["content"]
        self.assertIn("Fix postfix mail.", text)
        self.assertNotIn("You are an agent", text)
        self.assertIn("dpkg --configure -a", text)
        self.assertIn(BODY["plan"], text)
        self.assertNotIn("grep postfix", text)

    def test_messages_clip_long_context(self):
        msgs = C.replay_messages("Task Description:\n" + "t" * 9999, "o" * 9999 + "END", self.turn)
        self.assertLess(len(msgs[-1]["content"]), 9000)
        self.assertIn("END", msgs[-1]["content"])

    def test_high_score_passes_in_one_call(self):
        ask = scripted(self.right)
        got = run(C.make_verify(ask, "task", "obs", self.turn)(self.turn.with_prose(GOOD)))
        self.assertEqual((got.ok, got.score, got.base, len(ask.calls)), (True, 1.0, None, 1))

    def test_low_score_passes_when_original_no_better(self):
        ask = scripted(self.wrong)
        got = run(C.make_verify(ask, "task", "obs", self.turn)(self.turn.with_prose(GOOD)))
        self.assertTrue(got.ok)
        self.assertEqual(len(ask.calls), 2)
        self.assertEqual(got.score, got.base)

    def test_low_score_fails_when_original_better(self):
        got = run(C.make_verify(scripted(self.wrong, self.right), "task", "obs", self.turn)(self.turn.with_prose(GOOD)))
        self.assertFalse(got.ok)
        self.assertEqual(got.base, 1.0)

    def test_base_scored_once(self):
        ask = scripted(self.wrong)
        verify = C.make_verify(ask, "task", "obs", self.turn)
        run(verify(self.turn.with_prose(GOOD)))
        run(verify(self.turn.with_prose(GOOD)))
        self.assertEqual(len(ask.calls), 3)

    def test_no_commands_skips_model(self):
        turn = C.split_turn(json.dumps({**BODY, "commands": []}))
        ask = scripted(self.wrong)
        got = run(C.make_verify(ask, "task", "obs", turn)(turn))
        self.assertEqual((got.ok, len(ask.calls)), (True, 0))


class Budget(unittest.TestCase):
    def test_prepends_system(self):
        conv = row()["conversations"]
        got = C.tag_budget(conv, "full")
        self.assertEqual(got[0], {"role": "system", "content": "Reasoning budget: full"})
        self.assertEqual(got[1:], conv)

    def test_merges_existing_system(self):
        got = C.tag_budget([{"role": "system", "content": "Be helpful."}], "ultra")
        self.assertEqual(got, [{"role": "system", "content": "Reasoning budget: ultra\n\nBe helpful."}])


class Weights(unittest.TestCase):
    def test_parse(self):
        got = C.parse_weights(["result=ContextLengthExceededError:0", "trace_source=None:0.5"])
        self.assertEqual(got, [("result", "ContextLengthExceededError", 0.0), ("trace_source", "None", 0.5)])

    def test_bad_spec(self):
        for spec in ("result", "result=x", "result=x:heavy", "=x:1"):
            with self.assertRaises(ValueError):
                C.parse_weights([spec])

    def test_row_weight(self):
        rules = C.parse_weights(["result=AgentTimeoutError:0.5", "trace_source=None:0.5", "result=None:2"])
        self.assertEqual(C.row_weight({"result": "AgentTimeoutError", "trace_source": None}, rules), 0.25)
        self.assertEqual(C.row_weight({"result": None, "trace_source": "main"}, rules), 2.0)
        self.assertEqual(C.row_weight({}, []), 1.0)


def row(idx=7):
    return {
        "row_idx": idx,
        "task": "demo",
        "conversations": [
            {"role": "user", "content": "Task: fix postfix."},
            {"role": "assistant", "content": CONTENT},
            {"role": "user", "content": "New Terminal Output:\n$"},
            {"role": "assistant", "content": "Plain summary, not JSON."},
        ],
    }


class ShrinkTrace(unittest.TestCase):
    def test_trace(self):
        out, records = run(C.shrink_trace(row(), scripted(GOOD), words, workers=2))
        conv = out["conversations"]
        self.assertEqual(conv[0], row()["conversations"][0])
        self.assertEqual(conv[2], row()["conversations"][2])
        self.assertEqual(conv[3]["content"], "Plain summary, not JSON.")
        shrunk = C.split_turn(conv[1]["content"])
        self.assertEqual(shrunk.body["commands"], BODY["commands"])
        self.assertEqual(shrunk.think, GOOD["think"])
        self.assertEqual(out["task"], "demo")
        meta = out["caveman"]
        self.assertEqual((meta["turns"], meta["parsed"], meta["shrunk"]), (2, 1, 1))
        self.assertLess(meta["content_tokens"][1], meta["content_tokens"][0])
        self.assertEqual([r["turn"] for r in records], [1, 3])
        self.assertEqual(records[1]["reasons"], ["unparsed"])
        self.assertTrue(all(r["row_idx"] == 7 for r in records))

    def test_failed_turn_is_byte_identical(self):
        out, records = run(C.shrink_trace(row(), scripted(LOST_PATH), words))
        self.assertEqual(out["conversations"][1]["content"], CONTENT)
        self.assertEqual(records[0]["draft"], LOST_PATH)
        self.assertEqual(records[0]["prose_tokens"][0], records[0]["prose_tokens"][1])

    def test_level_hard_and_tag(self):
        data = row()
        data["conversations"][0]["content"] = "Task: fix postfix.\nbash: postfix: command not found"
        out, records = run(C.shrink_trace(data, scripted(GOOD), words, level="full", tag=True, weight=0.5))
        self.assertEqual(out["conversations"][0], {"role": "system", "content": "Reasoning budget: full"})
        self.assertEqual(out["conversations"][1], data["conversations"][0])
        self.assertEqual((records[0]["hard"], records[0]["level"], records[0]["replay"]), (True, "lite", None))
        self.assertEqual((out["caveman"]["level"], out["caveman"]["weight"]), ("full", 0.5))

    def test_replay_recorded(self):
        right = {"commands": [{"keystrokes": c["keystrokes"]} for c in BODY["commands"]]}
        verify = scripted(right)
        _, records = run(C.shrink_trace(row(), scripted(GOOD), words, verifier=verify))
        self.assertEqual(records[0]["replay"], {"score": 1.0, "base": None, "ok": True})
        self.assertEqual(records[0]["attempts"], [[]])
        self.assertIn("Task: fix postfix.", verify.calls[0][-1]["content"])


class Summarize(unittest.TestCase):
    def test_stats(self):
        _, records = run(C.shrink_trace(row(), scripted(GOOD), words))
        got = C.summarize(records)
        self.assertEqual((got["traces"], got["turns"], got["unparsed"], got["shrunk"]), (1, 2, 1, 1))
        self.assertEqual(got["shrink_rate"], 1.0)
        self.assertGreater(got["prose_tokens"]["saved_pct"], 50)
        self.assertGreater(got["content_tokens"]["saved_pct"], 0)

    def test_empty(self):
        self.assertEqual(C.summarize([])["turns"], 0)

    def test_levels_hard_replay(self):
        right = {"commands": [{"keystrokes": c["keystrokes"]} for c in BODY["commands"]]}
        _, records = run(C.shrink_trace(row(), scripted(GOOD), words, level="ultra", verifier=scripted(right)))
        got = C.summarize(iter(records))
        self.assertEqual((got["levels"], got["hard"]), ({"ultra": 1}, 0))
        self.assertEqual(got["replay"], {"checked": 1, "failed": 0, "based": 0, "score_p50": 1.0})


class FetchRows(unittest.TestCase):
    def test_rows_and_truncation(self):
        opener = Opener({"/rows?": [
            {"rows": [{"row_idx": 3, "row": {"task": "a"}, "truncated_cells": []}]},
            {"rows": [{"row_idx": 9, "row": {"task": "b"}, "truncated_cells": ["conversations"]}]},
        ]})
        got = list(C.fetch_rows([3, 9], opener=opener, wait=0))
        self.assertEqual(got, [{"row_idx": 3, "task": "a"}])
        self.assertIn("offset=3", opener.urls[0])
        self.assertIn("length=1", opener.urls[0])

    def test_retries_then_succeeds(self):
        opener = Opener({"/rows?": [OSError("reset"), {"rows": [{"row_idx": 1, "row": {}, "truncated_cells": []}]}]})
        self.assertEqual(list(C.fetch_rows([1], opener=opener, wait=0)), [{"row_idx": 1}])

    def test_gives_up(self):
        opener = Opener({"/rows?": [OSError("a"), OSError("b"), OSError("c")]})
        with self.assertRaises(OSError):
            list(C.fetch_rows([1], opener=opener, wait=0, tries=3))

    def test_dataset_size(self):
        opener = Opener({"/size?": {"size": {"dataset": {"num_rows": 94334}}}})
        self.assertEqual(C.dataset_size(opener=opener), 94334)

    def test_backoff_honours_retry_after(self):
        error = OSError("429")
        error.headers = {"Retry-After": "7"}
        self.assertEqual(C._backoff(error, 1, 2.0), 7.0)
        self.assertEqual(C._backoff(OSError("x"), 3, 2.0), 8.0)
        self.assertEqual(C._backoff(OSError("x"), 20, 2.0), 60.0)


class RunShrink(unittest.TestCase):
    def test_resume_skips_done_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            C.write_jsonl(out / C.SOURCE, [row(1), row(2)])
            ask = scripted(GOOD)
            first = run(C.run_shrink(out, ask, words, limit=1))
            self.assertEqual(first["traces"], 1)
            calls = len(ask.calls)
            second = run(C.run_shrink(out, ask, words))
            self.assertEqual(second["traces"], 2)
            self.assertEqual(len(ask.calls), calls * 2)
            self.assertEqual([r["row_idx"] for r in C.read_jsonl(out / C.TRAIN)], [1, 2])
            stats = json.loads((out / C.STATS).read_text(encoding="utf-8"))
            self.assertEqual(stats["traces"], 2)

    def test_level_dir_created_from_shared_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            C.write_jsonl(root / C.SOURCE, [row(1)])
            run(C.run_shrink(root / "ultra", scripted(GOOD), words, source=root / C.SOURCE, level="ultra", tag=True))
            (got,) = C.read_jsonl(root / "ultra" / C.TRAIN)
            self.assertEqual(got["caveman"]["level"], "ultra")
            self.assertEqual(got["conversations"][0]["role"], "system")
            self.assertTrue((root / "ultra" / C.STATS).exists())

    def test_empty_source_writes_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = run(C.run_shrink(Path(tmp) / "full", scripted(GOOD), words, source=Path(tmp) / C.SOURCE))
            self.assertEqual(stats["traces"], 0)

    def test_zero_weight_rows_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            C.write_jsonl(out / C.SOURCE, [{**row(1), "result": "ContextLengthExceededError"}, row(2)])
            rules = C.parse_weights(["result=ContextLengthExceededError:0", "result=None:2"])
            ask = scripted(GOOD)
            run(C.run_shrink(out, ask, words, weights=rules))
            (got,) = C.read_jsonl(out / C.TRAIN)
            self.assertEqual((got["row_idx"], got["caveman"]["weight"]), (2, 2.0))
            self.assertEqual(len(ask.calls), 1)


class Cli(unittest.TestCase):
    ENV = {
        "CAVEMAN_URL": "", "CAVEMAN_KEY": "", "CAVEMAN_LLM": "",
        "AGENTIC_INFERENCE_URL": "https://hosted/v1", "AGENTIC_INFERENCE_KEY": "sk-x", "AGENTIC_INFERENCE_LLM": "vendor/model",
    }

    def args(self, *argv, **env):
        from unittest import mock

        with mock.patch.dict("os.environ", {**self.ENV, **env}):
            return C.parse_args(list(argv))

    def test_defaults(self):
        got = self.args("shrink")
        self.assertEqual((got.level, got.replay, got.tag, got.weights, got.ctx), ("full", True, True, [], 0))

    def test_inference_config_by_default(self):
        got = self.args("shrink")
        self.assertEqual((got.url, got.key, got.model), ("https://hosted/v1", "sk-x", "vendor/model"))

    def test_own_url_needs_own_model(self):
        lan = self.args("shrink", CAVEMAN_URL="http://lan/v1", CAVEMAN_LLM="local:27b")
        self.assertEqual((lan.url, lan.key, lan.model), ("http://lan/v1", "", "local:27b"))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.args("shrink", CAVEMAN_URL="http://lan/v1")

    def test_missing_config_exits(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.args("shrink", AGENTIC_INFERENCE_URL="", AGENTIC_INFERENCE_LLM="")

    def test_weights_parsed(self):
        self.assertEqual(self.args("shrink", "--weight", "result=None:0").weights, [("result", "None", 0.0)])

    def test_bad_weight_exits(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.args("shrink", "--weight", "result")

    def test_key_follows_its_url(self):
        self.assertEqual((self.args("shrink").url, self.args("shrink").key), ("https://hosted/v1", "sk-x"))
        lan = self.args("shrink", CAVEMAN_URL="http://lan/v1", CAVEMAN_LLM="local:27b")
        self.assertEqual((lan.url, lan.key), ("http://lan/v1", ""))

    def test_every_row_needs_parquet(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(C.cmd_fetch(self.args("--out", tmp, "fetch", "--limit", "0")), 2)


class Files(unittest.TestCase):
    def test_append_repairs_partial_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.jsonl"
            path.write_text('{"row_idx": 1}\n{"row_idx": 2, "cut', encoding="utf-8")
            C.write_jsonl(path, [{"row_idx": 3}], mode="a")
            self.assertEqual([r["row_idx"] for r in C.iter_jsonl(path)], [1, 3])

    def test_row_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.jsonl"
            C.write_jsonl(path, [{"row_idx": 5, "a": 1}, {"a": 2, "row_idx": 9}, {"a": 3}])
            self.assertEqual(C.row_ids(path), {5, 9, None})
            self.assertEqual(C.row_ids(Path(tmp) / "missing.jsonl"), set())

    def test_jsonable_dates(self):
        import datetime

        moment = datetime.datetime(2026, 4, 18, 9, 41, tzinfo=datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.jsonl"
            C.write_jsonl(path, [{"date": moment}])
            self.assertEqual(C.read_jsonl(path), [{"date": "2026-04-18T09:41:00+00:00"}])


try:
    import pyarrow  # noqa: F401
except ImportError:
    pyarrow = None


@unittest.skipIf(pyarrow is None, "pyarrow not installed")
class Parquet(unittest.TestCase):
    def setUp(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.paths = []
        for name, tasks in (("0000.parquet", ["a", "b", "c"]), ("0001.parquet", ["d", "e"])):
            table = pa.table({
                "task": tasks,
                "conversations": [[{"role": "user", "content": t}] for t in tasks],
                "result": pa.array([None] * len(tasks), pa.string()),
            })
            pq.write_table(table, self.dir / name, row_group_size=2)
            self.paths.append(self.dir / name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_total(self):
        self.assertEqual(C.parquet_total(self.paths), 5)

    def test_sampled_rows_across_shards(self):
        got = list(C.parquet_rows(self.paths, [4, 1, 3]))
        self.assertEqual([(r["row_idx"], r["task"]) for r in got], [(1, "b"), (3, "d"), (4, "e")])
        self.assertEqual(got[0]["conversations"], [{"role": "user", "content": "b"}])
        self.assertIsNone(got[0]["result"])
        self.assertEqual(next(iter(got[0])), "row_idx")

    def test_all_rows(self):
        self.assertEqual([r["task"] for r in C.parquet_rows(self.paths)], list("abcde"))

    def test_shard_list(self):
        files = [
            {"config": "default", "split": "train", "filename": "0001.parquet", "url": "u1", "size": 2},
            {"config": "default", "split": "test", "filename": "0000.parquet", "url": "t0", "size": 2},
            {"config": "default", "split": "train", "filename": "0000.parquet", "url": "u0", "size": 2},
        ]
        opener = Opener({"/parquet?": {"parquet_files": files}})
        self.assertEqual([f["url"] for f in C.parquet_files(opener=opener)], ["u0", "u1"])

    def test_download_then_skip(self):
        urls = []

        def opener(url):
            urls.append(url)
            return io.BytesIO(b"abc")

        path = self.dir / "shard.parquet"
        C.download("u", path, 3, opener=opener)
        C.download("u", path, 3, opener=opener)
        self.assertEqual((path.read_bytes(), urls), (b"abc", ["u"]))
        self.assertFalse(path.with_name("shard.parquet.part").exists())

    def test_download_size_mismatch(self):
        path = self.dir / "shard.parquet"
        with self.assertRaises(OSError):
            C.download("u", path, 9, opener=lambda url: io.BytesIO(b"abc"), tries=2, wait=0)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
