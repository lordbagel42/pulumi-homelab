import unittest

from huddle_phone.config import Config


class ConfigTests(unittest.TestCase):
    def env(self):
        return {"SLACK_TEAM_ID": "T123", "SLACK_CHANNEL_ID": "C123", "SLACK_WORKSPACE": "example",
                "SLACK_CLIENT_TOKEN": "xoxc-test-secret", "SLACK_COOKIE": "d=xoxd-cookie-secret"}

    def test_defaults_and_secret_redaction(self):
        config = Config.from_env(self.env())
        self.assertTrue(config.dry_run)
        self.assertEqual(config.phone_channel, "SCCP/6738")
        self.assertNotIn("test-secret", repr(config))
        self.assertNotIn("cookie-secret", repr(config))

    def test_live_mode_requires_ami_secret(self):
        with self.assertRaises(ValueError):
            Config.from_env({**self.env(), "DRY_RUN": "false"})

    def test_enterprise_hostname_and_ids(self):
        for host in ("example.enterprise", "example.enterprise.slack.com"):
            config = Config.from_env({**self.env(), "SLACK_WORKSPACE": host, "SLACK_ENTERPRISE_ID": "E123"})
            self.assertEqual(config.workspace, "example.enterprise")
            self.assertEqual(config.team_id, "T123")
            self.assertEqual(config.enterprise_id, "E123")
        with self.assertRaises(ValueError):
            Config.from_env({**self.env(), "SLACK_WORKSPACE": "example.slack.com.attacker.test"})

    def test_invalid_endpoints_and_injection_are_rejected(self):
        for key, value in (("SLACK_WORKSPACE", "example.com/steal"), ("SLACK_CHANNEL_ID", "C12\nInjected"),
                           ("SLACK_COOKIE", "d=xoxd-test\r\nInjected: true"),
                           ("PHONE_CHANNEL", "SCCP/6738&other"), ("DRY_RUN", "sometimes"),
                           ("MAX_CALL_SECONDS", "0")):
            with self.assertRaises(ValueError, msg=key):
                Config.from_env({**self.env(), key: value})
