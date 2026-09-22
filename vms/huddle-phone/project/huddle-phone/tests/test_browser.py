from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from playwright.async_api import Error as BrowserError

from huddle_phone.browser import ChimeError, ChimeRuntime


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_reports_safe_initialization_failure(self):
        runtime = ChimeRuntime(SimpleNamespace())
        runtime.page = SimpleNamespace(evaluate=AsyncMock(return_value={
            "ok": False, "stage": "configure", "errorType": "ReferenceError", "statusCode": None}))
        with self.assertRaisesRegex(ChimeError, "stage=configure error_type=ReferenceError status_code=none"):
            await runtime.join({})

    async def test_browser_exception_cannot_disclose_credentials(self):
        runtime = ChimeRuntime(SimpleNamespace())
        runtime.page = SimpleNamespace(evaluate=AsyncMock(side_effect=BrowserError("private-join-token")))
        with self.assertRaises(ChimeError) as caught:
            await runtime.join({})
        self.assertEqual(str(caught.exception), "Chime stage=browser error_type=BrowserError status_code=none")

    async def test_server_disallows_freeform_browser_diagnostics(self):
        error = ChimeError({"stage": "private-token", "errorType": "private-token",
                            "statusCode": "private-token"})
        self.assertEqual(str(error), "Chime stage=unknown error_type=Error status_code=none")

    async def test_join_success(self):
        runtime = ChimeRuntime(SimpleNamespace())
        runtime.page = SimpleNamespace(evaluate=AsyncMock(return_value={"ok": True}))
        await runtime.join({})
