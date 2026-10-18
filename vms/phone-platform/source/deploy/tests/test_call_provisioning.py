"""Exercise call provisioning in disposable state, never a running PBX."""

import importlib.util
import os
import pwd
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("provision", ROOT / "deploy/provision.py")
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


class CallProvisioningTests(unittest.TestCase):
    def test_aliases_and_credentials_survive_repeat_deploy_and_333_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / "asterisk").mkdir()
            routes = state / "asterisk/routes.conf"
            alias = "exten => 123,1,Goto(sccp-internal,611,1)\n"
            routes.write_text("[switchboard-routes]\n" + alias)
            account = pwd.getpwuid(os.getuid())
            credentials = {"channel": "SCCP/6738", "user": "huddle-phone", "secret": "fixture-" * 8}
            with patch.object(provision, "STATE", state):
                for _ in range(2):
                    provision.configure_calls(ROOT, account, credentials)
                contents = routes.read_text()
                self.assertIn(alias, contents)
                self.assertEqual(1, contents.count('#tryinclude "switchboard/stream-calls.conf"'))
                self.assertEqual(1, contents.count('#tryinclude "switchboard/incoming-calls.conf"'))
                self.assertEqual(1, contents.count("include => switchboard-incoming-route"))
                self.assertEqual((state / "calls.env").stat().st_mode & 0o777, 0o600)
                self.assertIn("PHONE_PLATFORM_CALL_INCOMING_PORT=4573\n", (state / "calls.env").read_text())
                conflict = contents + "exten => 333,1,Goto(sccp-internal,555,1)\n"
                routes.write_text(conflict)
                with self.assertRaisesRegex(ValueError, "333"):
                    provision.configure_calls(ROOT, account, credentials)
                self.assertEqual(conflict, routes.read_text())


if __name__ == "__main__":
    unittest.main()
