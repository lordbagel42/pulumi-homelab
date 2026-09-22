import unittest
from unittest.mock import patch, AsyncMock
from callback_policy import callback_permitted


class CallbackPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_never_contacts_platform(self):
        with patch.dict('os.environ', {'PHONE_PLATFORM_CALLBACK_POLICY':'0'}), patch('control.platform_request',new_callable=AsyncMock) as request:
            self.assertTrue(await callback_permitted())
            request.assert_not_called()

    async def test_schedule_and_outage(self):
        with patch.dict('os.environ', {'PHONE_PLATFORM_CALLBACK_POLICY':'1'}), patch('control.platform_request',new_callable=AsyncMock) as request:
            request.return_value={'active':True}
            self.assertFalse(await callback_permitted())
            request.return_value={'active':False}
            self.assertTrue(await callback_permitted())
            request.return_value={}
            self.assertFalse(await callback_permitted())
            request.side_effect=RuntimeError('Unavailable')
            self.assertFalse(await callback_permitted())
