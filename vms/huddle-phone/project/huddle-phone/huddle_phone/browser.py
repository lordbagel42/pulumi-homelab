"""Headless WebRTC runtime for amazon-chime-sdk-js. Never loads Slack's UI."""

import asyncio
import contextlib
import os

from playwright.async_api import Error as BrowserError, async_playwright


class ChimeError(RuntimeError):
    """Only allow fixed diagnostic labels across the browser/log boundary."""

    def __init__(self, detail=None):
        detail = detail if isinstance(detail, dict) else {}
        stage = detail.get("stage")
        if stage not in ("configure", "audio-output", "audio-input", "connecting", "browser", "timeout"):
            stage = "unknown"
        kind = detail.get("errorType")
        if kind not in ("Error", "TypeError", "ReferenceError", "NotFoundError", "NotReadableError",
                        "PermissionDeniedError", "NotAllowedError", "NotSupportedError",
                        "OverconstrainedError", "GetUserMediaError", "TimeoutError", "AbortError", "BrowserError"):
            kind = "Error"
        code = detail.get("statusCode")
        code = str(code) if type(code) is int and 0 <= code <= 999 else "none"
        super().__init__(f"Chime stage={stage} error_type={kind} status_code={code}")


class ChimeRuntime:
    def __init__(self, config):
        self.config = config
        self.playwright = self.browser = self.page = None

    async def prepare(self, call_id, token):
        self.playwright = await async_playwright().start()
        # No Slack session credentials in Chromium's process environment.
        env = {key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "LANG", "LC_ALL")}
        self.browser = await self.playwright.chromium.launch(
            executable_path=self.config.chromium, headless=True, env=env,
            ignore_default_args=["--mute-audio"],
            args=["--autoplay-policy=no-user-gesture-required", "--disable-dev-shm-usage",
                  "--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
        )
        self.page = await self.browser.new_page()
        await self.page.goto(f"http://127.0.0.1:{self.config.http_port}/", wait_until="load")
        await self.page.wait_for_function("typeof window.preparePhone === 'function'")
        await asyncio.wait_for(self.page.evaluate("p => window.preparePhone(p)",
                                                  {"callId": call_id, "token": token}), 10)

    async def join(self, credentials):
        try:
            result = await asyncio.wait_for(self.page.evaluate("p => window.joinHuddle(p)", credentials), 35)
        except TimeoutError:
            raise ChimeError({"stage": "timeout", "errorType": "TimeoutError"}) from None
        except BrowserError:
            raise ChimeError({"stage": "browser", "errorType": "BrowserError"}) from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ChimeError(result)

    async def healthy(self):
        return (self.page is not None and not self.page.is_closed()
                and await self.page.evaluate("window.mediaReady && !window.mediaFailed"))

    async def close(self):
        if self.page and not self.page.is_closed():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.page.evaluate("window.stopHuddle()"), 5)
        if self.browser:
            with contextlib.suppress(Exception):
                await self.browser.close()
        if self.playwright:
            with contextlib.suppress(Exception):
                await self.playwright.stop()
