import asyncio
import contextlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import packet, read_packet
from huddle_operator import OperatorCall, choice, confirmed, lookup_query

USER = {"id": "U1234567890", "name": "Test Person", "username": "test.person"}


class DialogueTests(unittest.IsolatedAsyncioTestCase):
    def call(self, texts, users=None):
        call = OperatorCall(None, None, None)
        call.play = AsyncMock()
        call.text = AsyncMock(side_effect=texts)
        call.handoff = AsyncMock()
        call.client = SimpleNamespace(owner_id=USER["id"], request=AsyncMock(return_value={
            "users": users or [USER], "more": False}))
        return call

    async def test_name_requires_explicit_confirmation(self):
        call = self.call(["call Test Person", "yes"])
        async def handoff(user):
            self.assertEqual(call.text.await_count, 2)
            self.assertIn("username test.person", call.play.await_args_list[-2].args[0])
            self.assertEqual(user, USER)
        call.handoff.side_effect = handoff
        await call.dialogue()
        call.client.request.assert_awaited_once_with("lookup", query="Test Person")
        call.handoff.assert_awaited_once_with(USER)

    async def test_no_confirmation_or_negative_answer_never_invites(self):
        for answer in ("no", "maybe", "*", "silence"):
            call = self.call(["call Test Person", answer, "cancel"])
            await call.dialogue()
            call.handoff.assert_not_awaited()

    async def test_ambiguous_name_selects_then_confirms(self):
        other = {**USER, "id": "U9876543210", "username": "other.person"}
        call = self.call(["call Test Person", "option two", "#"], [USER, other])
        await call.dialogue()
        call.handoff.assert_awaited_once_with(other)

    async def test_call_me_uses_configured_owner_id(self):
        call = self.call(["call me", "yes please"])
        await call.dialogue()
        call.client.request.assert_awaited_once_with("lookup", query=USER["id"])

    async def test_directory_dial_resolves_exact_contact_before_handoff(self):
        call = self.call([])
        call.directory_number = "880012"
        target = {"user_id": USER["id"], "name": USER["name"], "username": USER["username"]}
        with patch("control.platform_request", new_callable=AsyncMock) as resolve:
            resolve.return_value = {"integration": "slack-huddles", "target": target}
            await call.dialogue()
        resolve.assert_awaited_once_with("/api/v1/directories/resolve", {"number": "880012"})
        call.handoff.assert_awaited_once_with({**target, "id": USER["id"]})
        call.text.assert_not_awaited()
        call.client.request.assert_not_awaited()

    async def test_unresolved_directory_never_calls_someone_else(self):
        call = self.call([])
        call.directory_number = "880012"
        with patch("control.platform_request", new_callable=AsyncMock) as resolve:
            resolve.return_value = {"integration": "other-app", "target": {"user_id": "UOTHER"}}
            await call.dialogue()
        call.handoff.assert_not_awaited()
        self.assertIn("could not connect", call.play.await_args.args[0])

    async def test_generic_operator_answers_question_then_joins_without_second_dial(self):
        call = self.call(["Call Alex", "option two"])
        call.call_id = str(uuid.uuid4())
        call.join_huddle = AsyncMock()
        request_id = str(uuid.uuid4())
        answered = False
        seen = []

        async def api(path, payload=None, **kwargs):
            nonlocal answered
            seen.append((path, payload, kwargs))
            if path == "/api/v1/operators":
                return {"id": "codex:12"}
            if path == "/api/v1/operators/calls/" + call.call_id:
                return {"request_id": request_id} if answered else {"state": "active"}
            if path == "/api/v1/questions/question-1/answer":
                self.assertEqual({"item_id": "person", "text": "Alex Two"}, payload)
                answered = True
                return {"state": "answered"}
            if path == "/api/v1/sessions/codex:12":
                return {"state": "waiting", "questions": [{"id": "question-1", "state": "pending", "answers": {},
                    "questions": [{"id": "person", "question": "Which Alex?", "options": [{"label": "Alex One"}, {"label": "Alex Two"}]}]}]}
            raise AssertionError(path)

        async def play(text):
            if text.startswith("Which Alex?"):
                call.inputs.put_nowait("spoken-answer-ready")
        call.play.side_effect = play
        with patch("control.platform_request", side_effect=api):
            await call.generic_dialogue()
        self.assertEqual("codex:12", call.operator_session_id)
        self.assertEqual(call.call_id, seen[0][1]["call_id"])
        self.assertEqual("phone-operator:" + call.call_id, seen[0][2]["idempotency_key"])
        call.join_huddle.assert_awaited_once_with(request_id)
        call.client.request.assert_not_awaited()
        call.handoff.assert_not_awaited()

    async def test_generic_operator_failure_does_not_fall_back_to_another_dial(self):
        call = self.call(["Call Alex"])
        call.call_id = str(uuid.uuid4())
        with patch("control.platform_request", new_callable=AsyncMock) as api:
            api.side_effect = RuntimeError("Platform unavailable")
            await call.generic_dialogue()
        call.client.request.assert_not_awaited()
        call.handoff.assert_not_awaited()
        self.assertIn("couldn't finish", call.play.await_args.args[0])

    def test_language_parser_does_not_treat_uncertainty_as_yes(self):
        self.assertEqual(lookup_query("Could you connect me to Test Person please?"), "Test Person")
        self.assertEqual(choice("number three"), 3)
        self.assertFalse(confirmed("yes but wait"))
        self.assertFalse(confirmed("no"))
        self.assertTrue(confirmed("Yes, please."))


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_display_changes_only_after_native_huddle_connects(self):
        call = OperatorCall(None, None, None)
        call.call_id, call.request_id = str(uuid.uuid4()), str(uuid.uuid4())
        call.client = SimpleNamespace(request=AsyncMock(side_effect=[
            {"phase": "joining"},
            {"phase": "connected", "user_id": USER["id"],
             "display": {"name": "Slack Nickname", "number": "880012"}},
        ]))
        with patch("huddle_operator.PBX") as pbx:
            pbx.return_value.connected_line = AsyncMock()
            await call.update_huddle_display()
            pbx.return_value.connected_line.assert_awaited_once_with(call.call_id, "Slack Nickname", "880012")

    async def test_display_failure_does_not_close_audio_or_cancel_huddle(self):
        call = OperatorCall(None, None, None)
        call.call_id, call.request_id = str(uuid.uuid4()), str(uuid.uuid4())
        call.client = SimpleNamespace(request=AsyncMock(return_value={
            "phase": "connected", "user_id": USER["id"], "display_name": USER["name"]}))
        with patch("huddle_operator.PBX") as pbx:
            pbx.return_value.connected_line = AsyncMock(side_effect=RuntimeError("Unavailable"))
            with self.assertLogs("huddle-operator", level="WARNING"):
                await call.update_huddle_display()
        self.assertFalse(call.closed)
        call.client.request.assert_awaited_once_with("status", request_id=call.request_id)

    async def test_hangup_during_status_check_does_not_update_another_call(self):
        call = OperatorCall(None, None, None)
        call.call_id, call.request_id = str(uuid.uuid4()), str(uuid.uuid4())
        async def status(*args, **kwargs):
            call.closed = True
            return {"phase": "connected", "user_id": USER["id"]}
        call.client = SimpleNamespace(request=status)
        with patch("huddle_operator.PBX") as pbx:
            await call.update_huddle_display()
            pbx.assert_not_called()

    async def test_real_tcp_handoff_preserves_bidirectional_audio_without_transcription(self):
        call_id = str(uuid.uuid4())
        received = asyncio.get_running_loop().create_future()
        handlers = set()
        async def backend(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                kind, payload = await read_packet(reader)
                self.assertEqual((kind, str(uuid.UUID(bytes=payload))), (1, call_id))
                kind, payload = await read_packet(reader)
                received.set_result((kind, payload))
                writer.write(packet(0x10, b"\x34\x56" * 160))
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)
        relay = await asyncio.start_server(backend, "127.0.0.1", 0)
        port = relay.sockets[0].getsockname()[1]
        client = SimpleNamespace(audio_port=port, request=AsyncMock(return_value={
            "phase": "awaiting-handset", "call_id": call_id}))
        bridge = SimpleNamespace(transcribe=AsyncMock(), synthesize=AsyncMock(return_value=bytes(320)))
        done = asyncio.Event()
        ready = asyncio.Event()
        calls = []
        async def accept(reader, writer):
            call = OperatorCall(bridge, reader, writer)
            calls.append(call)
            call.client = client
            receiver = asyncio.create_task(call.receive())
            handoff = asyncio.create_task(call.handoff(USER))
            try:
                while call.relay_writer is None:
                    await asyncio.sleep(.001)
                ready.set()
                await receiver
            finally:
                handoff.cancel()
                await asyncio.gather(handoff, return_exceptions=True)
                writer.close()
                await writer.wait_closed()
                done.set()
        operator = await asyncio.start_server(accept, "127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", operator.sockets[0].getsockname()[1])
            await asyncio.wait_for(ready.wait(), 2)
            data = packet(0x10, b"\x12\x34" * 160)
            writer.write(data[:2])
            await writer.drain()
            writer.write(data[2:])
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(received, 2), (0x10, b"\x12\x34" * 160))
            self.assertEqual(await asyncio.wait_for(read_packet(reader), 2), (0x10, b"\x34\x56" * 160))
            bridge.transcribe.assert_not_awaited()
            writer.write(packet(0x00))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(done.wait(), 2)
        finally:
            operator.close()
            relay.close()
            await operator.wait_closed()
            await relay.wait_closed()
            await asyncio.gather(*list(handlers), return_exceptions=True)
