import asyncio
import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from home_assistant import Config, ConfigurationError, HomeAssistant, HomeAssistantError, parse_reply


def response(text="The test lamp is on.", kind="action_done", conversation_id="test-conversation"):
    return {"conversation_id": conversation_id,
            "response": {"response_type": kind, "speech": {"plain": {"speech": text}},
                         "data": {"success": [], "failed": []}}}


class FakeHomeAssistant(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(length)) if length else None
        self.server.requests.append((self.command, self.path, self.headers.get("Authorization"), data))
        time.sleep(self.server.delay)
        self.send_response(self.server.status)
        if self.server.status == 302:
            self.send_header("Location", self.server.url + "/redirect-target")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload = {"message": "API running."} if self.path == "/api/" else self.server.payload
        with contextlib.suppress(BrokenPipeError):
            self.wfile.write(json.dumps(payload).encode())


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHomeAssistant)
        self.server.url = f"http://127.0.0.1:{self.server.server_port}"
        self.server.requests = []
        self.server.payload = response()
        self.server.status = 200
        self.server.delay = 0
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.client = HomeAssistant(Config(self.server.url, "test-secret-token"))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    async def test_authenticated_commands_and_follow_up(self):
        await self.client.check()
        first = await self.client.process("Turn on the test lamp")
        second = await self.client.process("Turn it off", first.conversation_id)
        self.assertEqual(first.speech, "The test lamp is on.")
        self.assertEqual(second.conversation_id, "test-conversation")
        self.assertEqual(self.server.requests[0][:2], ("GET", "/api/"))
        first_request, second_request = self.server.requests[1:]
        self.assertEqual(first_request[:3], ("POST", "/api/conversation/process", "Bearer test-secret-token"))
        self.assertEqual(first_request[3], {"text": "Turn on the test lamp", "language": "en"})
        self.assertEqual(second_request[3]["conversation_id"], "test-conversation")

    async def test_explicit_conversation_entity_id_is_passed_to_assist(self):
        client = HomeAssistant(Config(self.server.url, "test-token", agent_id="conversation.home_assistant"))
        await client.process("Is the test lamp on?")
        self.assertEqual(self.server.requests[0][3]["agent_id"], "conversation.home_assistant")

    async def test_authentication_failure_does_not_expose_body_or_token(self):
        self.server.status = 401
        self.server.payload = {"private": "test-secret-token"}
        with self.assertRaises(HomeAssistantError) as caught:
            await self.client.process("Turn on the test lamp")
        self.assertIn("rejected the access token", str(caught.exception))
        self.assertNotIn("test-secret-token", str(caught.exception))
        self.assertEqual(len(self.server.requests), 1)

    async def test_redirect_is_not_followed(self):
        self.server.status = 302
        with self.assertRaisesRegex(HomeAssistantError, "redirected"):
            await self.client.process("Turn on the test lamp")
        self.assertEqual(len(self.server.requests), 1)

    async def test_timeout_does_not_retry_device_action(self):
        self.server.delay = 0.1
        client = HomeAssistant(Config(self.server.url, "test-secret-token", timeout=0.02))
        with self.assertRaisesRegex(HomeAssistantError, "confirm the result"):
            await client.process("Turn on the test lamp")
        self.assertEqual(len(self.server.requests), 1)

    async def test_invalid_success_response_is_not_acknowledged_as_success(self):
        self.server.payload = {"unexpected": "data"}
        with self.assertRaisesRegex(HomeAssistantError, "invalid response"):
            await self.client.process("Turn on the test lamp")


class ConfigAndReplyTests(unittest.TestCase):
    def test_private_config_and_relative_token_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / "config.json"
            (path.parent / "token").write_text("test-secret-token\n")
            path.write_text(json.dumps({"url": "http://localhost:8123/", "token_file": "token"}))
            config = Config.load(path)
            self.assertEqual(config.url, "http://localhost:8123")
            self.assertEqual(config.token, "test-secret-token")
            self.assertNotIn("test-secret-token", repr(config))

    def test_environment_overrides_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"url": "http://localhost:8123", "token_file": "does-not-exist"}))
            with patch.dict(os.environ, {"HOME_ASSISTANT_TOKEN": "environment-token",
                                         "HOME_ASSISTANT_URL": "https://home.example.test",
                                         "HOME_ASSISTANT_AGENT_ID": "conversation.custom"}, clear=True):
                config = Config.load(path)
                self.assertEqual(config.url, "https://home.example.test")
                self.assertEqual(config.token, "environment-token")
                self.assertEqual(config.agent_id, "conversation.custom")

    def test_missing_configuration_is_actionable(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                Config.load(Path(folder) / "missing.json")

    def test_rejects_invalid_urls_tokens_and_timeouts(self):
        for url in ["", "file:///tmp/test", "http://user:secret@host", "http://host?token=secret",
                    "http://host#fragment", "http://host:bad", "http://host name", None, 123]:
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                Config(url, "test-token")
        for token in [None, "", "a\nb", "Bearer token"]:
            with self.subTest(token=token), self.assertRaises(ConfigurationError):
                Config("http://localhost", token)
        for timeout in [0, -1, 121, float("nan"), True, "20"]:
            with self.subTest(timeout=timeout), self.assertRaises(ConfigurationError):
                Config("http://localhost", "test-token", timeout=timeout)

    def test_assist_errors_are_spoken_verbatim(self):
        reply = parse_reply(response("I couldn't find the kitchen light.", "error"))
        self.assertEqual(reply.speech, "I couldn't find the kitchen light.")
        self.assertEqual(reply.response_type, "error")

    def test_ssml_reply_is_spoken_as_text(self):
        payload = response()
        payload["response"]["speech"] = {"ssml": {"speech": "<speak>The lamp is <emphasis>on</emphasis>.</speak>"}}
        reply = parse_reply(payload)
        self.assertNotIn("<", reply.speech)
        self.assertIn("lamp is", reply.speech)

    def test_partial_failure_never_falls_back_to_done(self):
        payload = response("")
        payload["response"]["data"]["failed"] = [{"id": "light.test"}]
        self.assertIn("could not complete", parse_reply(payload).speech)
        payload["response"]["response_type"] = "error"
        self.assertIn("could not handle", parse_reply(payload).speech)


if __name__ == "__main__":
    unittest.main()
