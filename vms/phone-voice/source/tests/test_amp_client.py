import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from amp_client import AmpClient, PENDING_PREFIX, thread_url
from amp_control import AmpControl
from session_store import Store


NATIVE = "T-11111111-2222-3333-4444-555555555555"
CHILD = "T-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
INIT = {"type": "system", "subtype": "init", "session_id": NATIVE}
RESULT = {"type": "result", "subtype": "success", "is_error": False,
          "result": "The work is finished.", "session_id": NATIVE}


class AmpClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.log = self.directory / "invocation.json"
        self.binary = self.directory / "amp"
        self.binary.write_text(f"#!{sys.executable}\n" + """
import json, os, sys, time
from pathlib import Path
prompt = sys.stdin.read()
Path(os.environ['FAKE_AMP_LOG']).write_text(json.dumps({'argv': sys.argv[1:], 'prompt': prompt}))
for event in json.loads(os.environ['FAKE_AMP_EVENTS']):
    if '_sleep' in event:
        time.sleep(event['_sleep'])
    elif '_raw' in event:
        print(event['_raw'], flush=True)
    else:
        print(json.dumps(event), flush=True)
print(os.environ.get('FAKE_AMP_ERROR', ''), file=sys.stderr, flush=True)
sys.exit(int(os.environ.get('FAKE_AMP_EXIT', '0')))
""")
        self.binary.chmod(0o700)
        self.env = patch.dict(os.environ, {
            "FAKE_AMP_LOG": str(self.log), "FAKE_AMP_EVENTS": json.dumps([INIT, RESULT]),
            "FAKE_AMP_EXIT": "0", "FAKE_AMP_ERROR": "", "AMP_API_KEY": "test-only-credential",
            "XDG_DATA_HOME": str(self.directory / "data"),
        })
        self.env.start()
        self.client = AmpClient(binary=str(self.binary), runner_id="test-runner", runner_dir="/remote/project")
        self.events = []
        self.client.on_event = lambda method, params: self.events.append((method, params))

    async def asyncTearDown(self):
        await self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    def script(self, events):
        os.environ["FAKE_AMP_EVENTS"] = json.dumps(events)

    async def test_first_turn_binds_native_thread_and_runs_only_on_runner(self):
        handle = await self.client.new_thread(number=42)
        self.assertTrue(handle.startswith(PENDING_PREFIX))
        self.assertFalse(self.log.exists())
        self.assertEqual("The work is finished.", await self.client.ask(handle, "Please inspect the project"))
        invocation = json.loads(self.log.read_text())
        argv = invocation["argv"]
        self.assertIn("runner:test-runner", argv)
        self.assertEqual("/remote/project", argv[argv.index("--runner-dir") + 1])
        self.assertEqual("private", argv[argv.index("--visibility") + 1])
        self.assertNotIn("Please inspect the project", argv)
        self.assertIn("Existing Switchboard session_id: 42", invocation["prompt"])
        self.assertNotIn("dangerously", " ".join(argv))
        bound = next(p for method, p in self.events if method == "thread/started")
        self.assertEqual(handle, bound["previousThreadId"])
        self.assertEqual(NATIVE, bound["threadId"])
        self.assertEqual(42, bound["number"])
        self.assertEqual(thread_url(NATIVE), bound["threadUrl"])
        self.assertEqual(2, sum(method == "amp/event" for method, _ in self.events))

    async def test_resume_continues_remote_executor_after_restart(self):
        await self.client.resume(NATIVE, "/remote/project", 42)
        await self.client.ask(NATIVE, "Continue")
        invocation = json.loads(self.log.read_text())
        self.assertEqual(["threads", "continue", NATIVE], invocation["argv"][:3])
        self.assertIn("--orb-execute", invocation["argv"])
        self.assertNotIn("--executor", invocation["argv"])
        self.assertFalse(any(method == "thread/started" for method, _ in self.events))

    async def test_subagent_text_and_tool_results_are_not_spoken_as_parent(self):
        self.script([INIT, {"type": "assistant", "session_id": CHILD, "parent_tool_use_id": "tool-child",
                           "message": {"content": [{"type": "text", "text": "Child internal response"}]}},
                     {"type": "assistant", "session_id": NATIVE,
                      "message": {"stop_reason": "tool_use", "content": [
                          {"type": "text", "text": "I am checking the files."},
                          {"type": "tool_use", "id": "cmd1", "name": "Bash", "input": {"cmd": "ls"}}]}}, RESULT])
        handle = await self.client.new_thread(number=2)
        self.assertEqual("The work is finished.", await self.client.ask(handle, "Check"))
        items = [p["item"] for method, p in self.events if method in {"item/started", "item/completed"}]
        self.assertEqual(["agentMessage", "commandExecution"], [i["type"] for i in items])
        self.assertEqual("I am checking the files.", items[0]["text"])
        self.assertEqual(4, sum(method == "amp/event" for method, _ in self.events))

    async def test_disconnect_is_not_reported_as_completion_or_replayed(self):
        self.script([INIT, {"type": "assistant", "session_id": NATIVE,
                           "message": {"content": [{"type": "text", "text": "I am working"}]}}])
        handle = await self.client.new_thread()
        with self.assertRaisesRegex(RuntimeError, "may still be running"):
            await self.client.ask(handle, "Work")
        self.assertEqual(NATIVE, self.client.aliases[handle])
        self.assertEqual({}, self.client.turns)

    async def test_unconfirmed_first_submission_cannot_create_duplicate_thread(self):
        self.script([])
        handle = await self.client.new_thread()
        with self.assertRaisesRegex(RuntimeError, "no native thread ID"):
            await self.client.ask(handle, "Work")
        with self.assertRaisesRegex(RuntimeError, "will not replay"):
            await self.client.ask(handle, "Work")
        restarted = AmpClient(binary=str(self.binary))
        with self.assertRaisesRegex(RuntimeError, "may already exist"):
            await restarted.resume(handle)

    async def test_invalid_native_binding_and_legacy_ids_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "not a native Amp thread"):
            await self.client.resume("codex-thread")
        await self.client.resume(NATIVE)
        self.script([{**INIT, "session_id": CHILD}, RESULT])
        with self.assertRaisesRegex(RuntimeError, "unexpected thread"):
            await self.client.ask(NATIVE, "Work")

    async def test_error_result_does_not_expose_raw_auth_output(self):
        self.script([INIT, {**RESULT, "is_error": True, "subtype": "error_during_execution",
                           "error": "Authorization: Bearer example-secret"}])
        os.environ["FAKE_AMP_ERROR"] = "secret login URL"
        handle = await self.client.new_thread()
        with self.assertRaises(RuntimeError) as error:
            await self.client.ask(handle, "Work")
        self.assertNotIn("secret", str(error.exception))
        self.assertIn(thread_url(NATIVE), str(error.exception))

    async def test_missing_login_fails_before_starting_an_unattended_cli(self):
        os.environ["AMP_API_KEY"] = ""
        with self.assertRaisesRegex(RuntimeError, "phone bridge service account"):
            await self.client.new_thread()
        self.assertFalse(self.log.exists())

    async def test_remote_cancel_requires_acknowledged_idle_state(self):
        self.script([INIT, {"_sleep": 30}, RESULT])
        started = asyncio.Event()
        self.client.on_event = lambda method, params: started.set() if method == "thread/started" else None
        handle = await self.client.new_thread()
        turn = asyncio.create_task(self.client.ask(handle, "Work"))
        await asyncio.wait_for(started.wait(), 2)
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            await self.client.interrupt(NATIVE)
        self.assertFalse(turn.done())
        with self.assertRaisesRegex(RuntimeError, "already running"):
            await self.client.ask(NATIVE, "Duplicate")
        control = self.client.control = AsyncMock(return_value={"ok": True, "state": "running"})
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            await self.client.interrupt(NATIVE)
        self.assertFalse(turn.done())
        control.return_value = {"ok": True, "state": "idle"}
        self.assertTrue(await self.client.interrupt(NATIVE))
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            await turn
        control.assert_awaited_with("cancel", NATIVE, None)

    async def test_running_steer_uses_control_and_never_falls_back_after_uncertain_delivery(self):
        self.script([INIT, {"_sleep": 30}, RESULT])
        started = asyncio.Event()
        self.client.on_event = lambda method, params: started.set() if method == "thread/started" else None
        handle = await self.client.new_thread()
        turn = asyncio.create_task(self.client.ask(handle, "Work"))
        await asyncio.wait_for(started.wait(), 2)
        self.assertFalse(await self.client.steer(NATIVE, "Use blue"))
        self.client.control = AsyncMock(return_value={"ok": False})
        with self.assertRaisesRegex(RuntimeError, "not queued again"):
            await self.client.steer(NATIVE, "Use blue")
        self.client.control.return_value = {"ok": True}
        self.assertTrue(await self.client.steer(NATIVE, "Use blue"))
        turn.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await turn

    def test_configured_is_only_local_configuration_not_runner_liveness(self):
        os.environ["AMP_API_KEY"] = ""
        self.assertFalse(self.client.configured)
        auth = self.directory / "data/amp/secrets.json"
        auth.parent.mkdir(parents=True)
        auth.write_text('{"example": "present"}')
        self.assertTrue(self.client.configured)
        self.assertEqual("runner:test-runner", self.client.executor)
        self.assertEqual("/not/on/phone", self.client.workspace("/not/on/phone"))
        with self.assertRaises(ValueError):
            self.client.workspace("../relative")
        with self.assertRaises(ValueError):
            self.client.workspace("/remote/../wrong")
        self.assertEqual("", thread_url(PENDING_PREFIX + "1"))


class AmpControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "sessions.db")
        self.session = self.store.create_session("Amp test", "/remote", thread_id=NATIVE,
                                                 engine="amp", executor="runner:test")
        self.control = AmpControl(self.store, timeout=1)

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    async def test_claim_once_validate_ack_and_report_actual_stop(self):
        task = asyncio.create_task(self.control.request("cancel", NATIVE))
        response = await self.control.poll([NATIVE], "plugin-a", wait_seconds=1)
        command = response["commands"][0]
        self.assertEqual(self.session["id"], response["sessions"][NATIVE]["id"])
        self.assertEqual([], (await self.control.poll([NATIVE], "plugin-b", wait_seconds=0))["commands"])
        with self.assertRaisesRegex(ValueError, "Unknown"):
            self.control.ack(command["id"], "plugin-b", {"ok": True, "state": "idle"})
        self.control.ack(command["id"], "plugin-a", {"ok": True, "state": "idle"})
        self.assertEqual({"ok": True, "state": "idle"}, await task)
        self.assertTrue(self.control.ack(command["id"], "plugin-a", {"ok": True, "state": "idle"})["accepted"])

    async def test_running_cancel_cannot_acknowledge_success(self):
        task = asyncio.create_task(self.control.request("cancel", NATIVE))
        command = (await self.control.poll([NATIVE], "plugin", wait_seconds=1))["commands"][0]
        self.control.ack(command["id"], "plugin", {"ok": True, "state": "running"})
        self.assertEqual({"ok": False, "state": "running"}, await task)

    async def test_unacknowledged_steering_is_expired_never_redelivered(self):
        task = asyncio.create_task(self.control.request("steer", NATIVE, "Use blue"))
        command = (await self.control.poll([NATIVE], "plugin", wait_seconds=1))["commands"][0]
        self.assertEqual("Use blue", command["text"])
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            await task
        self.assertEqual([], (await self.control.poll([NATIVE], "restarted-plugin", wait_seconds=0))["commands"])
        self.assertFalse(self.control.ack(command["id"], "plugin", {"ok": True})["accepted"])

    async def test_other_threads_cannot_claim_commands_and_lookup_is_read_only(self):
        task = asyncio.create_task(self.control.request("cancel", NATIVE))
        await asyncio.sleep(0)
        lookup = await self.control.poll([NATIVE], "plugin", lookup_only=True)
        self.assertEqual([], lookup["commands"])
        self.assertEqual(self.session["id"], lookup["sessions"][NATIVE]["id"])
        self.assertEqual({}, (await self.control.poll([CHILD], "plugin", wait_seconds=0))["sessions"])
        command = (await self.control.poll([NATIVE], "plugin", wait_seconds=0))["commands"][0]
        self.control.ack(command["id"], "plugin", {"ok": True, "state": "idle"})
        await task


if __name__ == "__main__":
    unittest.main()
