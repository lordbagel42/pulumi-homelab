import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatgpt_gateway import GatewayError, GatewayServer, MODEL, ollama_message, validate_request


class ConversionTests(unittest.TestCase):
    def test_only_offered_tools_can_be_returned(self):
        result = {"content": "", "tool_calls": [
            {"name": "HassTurnOn", "arguments_json": '{"name":"Wall Lights"}'}]}
        message = ollama_message(result, ["HassTurnOn"])
        self.assertEqual(message["tool_calls"][0]["function"],
                         {"name": "HassTurnOn", "arguments": {"name": "Wall Lights"}})
        with self.assertRaises(GatewayError):
            ollama_message(result, [])

    def test_bad_arguments_and_empty_answers_fail(self):
        for arguments in ("[]", "not json", None):
            with self.subTest(arguments=arguments), self.assertRaises(GatewayError):
                ollama_message({"content": "", "tool_calls": [
                    {"name": "HassTurnOn", "arguments_json": arguments}]}, ["HassTurnOn"])
        with self.assertRaises(GatewayError):
            ollama_message({"content": "", "tool_calls": []}, [])

    def test_unknown_models_and_unsupported_attachments_fail(self):
        base = {"model": MODEL, "messages": [{"role": "user", "content": "Hello"}]}
        for change in ({"model": "arbitrary-model"}, {"format": {"type": "object"}},
                       {"messages": [{"role": "user", "images": ["image"]}]},
                       {"messages": []}):
            with self.subTest(change=change), self.assertRaises(GatewayError):
                validate_request({**base, **change})


class FakeBackend:
    def __init__(self):
        self.calls = []

    def complete(self, payload):
        self.calls.append(payload)
        return {"role": "assistant", "content": "Hello from the test backend."}


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.server = GatewayServer(("127.0.0.1", 0), "private-test-token", ["127.0.0.1"], self.backend)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None, token="private-test-token"):
        request = urllib.request.Request(self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        return urllib.request.urlopen(request, timeout=3)

    def test_authentication_is_required_before_model_invocation(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/chat", {"model": MODEL, "messages": [{"role": "user", "content": "Hello"}]}, token="incorrect")
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(self.backend.calls, [])
        caught.exception.close()

    def test_client_address_is_restricted(self):
        self.server.allowed_hosts = {"192.0.2.10"}
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/tags")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_ollama_discovery_and_streamed_chat(self):
        with self.request("/api/tags") as result:
            self.assertEqual(json.load(result)["models"][0]["model"], MODEL)
        with self.request("/api/show", {"model": MODEL}) as result:
            self.assertIn("tools", json.load(result)["capabilities"])
        payload = {"model": MODEL, "stream": True,
                   "messages": [{"role": "user", "content": "Hello"}]}
        with self.request("/api/chat", payload) as result:
            self.assertEqual(result.headers["Content-Type"], "application/x-ndjson")
            reply = json.loads(result.readline())
            self.assertTrue(reply["done"])
            self.assertIn("test backend", reply["message"]["content"])
            self.assertEqual(result.readline(), b"")
        self.assertEqual(self.backend.calls, [payload])


if __name__ == "__main__":
    unittest.main()
