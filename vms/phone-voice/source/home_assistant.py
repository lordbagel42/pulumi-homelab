"""Home Assistant Assist client; no speech models or third-party dependencies."""

import asyncio
import json
import math
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "home-assistant.json"


class HomeAssistantError(Exception):
    """A message safe to speak or log, without credentials or response bodies."""


class ConfigurationError(HomeAssistantError):
    pass


@dataclass(frozen=True)
class Config:
    url: str
    token: str = field(repr=False)
    agent_id: str | None = None
    timeout: float = 20

    def __post_init__(self):
        try:
            parsed = urllib.parse.urlsplit(self.url)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment)
            parsed.port  # Validate a malformed port before the first call.
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid or any(char.isspace() for char in self.url):
            raise ConfigurationError("Set a valid Home Assistant HTTP or HTTPS URL.")
        if not isinstance(self.token, str) or not self.token.strip() or any(
                char.isspace() for char in self.token):
            raise ConfigurationError("Set a Home Assistant long-lived access token.")
        if self.agent_id is not None and (not isinstance(self.agent_id, str) or not self.agent_id.strip()):
            raise ConfigurationError("Set a valid Home Assistant conversation agent.")
        if (not isinstance(self.timeout, (int, float)) or isinstance(self.timeout, bool)
                or not math.isfinite(self.timeout) or not 0 < self.timeout <= 120):
            raise ConfigurationError("Home Assistant timeout must be between zero and 120 seconds.")
        object.__setattr__(self, "url", self.url.rstrip("/"))

    @classmethod
    def load(cls, path=None):
        path = Path(path or os.environ.get("HOME_ASSISTANT_CONFIG", CONFIG_PATH)).expanduser()
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                raise ConfigurationError("Could not read the Home Assistant configuration file.") from None
            if not isinstance(data, dict):
                raise ConfigurationError("Home Assistant configuration must be a JSON object.")
        token = os.environ.get("HOME_ASSISTANT_TOKEN", data.get("token", ""))
        token_file = os.environ.get("HOME_ASSISTANT_TOKEN_FILE", data.get("token_file"))
        if token_file and "HOME_ASSISTANT_TOKEN" not in os.environ:
            try:
                token_path = Path(token_file).expanduser()
                if not token_path.is_absolute():
                    token_path = path.parent / token_path
                token = token_path.read_text().strip()
            except (OSError, TypeError, ValueError):
                raise ConfigurationError("Could not read the Home Assistant token file.") from None
        return cls(
            url=os.environ.get("HOME_ASSISTANT_URL", data.get("url", "")),
            token=token,
            agent_id=os.environ.get("HOME_ASSISTANT_AGENT_ID", data.get("agent_id")),
            timeout=data.get("timeout", 20),
        )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the bearer token to a redirect destination.
        return None


@dataclass(frozen=True)
class Reply:
    speech: str
    conversation_id: str | None
    response_type: str


def parse_reply(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        raise HomeAssistantError("Home Assistant returned an invalid response. I could not confirm the result.")
    response = payload["response"]
    response_type = response.get("response_type")
    if response_type not in {"action_done", "query_answer", "error"}:
        raise HomeAssistantError("Home Assistant returned an invalid response. I could not confirm the result.")
    speech = response.get("speech") or {}
    spoken = ""
    if isinstance(speech, dict):
        plain = speech.get("plain") or {}
        if isinstance(plain, dict) and isinstance(plain.get("speech"), str):
            spoken = plain["speech"].strip()
        ssml = speech.get("ssml") or {}
        if not spoken and isinstance(ssml, dict) and isinstance(ssml.get("speech"), str):
            try:
                spoken = " ".join(ET.fromstring(ssml["speech"]).itertext()).strip()
            except ET.ParseError:
                pass
    if not spoken:
        data = response.get("data") or {}
        failed = data.get("failed") if isinstance(data, dict) else None
        if response_type == "error":
            spoken = "Home Assistant could not handle that request. Please try a device or room name."
        elif failed:
            spoken = "Home Assistant could not complete the request for every device. Please check their status."
        elif response_type == "action_done":
            spoken = "Home Assistant completed the request."
        else:
            spoken = "Home Assistant did not provide a spoken answer."
    conversation_id = payload.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        conversation_id = None
    return Reply(spoken, conversation_id, response_type)


class HomeAssistant:
    def __init__(self, config):
        self.config = config
        # Ignore system proxies for this local connection; HTTPS still verifies certificates.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _request(self, path, data=None):
        request = urllib.request.Request(
            self.config.url + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer " + self.config.token,
                     "Content-Type": "application/json", "Accept": "application/json"},
            method="POST" if data is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                # Bound bad upstream responses instead of buffering arbitrary amounts of data.
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise HomeAssistantError("Home Assistant returned too much data. I could not confirm the result.")
                return json.loads(body)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                message = "Home Assistant rejected the access token. Please update the phone's Home Assistant configuration."
            elif error.code == 404:
                message = "Home Assistant's API was not found. Check the URL and that Assist is enabled."
            elif 300 <= error.code < 400:
                message = "Home Assistant redirected the request. Configure its final URL and try again."
            else:
                message = "Home Assistant could not confirm the request. Please check the device's status before trying again."
            error.close()
            raise HomeAssistantError(message) from None
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError):
            # A timed-out action may have run. Never retry it automatically.
            raise HomeAssistantError(
                "I could not reach Home Assistant or confirm the result. Please check the connection and device's status before trying again.") from None
        except (ValueError, UnicodeError):
            raise HomeAssistantError("Home Assistant returned an invalid response. I could not confirm the result.") from None

    async def check(self):
        result = await asyncio.to_thread(self._request, "/api/")
        if not isinstance(result, dict) or result.get("message") != "API running.":
            raise HomeAssistantError("The configured URL did not return Home Assistant's API.")

    async def process(self, text, conversation_id=None):
        platform = os.environ.get("PHONE_PLATFORM_URL", "").rstrip("/")
        if platform:
            try:
                token = Path(os.environ.get("PHONE_PLATFORM_TOKEN_FILE", "/var/lib/codex-phone/switchboard/admin-token")).read_text().strip()
            except OSError:
                raise HomeAssistantError("The shared phone service credentials are unavailable.") from None
            def send():
                request = urllib.request.Request(platform+"/api/v1/integrations/home-assistant/conversation",
                    data=json.dumps({"text":text,"conversation_id":conversation_id}).encode(),
                    headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
                try:
                    with self.opener.open(request,timeout=self.config.timeout) as response:
                        return json.loads(response.read(1024*1024))
                except (urllib.error.URLError, OSError, ValueError):
                    raise HomeAssistantError("The shared phone service could not confirm the result. Please check the device's status before trying again.") from None
            return parse_reply(await asyncio.to_thread(send))
        data = {"text": text, "language": "en"}
        if self.config.agent_id:
            data["agent_id"] = self.config.agent_id
        if conversation_id:
            data["conversation_id"] = conversation_id
        result = await asyncio.to_thread(self._request, "/api/conversation/process", data)
        return parse_reply(result)
